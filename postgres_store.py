"""PostgreSQL access for searchable / discoverable data assets (repo `.env` POSTGRES_*)."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mongo_store import load_repo_dotenv

_log = logging.getLogger("datahive.postgres_store")

_REPO_ROOT = Path(__file__).resolve().parent

DEFAULT_ASSET_SCHEMAS = ("silver", "gold")

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


def _dotenv_file_values() -> dict[str, str]:
    """Read repo-root `.env`; file values beat os.environ for POSTGRES_* (setdefault misses updates)."""
    out: dict[str, str] = {}
    path = _REPO_ROOT / ".env"
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("POSTGRES_"):
            out[key] = value.strip().strip('"').strip("'")
    return out


def _pg_setting(name: str, default: str) -> str:
    load_repo_dotenv()
    file_vals = _dotenv_file_values()
    if name in file_vals and file_vals[name] != "":
        return file_vals[name]
    return os.environ.get(name, default)


def asset_schemas() -> tuple[str, ...]:
    raw = _pg_setting("POSTGRES_ASSET_SCHEMAS", "silver,gold")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else DEFAULT_ASSET_SCHEMAS


def postgres_dsn_kwargs() -> dict[str, Any]:
    load_repo_dotenv()
    return {
        "host": _pg_setting("POSTGRES_HOST", "localhost"),
        "port": int(_pg_setting("POSTGRES_PORT", "5432")),
        "dbname": _pg_setting("POSTGRES_DATABASE", "datahive"),
        "user": _pg_setting("POSTGRES_USER", "postgres"),
        "password": _pg_setting("POSTGRES_PASSWORD", ""),
    }


def postgres_conninfo() -> str | None:
    """Optional URI override. Uses POSTGRES_CONNINFO or POSTGRES_URI only (not DATABASE_URL)."""
    for key in ("POSTGRES_CONNINFO", "POSTGRES_URI"):
        val = _pg_setting(key, "").strip()
        if val:
            return val
    return None


def postgres_connect():
    """Open a psycopg connection using repo `.env` POSTGRES_* settings."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    explicit = postgres_conninfo()
    if explicit:
        return psycopg.connect(explicit, connect_timeout=5)
    kw = postgres_dsn_kwargs()
    return psycopg.connect(
        make_conninfo(
            host=kw["host"],
            port=kw["port"],
            dbname=kw["dbname"],
            user=kw["user"],
            password=kw["password"],
        ),
        connect_timeout=5,
    )


def redacted_postgres_host() -> str:
    kw = postgres_dsn_kwargs()
    schemas = ", ".join(asset_schemas())
    return f"{kw['user']}@{kw['host']}:{kw['port']}/{kw['dbname']} (schemas: {schemas})"


@contextmanager
def postgres_connection() -> Iterator[Any]:
    conn = postgres_connect()
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


def _allowed_schema_name(schema: str) -> bool:
    allowed = asset_schemas()
    return schema in allowed or schema.lower() in {a.lower() for a in allowed}


def _resolve_namespace(cur: Any, schema: str) -> str:
    cur.execute(
        """
        SELECT nspname
        FROM pg_catalog.pg_namespace
        WHERE nspname = %s OR lower(nspname) = lower(%s)
        ORDER BY CASE WHEN nspname = %s THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (schema, schema, schema),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Schema '{schema}' was not found in PostgreSQL.")
    return str(row[0])


def _relation_type_label(relkind: str) -> str:
    if relkind in ("v", "m"):
        return "View"
    if relkind == "f":
        return "Foreign Table"
    return "Table"


def _list_relations_in_namespace(cur: Any, nspname: str) -> list[tuple[str, str, str]]:
    """Return (name, asset_type, relkind) for user relations in one schema."""
    cur.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND NOT c.relispartition
        ORDER BY c.relname
        """,
        (nspname,),
    )
    out: list[tuple[str, str, str]] = []
    for name, relkind in cur.fetchall():
        out.append((str(name), _relation_type_label(str(relkind)), str(relkind)))
    return out


