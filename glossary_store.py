"""Unified glossary apply across connectors (AWS, Azure, GCP, Snowflake, Postgres, …).

Canonical store: MongoDB `asset_glossary`.
Source sync (when a connector resolves):
  - Postgres  → COMMENT ON COLUMN
  - Snowflake → COMMENT ON COLUMN / COMMENT ON TABLE
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mongo_store
import postgres_store

_log = logging.getLogger("datahive.glossary_store")

POSTGRES_PLATFORMS = frozenset(
    {"postgres", "postgresql", "pg", "local", "datahive", "datahivepoc"}
)
SNOWFLAKE_PLATFORMS = frozenset({"snowflake", "sf"})
PLATFORM_ALIASES = {
    "google": "gcp",
    "google_cloud": "gcp",
    "bigquery": "gcp",
    "s3": "aws",
    "redshift": "aws",
    "glue": "aws",
    "synapse": "azure",
    "fabric": "azure",
    "mssql": "azure",
    "sf": "snowflake",
    "postgresql": "postgres",
    "pg": "postgres",
    "local postgres": "postgres",
    "local_postgres": "postgres",
}
# When spreadsheet `connection` is just a platform label (e.g. "Snowflake").
PLATFORM_CONNECTION_LABELS = frozenset(
    {
        "snowflake",
        "sf",
        "aws",
        "azure",
        "gcp",
        "google",
        "bigquery",
        "postgres",
        "postgresql",
        "pg",
        "local",
        "local postgres",
    }
)


def asset_glossary_collection_name() -> str:
    return mongo_store.asset_glossary_collection_name()


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _norm_key(value: str) -> str:
    return _norm(value).lower()


def _normalize_platform(raw: str | None) -> str:
    text = _norm_key(raw or "")
    if not text:
        return ""
    return PLATFORM_ALIASES.get(text, text)


def _is_postgres_platform(platform: str) -> bool:
    return _normalize_platform(platform) in POSTGRES_PLATFORMS or platform == "postgres"


def _is_snowflake_platform(platform: str) -> bool:
    return _normalize_platform(platform) in SNOWFLAKE_PLATFORMS


def _connector_platform(doc: dict[str, Any], hint: str = "") -> str:
    cloud = _normalize_platform(
        str(doc.get("cloud") or doc.get("connector_type") or hint or "")
    )
    if cloud in POSTGRES_PLATFORMS:
        return "postgres"
    return cloud or "unknown"


def _from_connector(doc: dict[str, Any], *, name: str, hint: str, source: str) -> dict[str, Any]:
    platform = _connector_platform(doc, hint)
    display = str(doc.get("display_name") or name)
    return {
        "connection": display,
        "connection_key": _norm_key(display),
        "platform": platform,
        "connector_id": doc.get("id"),
        "display_name": display,
        "resolved": True,
        "source": source,
        "cloud": doc.get("cloud"),
        "mode": doc.get("mode"),
        "doc": doc,
    }


def resolve_connection(connection: str, platform_hint: str = "") -> dict[str, Any]:
    """
    Map spreadsheet `connection` (+ optional `platform`) to a connector_dtls document.

    Resolution order:
      1. Exact connector display_name / id match
      2. Connection label is a platform name (e.g. "Snowflake") → first connector of that cloud
      3. platform_hint column → first connector of that cloud
      4. Blank connection + postgres-ish hint → local Postgres
      5. Unresolved spreadsheet name (registry-only; no source sync)
    """
    name = _norm(connection)
    hint = _normalize_platform(platform_hint)
    name_key = _norm_key(name)

    # 1) Named connector
    if name and name_key not in PLATFORM_CONNECTION_LABELS:
        try:
            matched = mongo_store.find_connector_by_name(name)
        except RuntimeError as exc:
            _log.warning("connector lookup failed for %r: %s", name, exc)
            matched = None
        if matched:
            return _from_connector(matched, name=name, hint=hint, source="connector_dtls")

    # 2/3) Platform label or explicit platform column
    platform_from_label = (
        _normalize_platform(name) if name_key in PLATFORM_CONNECTION_LABELS else ""
    )
    platform = hint or platform_from_label
    if platform:
        try:
            by_platform = mongo_store.find_connector_by_platform(platform)
        except RuntimeError as exc:
            _log.warning("platform connector lookup failed for %r: %s", platform, exc)
            by_platform = None
        if by_platform:
            return _from_connector(
                by_platform,
                name=str(by_platform.get("display_name") or name or platform),
                hint=platform,
                source="connector_platform",
            )
        # Known platform but no saved connector — still tag correctly (no Postgres fallback).
        if platform in POSTGRES_PLATFORMS:
            platform = "postgres"
        return {
            "connection": name or platform,
            "connection_key": _norm_key(name or platform),
            "platform": platform,
            "connector_id": None,
            "display_name": name or platform,
            "resolved": False,
            "source": "platform_unmatched",
        }

    # 4) Blank / local → Postgres only when clearly local
    if not name or name_key in POSTGRES_PLATFORMS:
        return {
            "connection": name or "postgres",
            "connection_key": _norm_key(name or "postgres"),
            "platform": "postgres",
            "connector_id": None,
            "display_name": name or "postgres",
            "resolved": True,
            "source": "default_postgres",
        }

    # 5) Unresolved free-text connection name
    return {
        "connection": name,
        "connection_key": name_key,
        "platform": "unknown",
        "connector_id": None,
        "display_name": name,
        "resolved": False,
        "source": "spreadsheet",
    }


def _format_source_comment(meta: dict[str, Any]) -> str:
    """Human-readable column comment for source systems (Snowflake / optional Postgres text)."""
    parts: list[str] = []
    business = _norm(str(meta.get("business_name") or ""))
    description = _norm(str(meta.get("description") or meta.get("business_definition") or ""))
    if business and description:
        parts.append(f"{business} — {description}")
    elif business or description:
        parts.append(business or description)
    extras = []
    for key in ("classification", "sensitivity", "owner", "steward"):
        val = _norm(str(meta.get(key) or ""))
        if val:
            extras.append(f"{key}={val}")
    if extras:
        parts.append("[" + "; ".join(extras) + "]")
    text = " ".join(parts).strip()
    if not text:
        text = json.dumps(meta, ensure_ascii=False)
    return text[:2000]


def _sql_quote_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _snowflake_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _snowflake_column_exists(cur, database: str, schema: str, table: str, column: str) -> tuple[str, str, str, str] | None:
    """Return resolved (database, schema, table, column) if the column exists."""
    try:
        cur.execute(
            f"DESCRIBE TABLE {_snowflake_ident(database)}.{_snowflake_ident(schema)}.{_snowflake_ident(table)}"
        )
        rows = list(cur.fetchall() or [])
    except Exception:  # noqa: BLE001
        return None
    want = column.upper()
    for row in rows:
        name = str(row[0] or "")
        if name.upper() == want:
            return database, schema, table, name
    return None


def _snowflake_set_column_comment(
    cur,
    database: str,
    schema: str,
    table: str,
    column: str,
    comment: str,
) -> None:
    fq = (
        f"{_snowflake_ident(database)}.{_snowflake_ident(schema)}."
        f"{_snowflake_ident(table)}.{_snowflake_ident(column)}"
    )
    cur.execute(f"COMMENT ON COLUMN {fq} IS {_sql_quote_literal(comment)}")


def _snowflake_set_table_comment(
    cur,
    database: str,
    schema: str,
    table: str,
    comment: str,
) -> None:
    fq = f"{_snowflake_ident(database)}.{_snowflake_ident(schema)}.{_snowflake_ident(table)}"
    cur.execute(f"COMMENT IF EXISTS ON TABLE {fq} IS {_sql_quote_literal(comment)}")


def _sync_snowflake_column(
    *,
    connector_id: str,
    sf_cache: dict[str, Any],
    database: str,
    schema: str,
    table: str,
    column: str,
    meta: dict[str, Any],
    table_comments_done: set[str],
) -> tuple[str, str, str, str]:
    """Apply COMMENT on Snowflake column (and once per table). Returns resolved names."""
    import snowflake_catalog

    if connector_id not in sf_cache:
        doc = snowflake_catalog.load_connector_doc(connector_id)
        conn = snowflake_catalog.open_connection(doc)
        cur = conn.cursor()
        snowflake_catalog.prepare_session(cur, doc, database=database or "SALES_DB", schema=schema or "RAW")
        # Prefer roles that own RAW objects when available.
        for role in ("DEV_ADMIN_ROLE", "SYSADMIN", "DATA_ENGINEER"):
            try:
                cur.execute(f"USE ROLE {role}")
                snowflake_catalog.prepare_session(
                    cur, doc, database=database or "SALES_DB", schema=schema or "RAW"
                )
                break
            except Exception:  # noqa: BLE001
                continue
        sf_cache[connector_id] = {"conn": conn, "cur": cur, "doc": doc}

    entry = sf_cache[connector_id]
    cur = entry["cur"]
    db = database or "SALES_DB"
    found = _snowflake_column_exists(cur, db, schema, table, column)
    if not found:
        # Retry with uppercase identifiers (common Snowflake default).
        found = _snowflake_column_exists(cur, db.upper(), schema.upper(), table.upper(), column.upper())
    if not found:
        raise LookupError(f"Snowflake column not found: {db}.{schema}.{table}.{column}")

    r_db, r_schema, r_table, r_col = found
    comment = _format_source_comment(meta)
    _snowflake_set_column_comment(cur, r_db, r_schema, r_table, r_col, comment)

    table_key = f"{r_db}.{r_schema}.{r_table}".upper()
    if table_key not in table_comments_done:
        table_comment = _norm(str(meta.get("source_system") or "")) or "DataHive glossary"
        business = _norm(str(meta.get("business_name") or ""))
        # Keep table comment short / stable — first column sync for this table wins.
        _snowflake_set_table_comment(
            cur,
            r_db,
            r_schema,
            r_table,
            f"DataHive glossary · {table_comment}" + (f" · e.g. {business}" if business else ""),
        )
        table_comments_done.add(table_key)

    return r_db, r_schema, r_table, r_col


def apply_glossary_file(path: Path | str) -> dict[str, Any]:
    """
    Apply glossary rows to the unified asset_glossary registry and sync source comments.

    - Always upserts business metadata into Mongo `asset_glossary`
    - Postgres connections → COMMENT ON COLUMN
    - Snowflake connections → COMMENT ON COLUMN (+ table comment once)
    """
    file_path = Path(path)
    rows = postgres_store._read_glossary_rows(file_path)
    if not rows:
        return {
            "rows_total": 0,
            "updated": 0,
            "registry_updated": 0,
            "source_synced": 0,
            "skipped": 0,
            "failed": 0,
            "errors": ["No data rows found in glossary file."],
            "updates": [],
            "platforms": [],
        }

    present: set[str] = set()
    for row in rows:
        present.update(k for k, v in row.items() if k)
    missing_headers = [h for h in ("schema", "table", "column") if h not in present]
    if missing_headers:
        return {
            "rows_total": len(rows),
            "updated": 0,
            "registry_updated": 0,
            "source_synced": 0,
            "skipped": 0,
            "failed": len(rows),
            "errors": [
                "Missing required columns: "
                + ", ".join(missing_headers)
                + ". Expected identifiers: connection, database, schema, table, column "
                "(optional: platform)."
            ],
            "updates": [],
            "platforms": [],
        }

    updated = 0
    registry_updated = 0
    source_synced = 0
    skipped = 0
    failed = 0
    errors: list[str] = []
    updates: list[dict[str, Any]] = []
    platforms_seen: set[str] = set()
    pg_cache: dict[str, Any] = {}
    sf_cache: dict[str, Any] = {}
    sf_table_comments: set[str] = set()

    try:
        for idx, row in enumerate(rows, start=2):
            connection = row.get("connection", "")
            platform_hint = row.get("platform", "") or row.get("cloud", "")
            database = row.get("database", "")
            schema = row.get("schema", "")
            table = row.get("table", "")
            column = row.get("column", "")

            if not schema or not table or not column:
                skipped += 1
                errors.append(f"Row {idx}: skipped — schema/table/column are required.")
                continue

            meta: dict[str, Any] = {}
            for key in postgres_store.GLOSSARY_META_FIELDS:
                val = row.get(key, "")
                if val != "":
                    meta[key] = val
            if not meta:
                skipped += 1
                errors.append(f"Row {idx}: skipped — no metadata fields to apply.")
                continue

            resolved = resolve_connection(connection, platform_hint)
            platform = resolved["platform"]
            platforms_seen.add(platform)
            if not database:
                database = (
                    postgres_store.postgres_database_name()
                    if _is_postgres_platform(platform)
                    else ""
                )

            term_doc = {
                "connection": resolved["connection"],
                "connection_key": resolved["connection_key"],
                "connector_id": resolved.get("connector_id"),
                "platform": platform,
                "database": database,
                "schema": schema,
                "table": table,
                "column": column,
                "metadata": meta,
                **meta,
                "connector_resolved": bool(resolved.get("resolved")),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                mongo_store.upsert_asset_glossary_term(term_doc)
                registry_updated += 1
            except RuntimeError as exc:
                failed += 1
                errors.append(
                    f"Row {idx}: registry write failed — "
                    f"{resolved['connection']}.{database}.{schema}.{table}.{column}: {exc}"
                )
                continue

            sync_status = "registry"
            sync_detail: str | None = None
            resolved_schema, resolved_table, resolved_column = schema, table, column

            if _is_postgres_platform(platform):
                db_name = database or postgres_store.postgres_database_name()
                try:
                    if db_name not in pg_cache:
                        pg_cache[db_name] = postgres_store._postgres_connect_to(db_name)
                        pg_cache[db_name].autocommit = True
                    conn = pg_cache[db_name]
                    with conn.cursor() as cur:
                        found = postgres_store._column_exists(cur, schema, table, column)
                        if not found:
                            failed += 1
                            hint = postgres_store._suggest_column_target(
                                cur, schema, table, column
                            )
                            msg = (
                                f"Row {idx}: Postgres column not found — "
                                f"{db_name}.{schema}.{table}.{column} "
                                f"(registry saved for connection "
                                f"'{resolved['connection']}')"
                            )
                            if hint:
                                msg += f" ({hint})"
                            errors.append(msg)
                            updates.append(
                                {
                                    "row": idx,
                                    "connection": resolved["connection"],
                                    "platform": platform,
                                    "database": db_name,
                                    "schema": schema,
                                    "table": table,
                                    "column": column,
                                    "registry": True,
                                    "source_synced": False,
                                }
                            )
                            continue
                        nsp, rel, att = found
                        resolved_schema, resolved_table, resolved_column = nsp, rel, att
                        cur.execute(
                            """
                            SELECT pg_catalog.col_description(c.oid, a.attnum)
                            FROM pg_catalog.pg_attribute a
                            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s
                            """,
                            (nsp, rel, att),
                        )
                        existing_raw = cur.fetchone()
                        existing = (
                            postgres_store._parse_column_metadata(
                                existing_raw[0] if existing_raw else None
                            )
                            or {}
                        )
                        if not isinstance(existing, dict):
                            existing = {}
                        merged = dict(existing)
                        merged.update(meta)
                        merged["connection"] = resolved["connection"]
                        merged["database"] = db_name
                        merged["platform"] = "postgres"
                        postgres_store._set_column_comment(
                            cur, nsp, rel, att, json.dumps(merged, ensure_ascii=False)
                        )
                    source_synced += 1
                    sync_status = "postgres_comment"
                    database = db_name
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(
                        f"Row {idx}: Postgres sync failed — "
                        f"{db_name}.{schema}.{table}.{column}: {exc} "
                        f"(registry saved)"
                    )
                    updates.append(
                        {
                            "row": idx,
                            "connection": resolved["connection"],
                            "platform": platform,
                            "database": db_name,
                            "schema": schema,
                            "table": table,
                            "column": column,
                            "registry": True,
                            "source_synced": False,
                        }
                    )
                    continue

            elif _is_snowflake_platform(platform):
                connector_id = resolved.get("connector_id")
                db_name = database or "SALES_DB"
                if not connector_id:
                    sync_detail = (
                        f"No Snowflake connector found for '{resolved['connection']}'. "
                        "Save a Snowflake connection (or set connection to its display name, "
                        "e.g. SFSALESDB) then re-upload."
                    )
                    failed += 1
                    errors.append(f"Row {idx}: {sync_detail} (registry saved)")
                    updates.append(
                        {
                            "row": idx,
                            "connection": resolved["connection"],
                            "platform": platform,
                            "database": db_name,
                            "schema": schema,
                            "table": table,
                            "column": column,
                            "registry": True,
                            "source_synced": False,
                            "note": sync_detail,
                        }
                    )
                    continue
                try:
                    r_db, r_schema, r_table, r_col = _sync_snowflake_column(
                        connector_id=str(connector_id),
                        sf_cache=sf_cache,
                        database=db_name,
                        schema=schema,
                        table=table,
                        column=column,
                        meta=meta,
                        table_comments_done=sf_table_comments,
                    )
                    database = r_db
                    resolved_schema, resolved_table, resolved_column = r_schema, r_table, r_col
                    source_synced += 1
                    sync_status = "snowflake_comment"
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(
                        f"Row {idx}: Snowflake sync failed — "
                        f"{db_name}.{schema}.{table}.{column}: {exc} "
                        f"(registry saved)"
                    )
                    updates.append(
                        {
                            "row": idx,
                            "connection": resolved["connection"],
                            "platform": platform,
                            "database": db_name,
                            "schema": schema,
                            "table": table,
                            "column": column,
                            "registry": True,
                            "source_synced": False,
                        }
                    )
                    continue

            else:
                if not resolved.get("resolved"):
                    sync_detail = (
                        f"Connection '{resolved['connection']}' not found in "
                        "connector_dtls — saved under spreadsheet name. "
                        "Use the Connectors display name (or platform=snowflake/postgres) "
                        "for source sync."
                    )

            updated += 1
            item = {
                "row": idx,
                "connection": resolved["connection"],
                "platform": platform,
                "database": database,
                "schema": resolved_schema,
                "table": resolved_table,
                "column": resolved_column,
                "registry": True,
                "source_synced": sync_status in {"postgres_comment", "snowflake_comment"},
                "sync": sync_status,
            }
            if sync_detail:
                item["note"] = sync_detail
            updates.append(item)
    finally:
        for conn in pg_cache.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        for entry in sf_cache.values():
            try:
                entry["cur"].close()
            except Exception:  # noqa: BLE001
                pass
            try:
                entry["conn"].close()
            except Exception:  # noqa: BLE001
                pass

    return {
        "rows_total": len(rows),
        "updated": updated,
        "registry_updated": registry_updated,
        "source_synced": source_synced,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:50],
        "updates": updates[:100],
        "platforms": sorted(platforms_seen),
        "collection": asset_glossary_collection_name(),
    }
