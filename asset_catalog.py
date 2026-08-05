"""Multi-connector asset catalog for the Assets tab.

Builds a unified view from:
  - env Postgres (local-postgres sentinel)
  - connector_dtls (saved AWS / Azure / GCP / Snowflake / upload / …)
  - asset_glossary terms keyed by connection

Access rule (privilege):
  - role admin (or user Admin/admin) → all connectors
  - otherwise → connectors owned by the current user (+ local Postgres)
"""
from __future__ import annotations

import logging
import re
from typing import Any

import mongo_store
import postgres_store
import snowflake_catalog

_log = logging.getLogger("datahive.asset_catalog")

LOCAL_POSTGRES_ID = "local-postgres"

_SCOPE_SPLIT = re.compile(r"[,;\n]+")
_QUALIFIED = re.compile(
    r"^(?:(?P<db>[\w.\-]+)[./])?(?P<schema>[\w.\-]+)[./](?P<table>[\w.\-]+)$"
)


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def user_is_admin(user: str, role: str | None = None) -> bool:
    role_l = _norm(role).lower()
    if role_l == "admin":
        return True
    user_l = _norm(user).lower()
    return user_l in {"admin", "administrator", "root", "system"}


def list_accessible_connectors(user: str, role: str | None = None) -> list[dict[str, Any]]:
    """Connectors the user may browse, plus the local Postgres catalog entry."""
    admin = user_is_admin(user, role)
    items: list[dict[str, Any]] = [
        {
            "id": LOCAL_POSTGRES_ID,
            "display_name": "Local Postgres",
            "cloud": "postgres",
            "platform": "postgres",
            "connector_type": "database",
            "mode": "local",
            "user": "system",
            "apis": [],
            "dataset_scope": postgres_store.postgres_database_name(),
            "privileged": True,
            "browsable": True,
            "structure_supported": True,
        }
    ]

    try:
        coll = mongo_store.connectors_collection()
        cursor = coll.find(
            {},
            {
                "display_name": 1,
                "cloud": 1,
                "connector_type": 1,
                "mode": 1,
                "user": 1,
                "apis": 1,
                "dataset_scope": 1,
                "region": 1,
                "account_id": 1,
                "file_name": 1,
                "upload_relative_path": 1,
                "saved_at": 1,
                "connection_status": 1,
            },
        ).sort([("saved_at", -1), ("_id", -1)])
    except Exception as exc:  # noqa: BLE001
        _log.warning("connector list failed: %s", exc)
        return items

    user_l = _norm(user).lower()
    seen_names = {"local postgres", "postgres"}
    for doc in cursor:
        owner = _norm(str(doc.get("user") or "")).lower()
        if not admin and owner and owner not in {user_l, "unknown", "system"}:
            # Allow shared connectors with blank owner; restrict others.
            continue
        cloud = _norm(str(doc.get("cloud") or doc.get("connector_type") or "unknown")).lower()
        platform = "postgres" if cloud in {"postgres", "postgresql", "pg"} else cloud
        display = doc.get("display_name") or f"{cloud} connection"
        seen_names.add(_norm(display).lower())
        items.append(
            {
                "id": str(doc["_id"]),
                "display_name": display,
                "cloud": cloud,
                "platform": platform,
                "connector_type": doc.get("connector_type"),
                "mode": doc.get("mode") or "cloud",
                "user": doc.get("user") or "unknown",
                "apis": list(doc.get("apis") or []),
                "dataset_scope": doc.get("dataset_scope") or "",
                "region": doc.get("region"),
                "account_id": doc.get("account_id"),
                "file_name": doc.get("file_name"),
                "upload_relative_path": doc.get("upload_relative_path"),
                "saved_at": doc.get("saved_at"),
                "connection_status": doc.get("connection_status"),
                "privileged": admin or owner in {user_l, "", "unknown", "system"},
                "browsable": True,
                "structure_supported": platform in {"postgres", "snowflake"},
            }
        )

    # Surface connections that already have glossary terms but no connector_dtls row yet.
    try:
        gloss = mongo_store.asset_glossary_collection()
        for row in gloss.aggregate(
            [
                {
                    "$group": {
                        "_id": {
                            "connection": "$connection",
                            "platform": "$platform",
                        }
                    }
                }
            ]
        ):
            key = row.get("_id") or {}
            name = _norm(str(key.get("connection") or ""))
            if not name or name.lower() in seen_names:
                continue
            if name.lower() in {"postgres", "local postgres", "postgresql"}:
                continue
            platform = _norm(str(key.get("platform") or "unknown")).lower() or "unknown"
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "conn"
            items.append(
                {
                    "id": f"glossary:{slug}",
                    "display_name": name,
                    "cloud": platform,
                    "platform": platform,
                    "connector_type": "glossary",
                    "mode": "registry",
                    "user": "glossary",
                    "apis": [],
                    "dataset_scope": "",
                    "privileged": True,
                    "browsable": True,
                    "structure_supported": platform in {"postgres", "snowflake"},
                }
            )
            seen_names.add(name.lower())
    except Exception as exc:  # noqa: BLE001
        _log.warning("glossary connector synthesis failed: %s", exc)

    return items


