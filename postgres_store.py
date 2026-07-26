"""PostgreSQL access for searchable / discoverable data assets (repo `.env` POSTGRES_*)."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from mongo_store import load_repo_dotenv

_log = logging.getLogger("datahive.postgres_store")

ASSET_TYPES = ("View", "Column", "Query", "Term", "Category", "Glossary", "Dashboard", "Table")

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS assets (
        id SERIAL PRIMARY KEY,
        owner TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        crumb TEXT,
        edited TEXT,
        verified BOOLEAN NOT NULL DEFAULT FALSE,
        warning BOOLEAN NOT NULL DEFAULT FALSE,
        tab TEXT NOT NULL DEFAULT 'recently_verified'
    )
    """,
    "CREATE INDEX IF NOT EXISTS assets_owner_tab_idx ON assets (owner, tab)",
)


def _pg_setting(name: str, default: str) -> str:
    load_repo_dotenv()
    return os.environ.get(name, default)


def postgres_dsn_kwargs() -> dict[str, Any]:
    load_repo_dotenv()
    return {
        "host": _pg_setting("POSTGRES_HOST", "localhost"),
        "port": int(_pg_setting("POSTGRES_PORT", "5432")),
        "dbname": _pg_setting("POSTGRES_DATABASE", "datahive"),
        "user": _pg_setting("POSTGRES_USER", "postgres"),
        "password": _pg_setting("POSTGRES_PASSWORD", ""),
    }


def redacted_postgres_host() -> str:
    kw = postgres_dsn_kwargs()
    return f"{kw['user']}@{kw['host']}:{kw['port']}/{kw['dbname']}"


@contextmanager
def postgres_connection() -> Iterator[Any]:
    import psycopg

    conn = psycopg.connect(**postgres_dsn_kwargs(), connect_timeout=5)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping_postgres() -> None:
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")


def ensure_assets_schema() -> None:
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            for stmt in _SCHEMA_STATEMENTS:
                cur.execute(stmt)
            cur.execute("SELECT COUNT(*) FROM assets")
            count = int(cur.fetchone()[0])
            if count == 0:
                seed = [
                    (
                        "Admin",
                        "Food Beverage Order Analysis",
                        "Dashboard",
                        "Dashboard › Food Beverage Order Analysis",
                        "edited 3 months ago",
                        True,
                        True,
                        "recently_verified",
                    ),
                    (
                        "Admin",
                        "Customer Acquisition Cost Metrics",
                        "Dashboard",
                        "Dashboard › Customer Acquisition Cost",
                        "edited 3 months ago",
                        True,
                        False,
                        "recently_verified",
                    ),
                    (
                        "Admin",
                        "Revenue by Region (draft)",
                        "Query",
                        "Query › Revenue by Region",
                        "edited yesterday",
                        False,
                        False,
                        "my_drafts",
                    ),
                ]
                cur.executemany(
                    """
                    INSERT INTO assets (owner, name, type, crumb, edited, verified, warning, tab)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    seed,
                )


def _catalog_assets(limit: int = 200) -> list[dict[str, Any]]:
    sql = """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
          AND table_type IN ('BASE TABLE', 'VIEW')
        ORDER BY table_schema, table_name
        LIMIT %s
    """
    items: list[dict[str, Any]] = []
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
    for schema, name, table_type in rows:
        asset_type = "View" if table_type == "VIEW" else "Table"
        items.append(
            {
                "name": name,
                "type": asset_type,
                "crumb": f"{schema} › {name}",
                "edited": "catalog snapshot",
                "verified": True,
                "warning": False,
                "source": "catalog",
            }
        )
    return items


def _table_assets(owner: str, tab: str) -> list[dict[str, Any]]:
    sql = """
        SELECT name, type, crumb, edited, verified, warning
        FROM assets
        WHERE owner = %s AND tab = %s
        ORDER BY id DESC
    """
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (owner, tab))
            rows = cur.fetchall()
    return [
        {
            "name": r[0],
            "type": r[1],
            "crumb": r[2] or r[1],
            "edited": r[3] or "",
            "verified": bool(r[4]),
            "warning": bool(r[5]),
            "source": "assets",
        }
        for r in rows
    ]


def _merge_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {t: 0 for t in ASSET_TYPES}
    for it in items:
        t = it.get("type") or "Table"
        counts[t] = counts.get(t, 0) + 1
    return counts


def relevant_assets(
    owner: str,
    tab: str = "recently_verified",
    asset_type: str | None = None,
) -> dict[str, Any]:
    ensure_assets_schema()
    if tab == "my_drafts":
        pool = _table_assets(owner, "my_drafts")
    else:
        pool = [i for i in _table_assets(owner, "recently_verified") if i.get("verified")]
        pool.extend(_catalog_assets())
    count_pool = _table_assets(owner, tab) + _catalog_assets()
    items = pool
    if asset_type:
        items = [i for i in items if i.get("type") == asset_type]
    return {
        "tab": tab,
        "type": asset_type,
        "counts": _merge_counts(count_pool),
        "items": items,
    }


def search_assets(
    owner: str,
    query: str,
    *,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    ensure_assets_schema()
    q = (query or "").strip()
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    items: list[dict[str, Any]] = []
    if not q:
        return {"query": q, "items": items}

    like = f"%{q}%"
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT name, type
                FROM assets
                WHERE owner = %s AND (name ILIKE %s OR type ILIKE %s OR crumb ILIKE %s)
                ORDER BY name
                LIMIT %s OFFSET %s
                """,
                (owner, like, like, like, limit, offset),
            )
            for name, typ in cur.fetchall():
                items.append({"name": name, "type": typ})

            if len(items) < limit:
                cur.execute(
                    """
                    SELECT table_schema, table_name, table_type
                    FROM information_schema.tables
                    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                      AND (table_name ILIKE %s OR table_schema ILIKE %s)
                    ORDER BY table_schema, table_name
                    LIMIT %s
                    """,
                    (like, like, limit - len(items)),
                )
                seen = {(i["name"], i["type"]) for i in items}
                for schema, name, table_type in cur.fetchall():
                    typ = "View" if table_type == "VIEW" else "Table"
                    key = (name, typ)
                    if key in seen:
                        continue
                    items.append({"name": name, "type": typ})
                    seen.add(key)

            if len(items) < limit:
                cur.execute(
                    """
                    SELECT table_schema, table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                      AND (column_name ILIKE %s OR table_name ILIKE %s)
                    ORDER BY table_schema, table_name, column_name
                    LIMIT %s
                    """,
                    (like, like, limit - len(items)),
                )
                seen = {(i["name"], i["type"]) for i in items}
                for _schema, table, column in cur.fetchall():
                    label = f"{table}.{column}"
                    key = (label, "Column")
                    if key in seen:
                        continue
                    items.append({"name": label, "type": "Column"})
                    seen.add(key)

    return {"query": q, "items": items[:limit]}


def discover_assets(owner: str, *, limit: int = 100) -> dict[str, Any]:
    ensure_assets_schema()
    items = _table_assets(owner, "recently_verified") + _table_assets(owner, "my_drafts")
    items.extend(_catalog_assets(limit=limit))
    return {"items": items, "counts": _merge_counts(items)}
