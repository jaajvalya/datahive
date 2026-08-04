"""Unified glossary apply across all connectors (AWS, Azure, GCP, Snowflake, Postgres, …).

Canonical store: MongoDB `asset_glossary` (MONGO_URI / MONGO_ASSET_GLOSSARY_COLLECTION).
Postgres column comments are an optional source sync when the connection resolves to Postgres.
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
}


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


def resolve_connection(connection: str, platform_hint: str = "") -> dict[str, Any]:
    """
    Map spreadsheet `connection` to a connector_dtls document when possible.
    Falls back to Postgres when blank / local, or uses platform_hint for unknown names.
    """
    name = _norm(connection)
    hint = _normalize_platform(platform_hint)

    if not name or _norm_key(name) in POSTGRES_PLATFORMS:
        return {
            "connection": name or "postgres",
            "connection_key": _norm_key(name or "postgres"),
            "platform": "postgres",
            "connector_id": None,
            "display_name": name or "postgres",
            "resolved": True,
            "source": "default_postgres",
        }

    try:
        matched = mongo_store.find_connector_by_name(name)
    except RuntimeError as exc:
        _log.warning("connector lookup failed for %r: %s", name, exc)
        matched = None

    if matched:
        cloud = _normalize_platform(
            str(matched.get("cloud") or matched.get("connector_type") or hint or "")
        )
        if not cloud:
            cloud = "unknown"
        if cloud in POSTGRES_PLATFORMS:
            cloud = "postgres"
        return {
            "connection": matched.get("display_name") or name,
            "connection_key": _norm_key(str(matched.get("display_name") or name)),
            "platform": cloud,
            "connector_id": matched.get("id"),
            "display_name": matched.get("display_name") or name,
            "resolved": True,
            "source": "connector_dtls",
            "cloud": matched.get("cloud"),
            "mode": matched.get("mode"),
        }

    platform = hint or "unknown"
    if platform in POSTGRES_PLATFORMS:
        platform = "postgres"
    return {
        "connection": name,
        "connection_key": _norm_key(name),
        "platform": platform,
        "connector_id": None,
        "display_name": name,
        "resolved": False,
        "source": "spreadsheet",
    }


def _is_postgres_platform(platform: str) -> bool:
    return _normalize_platform(platform) in POSTGRES_PLATFORMS or platform == "postgres"


def apply_glossary_file(path: Path | str) -> dict[str, Any]:
    """
    Apply glossary rows to the unified asset_glossary registry for every connector.

    - Always upserts business metadata into Mongo `asset_glossary`
    - When connection resolves to Postgres, also writes COMMENT ON COLUMN
    - Cloud connectors (AWS/Azure/GCP/Snowflake/…) are registry-backed the same way
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
            else:
                if not resolved.get("resolved"):
                    sync_detail = (
                        f"Connection '{resolved['connection']}' not found in "
                        "connector_dtls — saved under spreadsheet name. "
                        "Use the Connectors display name for tighter matching."
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
                "source_synced": sync_status == "postgres_comment",
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