def _snowflake_live_assets(connector: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Fetch live Snowflake assets. Returns (assets, schemas, error_note)."""
    try:
        assets, schemas = snowflake_catalog.catalog_assets_for_connector(connector["id"])
        return assets, schemas, None
    except Exception as exc:  # noqa: BLE001
        _log.warning("snowflake catalog failed for %s: %s", connector.get("id"), exc)
        return [], [], str(exc)


def _connector_by_id(connectors: list[dict[str, Any]], connector_id: str | None) -> dict[str, Any] | None:
    if not connector_id or connector_id == "all":
        return None
    for c in connectors:
        if c["id"] == connector_id:
            return c
    return None


def _parse_scope_assets(connector: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn dataset_scope / apis / upload file into browseable asset stubs."""
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    scope = _norm(str(connector.get("dataset_scope") or ""))
    for part in _SCOPE_SPLIT.split(scope):
        token = part.strip()
        if not token:
            continue
        m = _QUALIFIED.match(token.replace(":", "."))
        if m:
            schema = m.group("schema")
            table = m.group("table")
            database = m.group("db") or ""
            key = f"{database}|{schema}|{table}".lower()
            if key in seen:
                continue
            seen.add(key)
            assets.append(
                {
                    "name": table,
                    "type": "Table",
                    "schema": schema,
                    "database": database,
                    "crumb": ".".join(p for p in (database, schema, table) if p),
                    "source": "dataset_scope",
                }
            )
        else:
            key = f"scope|{token.lower()}"
            if key in seen:
                continue
            seen.add(key)
            assets.append(
                {
                    "name": token,
                    "type": "Scope",
                    "schema": token,
                    "database": "",
                    "crumb": token,
                    "source": "dataset_scope",
                }
            )

    for api in connector.get("apis") or []:
        name = _norm(str(api))
        if not name:
            continue
        key = f"api|{name.lower()}"
        if key in seen:
            continue
        seen.add(key)
        assets.append(
            {
                "name": name,
                "type": "API",
                "schema": "apis",
                "database": "",
                "crumb": f"apis / {name}",
                "source": "apis",
            }
        )

    if connector.get("file_name") or connector.get("upload_relative_path"):
        fname = connector.get("file_name") or connector.get("upload_relative_path")
        key = f"file|{str(fname).lower()}"
        if key not in seen:
            assets.append(
                {
                    "name": str(fname),
                    "type": "File",
                    "schema": "uploads",
                    "database": "",
                    "crumb": str(connector.get("upload_relative_path") or fname),
                    "source": "upload",
                }
            )
    return assets


def _glossary_assets_for_connection(connection_name: str) -> list[dict[str, Any]]:
    """Group asset_glossary terms into table-level assets for a connection."""
    try:
        coll = mongo_store.asset_glossary_collection()
        cursor = coll.find(
            {
                "$or": [
                    {"connection_key": _norm(connection_name).lower()},
                    {"connection": {"$regex": f"^{re.escape(connection_name)}$", "$options": "i"}},
                ]
            }
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("glossary assets lookup failed: %s", exc)
        return []

    tables: dict[str, dict[str, Any]] = {}
    for doc in cursor:
        database = str(doc.get("database") or "")
        schema = str(doc.get("schema") or "")
        table = str(doc.get("table") or "")
        column = str(doc.get("column") or "")
        if not schema or not table:
            continue
        key = f"{database}|{schema}|{table}".lower()
        entry = tables.setdefault(
            key,
            {
                "name": table,
                "type": "Table",
                "schema": schema,
                "database": database,
                "crumb": ".".join(p for p in (database, schema, table) if p),
                "source": "asset_glossary",
                "columns": [],
            },
        )
        if column:
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            entry["columns"].append(
                {
                    "name": column,
                    "business_name": doc.get("business_name") or meta.get("business_name"),
                    "description": doc.get("description") or meta.get("description"),
                    "classification": doc.get("classification") or meta.get("classification"),
                    "sensitivity": doc.get("sensitivity") or meta.get("sensitivity"),
                    "metadata": {
                        **meta,
                        **{
                            k: doc[k]
                            for k in (
                                "business_name",
                                "description",
                                "business_definition",
                                "classification",
                                "sensitivity",
                                "source_system",
                                "owner",
                                "steward",
                            )
                            if doc.get(k)
                        },
                    },
                }
            )
    return list(tables.values())


def _postgres_catalog_assets() -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    try:
        schemas = postgres_store.list_schemas()
        counts = postgres_store.catalog_counts()
        raw = postgres_store._catalog_assets()  # noqa: SLF001 — shared catalog builder
    except Exception as exc:  # noqa: BLE001
        _log.warning("postgres catalog failed: %s", exc)
        return [], {"Table": 0, "View": 0, "All": 0}, []

    assets = []
    for item in raw:
        assets.append(
            {
                "name": item.get("name"),
                "type": item.get("type") or "Table",
                "schema": item.get("schema") or (item.get("crumb") or "").split(".")[0],
                "database": postgres_store.postgres_database_name(),
                "crumb": item.get("crumb")
                or f"{item.get('schema')}.{item.get('name')}",
                "source": "postgres",
            }
        )
    return assets, counts, schemas


def _annotate(
    assets: list[dict[str, Any]], connector: dict[str, Any]
) -> list[dict[str, Any]]:
    out = []
    for a in assets:
        item = dict(a)
        item["connector_id"] = connector["id"]
        item["connector_name"] = connector.get("display_name")
        item["platform"] = connector.get("platform") or connector.get("cloud")
        item["cloud"] = connector.get("cloud")
        item["structure_supported"] = bool(connector.get("structure_supported"))
        out.append(item)
    return out


def build_catalog(
    user: str,
    *,
    role: str | None = None,
    connector_id: str | None = None,
) -> dict[str, Any]:
    """
    Unified asset catalog.
    connector_id: omitted / 'all' → every accessible connector; else one connector.
    """
    connectors = list_accessible_connectors(user, role)
    selected = _connector_by_id(connectors, connector_id)
    if connector_id and connector_id != "all" and selected is None:
        raise ValueError(f"Connector not found or not permitted: {connector_id}")

    targets = [selected] if selected else connectors
    all_assets: list[dict[str, Any]] = []
    schema_set: set[str] = set()
    counts = {"Table": 0, "View": 0, "API": 0, "File": 0, "Scope": 0, "Schema": 0, "All": 0}

    for conn in targets:
        if conn["id"] == LOCAL_POSTGRES_ID:
            pg_assets, pg_counts, pg_schemas = _postgres_catalog_assets()
            annotated = _annotate(pg_assets, conn)
            all_assets.extend(annotated)
            schema_set.update(pg_schemas)
            for key in ("Table", "View", "All"):
                counts[key] = counts.get(key, 0) + int(pg_counts.get(key) or 0)
            continue

        platform = str(conn.get("platform") or conn.get("cloud") or "").lower()
        gloss = _glossary_assets_for_connection(str(conn.get("display_name") or ""))
        live_assets: list[dict[str, Any]] = []
        live_schemas: list[str] = []
        live_error: str | None = None

        if platform == "snowflake" and not str(conn.get("id", "")).startswith("glossary:"):
            live_assets, live_schemas, live_error = _snowflake_live_assets(conn)
            schema_set.update(live_schemas)

        # Prefer live Snowflake metadata. Only fall back to scope/API stubs if live fetch failed.
        live_ok = platform == "snowflake" and not live_error
        scoped = [] if live_ok else _parse_scope_assets(conn)
        by_crumb = {a["crumb"].lower(): a for a in scoped if a.get("crumb")}
        for a in live_assets:
            if a.get("crumb"):
                by_crumb[a["crumb"].lower()] = a
        for g in gloss:
            # Glossary enriches / overrides when present.
            by_crumb[g["crumb"].lower()] = g
        combined = list(by_crumb.values())
        annotated = _annotate(combined, conn)
        for a in annotated:
            # Empty Snowflake schemas are browseable but have no column structure.
            if a.get("type") == "Schema" or a.get("empty"):
                a["structure_supported"] = False
        if live_error and platform == "snowflake" and not live_assets:
            # Surface a single diagnostic asset so the UI is not silently empty.
            annotated.append(
                {
                    "name": "snowflake_catalog_error",
                    "type": "Scope",
                    "schema": str(conn.get("dataset_scope") or "snowflake"),
                    "database": "",
                    "crumb": "snowflake / catalog_error",
                    "source": "snowflake_error",
                    "connector_id": conn["id"],
                    "connector_name": conn.get("display_name"),
                    "platform": "snowflake",
                    "cloud": conn.get("cloud"),
                    "structure_supported": False,
                    "error": live_error,
                }
            )
            schema_set.add(str(conn.get("dataset_scope") or "snowflake"))
        all_assets.extend(annotated)
        for a in annotated:
            if a.get("schema"):
                schema_set.add(str(a["schema"]))
            t = str(a.get("type") or "Table")
            counts[t] = counts.get(t, 0) + 1
            counts["All"] = counts.get("All", 0) + 1

    # Stable sort: platform, connector, schema, name
    all_assets.sort(
        key=lambda a: (
            str(a.get("platform") or ""),
            str(a.get("connector_name") or ""),
            str(a.get("schema") or ""),
            str(a.get("name") or ""),
        )
    )

    return {
        "connectors": [
            {
                "id": c["id"],
                "display_name": c["display_name"],
                "cloud": c.get("cloud"),
                "platform": c.get("platform"),
                "mode": c.get("mode"),
                "user": c.get("user"),
                "structure_supported": c.get("structure_supported"),
                "dataset_scope": c.get("dataset_scope"),
            }
            for c in connectors
        ],
        "selected_connector_id": selected["id"] if selected else "all",
        "items": all_assets,
        "schemas": sorted(schema_set, key=lambda s: s.lower()),
        "counts": counts,
        "connector_count": len(connectors),
        "asset_count": len(all_assets),
        "admin": user_is_admin(user, role),
    }


def connector_structure(
    user: str,
    *,
    role: str | None = None,
    connector_id: str,
    schema: str,
    table: str,
) -> dict[str, Any]:
    """Structure for an asset — Postgres live columns, or glossary columns for others."""
    connectors = list_accessible_connectors(user, role)
    conn = _connector_by_id(connectors, connector_id)
    if conn is None:
        raise ValueError(f"Connector not found or not permitted: {connector_id}")

    if conn["id"] == LOCAL_POSTGRES_ID or conn.get("platform") == "postgres":
        structure = postgres_store.table_structure(schema, table)
        structure["connector_id"] = conn["id"]
        structure["connector_name"] = conn["display_name"]
        structure["platform"] = "postgres"
        return structure

    platform = str(conn.get("platform") or conn.get("cloud") or "").lower()
    if platform == "snowflake" and not str(conn.get("id", "")).startswith("glossary:"):
        try:
            structure = snowflake_catalog.table_structure_for_connector(
                conn["id"], schema, table
            )
            structure["connector_id"] = conn["id"]
            structure["connector_name"] = conn["display_name"]
            return structure
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "snowflake structure failed for %s.%s on %s: %s",
                schema,
                table,
                conn.get("id"),
                exc,
            )
            raise ValueError(f"Snowflake structure fetch failed: {exc}") from exc

    gloss = _glossary_assets_for_connection(str(conn.get("display_name") or ""))
    match = None
    for a in gloss:
        if (
            str(a.get("schema") or "").lower() == schema.lower()
            and str(a.get("name") or "").lower() == table.lower()
        ):
            match = a
            break
    columns = []
    if match:
        for i, col in enumerate(match.get("columns") or [], start=1):
            columns.append(
                {
                    "position": i,
                    "name": col.get("name"),
                    "type": "—",
                    "nullable": True,
                    "default": None,
                    "primary_key": False,
                    "comment": None,
                    "metadata": col.get("metadata") or {
                        k: col.get(k)
                        for k in (
                            "business_name",
                            "description",
                            "classification",
                            "sensitivity",
                        )
                        if col.get(k)
                    },
                }
            )
    return {
        "schema": schema,
        "table": table,
        "table_type": "Table",
        "comment": None,
        "metadata": None,
        "columns": columns,
        "connector_id": conn["id"],
        "connector_name": conn["display_name"],
        "platform": conn.get("platform"),
        "structure_source": "asset_glossary" if columns else "empty",
        "note": None
        if columns
        else "No glossary columns registered for this asset yet. Upload a glossary row for this connection.",
    }