def _catalog_counts() -> dict[str, int]:
    """All = column count; View / Table = relation counts in asset schemas (pg_catalog)."""
    schemas = list(asset_schemas())
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            resolved: list[str] = []
            for s in schemas:
                try:
                    resolved.append(_resolve_namespace(cur, s))
                except ValueError:
                    continue
            if not resolved:
                return {"All": 0, "View": 0, "Table": 0}

            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_schema = ANY(%s)
                """,
                (resolved,),
            )
            fields_n = int(cur.fetchone()[0])

            tables_n = 0
            views_n = 0
            for nsp in resolved:
                for _name, asset_type, _kind in _list_relations_in_namespace(cur, nsp):
                    if asset_type == "View":
                        views_n += 1
                    elif asset_type == "Table":
                        tables_n += 1

    return {"All": fields_n, "View": views_n, "Table": tables_n}


def catalog_counts() -> dict[str, int]:
    """Public wrapper for chip counts (All / View / Table)."""
    return _catalog_counts()


def _catalog_assets() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            for schema in asset_schemas():
                try:
                    nsp = _resolve_namespace(cur, schema)
                except ValueError:
                    continue
                for name, asset_type, _kind in _list_relations_in_namespace(cur, nsp):
                    items.append(
                        {
                            "name": name,
                            "type": asset_type,
                            "crumb": f"{nsp} › {name}",
                            "edited": "catalog snapshot",
                            "verified": True,
                            "warning": False,
                            "source": "catalog",
                            "schema": nsp,
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


def _apply_type_filter(
    items: list[dict[str, Any]], asset_type: str | None
) -> list[dict[str, Any]]:
    if not asset_type or asset_type == "All":
        return items
    return [i for i in items if i.get("type") == asset_type]


def relevant_assets(
    owner: str,
    tab: str = "recently_verified",
    asset_type: str | None = None,
) -> dict[str, Any]:
    ensure_assets_schema()
    counts = _catalog_counts()
    if tab == "my_drafts":
        items = _table_assets(owner, "my_drafts")
    else:
        items = _catalog_assets()
    items = _apply_type_filter(items, asset_type)
    return {
        "tab": tab,
        "type": asset_type,
        "schemas": list(asset_schemas()),
        "counts": counts,
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
    schemas = list(asset_schemas())
    if not q:
        return {"query": q, "items": items}

    like = f"%{q}%"
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = ANY(%s)
                  AND table_type IN ('BASE TABLE', 'VIEW')
                  AND (table_name ILIKE %s OR table_schema ILIKE %s)
                ORDER BY table_schema, table_name
                LIMIT %s OFFSET %s
                """,
                (schemas, like, like, limit, offset),
            )
            for schema, name, table_type in cur.fetchall():
                typ = "View" if table_type == "VIEW" else "Table"
                items.append({"name": name, "type": typ, "schema": schema})

            if len(items) < limit:
                cur.execute(
                    """
                    SELECT table_schema, table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = ANY(%s)
                      AND (column_name ILIKE %s OR table_name ILIKE %s)
                    ORDER BY table_schema, table_name, ordinal_position
                    LIMIT %s
                    """,
                    (schemas, like, like, limit - len(items)),
                )
                seen = {(i["name"], i["type"], i.get("schema")) for i in items}
                for schema, table, column in cur.fetchall():
                    label = f"{table}.{column}"
                    key = (label, "Column", schema)
                    if key in seen:
                        continue
                    items.append({"name": label, "type": "Column", "schema": schema})
                    seen.add(key)

            if len(items) < limit:
                cur.execute(
                    """
                    SELECT DISTINCT name, type
                    FROM assets
                    WHERE owner = %s AND (name ILIKE %s OR type ILIKE %s OR crumb ILIKE %s)
                    ORDER BY name
                    LIMIT %s
                    """,
                    (owner, like, like, like, limit - len(items)),
                )
                seen = {(i["name"], i["type"]) for i in items}
                for name, typ in cur.fetchall():
                    key = (name, typ)
                    if key in seen:
                        continue
                    items.append({"name": name, "type": typ})
                    seen.add(key)

    return {"query": q, "items": items[:limit]}


def discover_assets(owner: str, *, limit: int = 100) -> dict[str, Any]:
    ensure_assets_schema()
    _ = owner
    _ = limit
    items = _catalog_assets()
    return {"items": items, "schemas": list(asset_schemas()), "counts": _catalog_counts()}


def list_schemas() -> list[str]:
    """Configured asset schemas that exist in PostgreSQL (silver, gold, …)."""
    configured = list(asset_schemas())
    found: list[str] = []
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            for name in configured:
                try:
                    found.append(_resolve_namespace(cur, name))
                except ValueError:
                    _log.warning("asset schema %r not found in PostgreSQL", name)
    # De-dupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for s in found:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered if ordered else list(configured)


def list_tables(schema: str) -> list[dict[str, Any]]:
    """All tables/views in one schema (pg_catalog — matches what you see in Postgres)."""
    if not _allowed_schema_name(schema):
        raise ValueError(f"Schema '{schema}' is not in the configured asset schemas.")
    with postgres_connection() as conn:
        with conn.cursor() as cur:
            nsp = _resolve_namespace(cur, schema)
            rels = _list_relations_in_namespace(cur, nsp)
    return [{"name": name, "type": asset_type} for name, asset_type, _kind in rels]


def table_structure(schema: str, table: str) -> dict[str, Any]:
    """Column-level structure (name, type, nullable, default, primary key) for one table/view."""
    if not _allowed_schema_name(schema):
        raise ValueError(f"Schema '{schema}' is not in the configured asset schemas.")

    with postgres_connection() as conn:
        with conn.cursor() as cur:
            nsp = _resolve_namespace(cur, schema)
            cur.execute(
                """
                SELECT c.relkind
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                """,
                (nsp, table),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Table '{nsp}.{table}' was not found.")
            table_type = _relation_type_label(str(row[0]))

            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default,
                       character_maximum_length, numeric_precision, numeric_scale,
                       ordinal_position
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (nsp, table),
            )
            cols = cur.fetchall()

            cur.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s AND tc.table_name = %s
                """,
                (nsp, table),
            )
            pk_columns = {r[0] for r in cur.fetchall()}

    columns: list[dict[str, Any]] = []
    for name, data_type, nullable, default, char_len, num_prec, num_scale, position in cols:
        type_display = data_type
        if char_len:
            type_display = f"{data_type}({char_len})"
        elif num_prec is not None and data_type in ("numeric", "decimal"):
            type_display = (
                f"{data_type}({num_prec},{num_scale})" if num_scale else f"{data_type}({num_prec})"
            )
        columns.append(
            {
                "position": position,
                "name": name,
                "type": type_display,
                "nullable": nullable == "YES",
                "default": default,
                "primary_key": name in pk_columns,
            }
        )

    return {
        "schema": nsp,
        "table": table,
        "table_type": table_type,
        "columns": columns,
    }
