"""Live Snowflake metadata catalog for Assets (schemas / tables / columns)."""
from __future__ import annotations

import logging
import re
from typing import Any

import mongo_store

_log = logging.getLogger("datahive.snowflake_catalog")


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _parse_account(account_id: str, region: str) -> str:
    account = account_id.strip()
    if "." in account or "-" in account:
        return account
    region = region.strip()
    return f"{account}.{region}" if region else account


def _load_private_key_bytes(pem: str, passphrase: str | None) -> bytes:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    password = passphrase.encode("utf-8") if passphrase else None
    key = serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=password,
        backend=default_backend(),
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def parse_scope_options(scope: str) -> dict[str, str]:
    """Parse dataset_scope into warehouse/database/schema/role hints."""
    out: dict[str, str] = {}
    for part in re.split(r"[;\n,]", scope or ""):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            key_u = key.strip().upper()
            val = val.strip()
            if key_u in {"WAREHOUSE", "WH"} and val:
                out["warehouse"] = val
            elif key_u in {"DATABASE", "DB"} and val:
                out["database"] = val
            elif key_u == "SCHEMA" and val:
                out["schema"] = val
            elif key_u == "ROLE" and val:
                out["role"] = val
            continue
        if part.count(".") >= 2:
            # DB.SCHEMA.TABLE → keep db/schema filters
            bits = [b for b in part.split(".") if b]
            if len(bits) >= 2:
                out.setdefault("database", bits[0])
                out.setdefault("schema", bits[1])
            continue
        if part.count(".") == 1:
            db, _, sch = part.partition(".")
            out.setdefault("database", db)
            out.setdefault("schema", sch)
            continue
        if not part.startswith("@"):
            out.setdefault("database", part)
    return out


def connect_kwargs_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Build snowflake.connector.connect kwargs from an unsealed connector doc."""
    account_id = _norm(doc.get("account_id"))
    if not account_id:
        raise ValueError("Snowflake account identifier is missing on this connector.")
    region = _norm(doc.get("region"))
    auth_type = _norm(doc.get("auth_type")).lower() or "password"
    user = _norm(doc.get("access_key_id") or doc.get("username") or doc.get("user"))
    password = _norm(doc.get("secret_access_key") or doc.get("password"))
    private_key_pem = _norm(doc.get("service_account_json") or doc.get("private_key"))
    scope_opts = parse_scope_options(_norm(doc.get("dataset_scope")))

    kwargs: dict[str, Any] = {
        "account": _parse_account(account_id, region),
        "user": user or None,
        "login_timeout": 30,
        "network_timeout": 45,
        "client_session_keep_alive": False,
    }
    for key in ("warehouse", "database", "schema", "role"):
        if scope_opts.get(key):
            kwargs[key] = scope_opts[key]

    if auth_type in {"password", "access_keys"}:
        if not user or not password:
            raise ValueError("Snowflake username/password credentials are incomplete.")
        kwargs["password"] = password
    elif auth_type == "key_pair":
        if not user or not private_key_pem:
            raise ValueError("Snowflake key-pair credentials are incomplete.")
        kwargs["private_key"] = _load_private_key_bytes(private_key_pem, password or None)
    elif auth_type == "oauth2":
        token = _norm(doc.get("api_key") or doc.get("access_token"))
        if not token:
            raise ValueError("Snowflake OAuth access token is missing.")
        kwargs.pop("user", None)
        kwargs["authenticator"] = "oauth"
        kwargs["token"] = token
    else:
        raise ValueError(f"Unsupported Snowflake auth type '{auth_type}'.")

    return {k: v for k, v in kwargs.items() if v is not None}


def open_connection(doc: dict[str, Any]):
    import snowflake.connector as sf

    return sf.connect(**connect_kwargs_from_doc(doc))


def load_connector_doc(connector_id: str) -> dict[str, Any]:
    doc = mongo_store.get_connector_document(connector_id, with_secrets=True)
    if not doc:
        raise LookupError(f"Connector not found: {connector_id}")
    return doc


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _fetch_all(cur) -> list[tuple]:
    try:
        return list(cur.fetchall() or [])
    except Exception:  # noqa: BLE001
        return []


def _show_result_name(row: tuple | list, fallback_idx: int = 1) -> str:
    """SHOW commands return name in different positions depending on driver/version."""
    if not row:
        return ""
    # Prefer second column (common for SHOW SCHEMAS / SHOW TABLES)
    for idx in (fallback_idx, 0, 2):
        if idx < len(row) and row[idx] not in (None, ""):
            return str(row[idx])
    return str(row[0])


def list_databases(conn, preferred: str | None = None) -> list[str]:
    with conn.cursor() as cur:
        if preferred:
            return [preferred]
        cur.execute("SHOW DATABASES")
        rows = _fetch_all(cur)
    names = []
    for row in rows:
        name = _show_result_name(row, 1)
        if name and name.upper() not in {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"}:
            names.append(name)
    return names


def list_schemas_in_database(conn, database: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW SCHEMAS IN DATABASE {_ident(database)}")
        rows = _fetch_all(cur)
    out = []
    for row in rows:
        name = _show_result_name(row, 1)
        if not name or name.upper() in {"INFORMATION_SCHEMA"}:
            continue
        out.append(name)
    return out


def list_tables_in_schema(conn, database: str, schema: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    with conn.cursor() as cur:
        cur.execute(f"SHOW TABLES IN SCHEMA {_ident(database)}.{_ident(schema)}")
        for row in _fetch_all(cur):
            name = _show_result_name(row, 1)
            if name:
                items.append({"name": name, "type": "Table"})
        try:
            cur.execute(f"SHOW VIEWS IN SCHEMA {_ident(database)}.{_ident(schema)}")
            for row in _fetch_all(cur):
                name = _show_result_name(row, 1)
                if name:
                    items.append({"name": name, "type": "View"})
        except Exception as exc:  # noqa: BLE001
            _log.debug("SHOW VIEWS skipped for %s.%s: %s", database, schema, exc)
    return items


def catalog_assets_for_doc(doc: dict[str, Any], *, max_tables: int = 500) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Discover tables/views from Snowflake.
    Returns (assets, schema_labels) where schema_labels are DATABASE.SCHEMA.
    """
    scope_opts = parse_scope_options(_norm(doc.get("dataset_scope")))
    preferred_db = scope_opts.get("database")
    preferred_schema = scope_opts.get("schema")

    conn = None
    assets: list[dict[str, Any]] = []
    schema_labels: list[str] = []
    try:
        conn = open_connection(doc)
        databases = list_databases(conn, preferred_db)
        if not databases and preferred_db:
            databases = [preferred_db]
        for database in databases:
            schemas = list_schemas_in_database(conn, database)
            if preferred_schema:
                schemas = [s for s in schemas if s.lower() == preferred_schema.lower()] or [
                    preferred_schema
                ]
            for schema in schemas:
                label = f"{database}.{schema}"
                schema_labels.append(label)
                objects = list_tables_in_schema(conn, database, schema)
                if not objects:
                    # Keep empty schemas visible so Assets can show them instead of a stub.
                    assets.append(
                        {
                            "name": schema,
                            "type": "Schema",
                            "schema": label,
                            "database": database,
                            "snowflake_schema": schema,
                            "crumb": label,
                            "source": "snowflake",
                            "empty": True,
                            "note": f"No tables or views found in {label}.",
                        }
                    )
                    continue
                for obj in objects:
                    assets.append(
                        {
                            "name": obj["name"],
                            "type": obj["type"],
                            "schema": label,
                            "database": database,
                            "snowflake_schema": schema,
                            "crumb": f"{database}.{schema}.{obj['name']}",
                            "source": "snowflake",
                        }
                    )
                    if len(assets) >= max_tables:
                        return assets, schema_labels
        return assets, schema_labels
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def catalog_assets_for_connector(
    connector_id: str, *, max_tables: int = 500
) -> tuple[list[dict[str, Any]], list[str]]:
    return catalog_assets_for_doc(load_connector_doc(connector_id), max_tables=max_tables)


def _split_schema_ref(schema: str, default_database: str | None = None) -> tuple[str, str]:
    """Accept 'DB.SCHEMA' or bare 'SCHEMA'."""
    raw = _norm(schema)
    if "." in raw:
        db, _, sch = raw.partition(".")
        return db, sch
    if default_database:
        return default_database, raw
    return "", raw


def table_structure_for_doc(doc: dict[str, Any], schema: str, table: str) -> dict[str, Any]:
    scope_opts = parse_scope_options(_norm(doc.get("dataset_scope")))
    database, sf_schema = _split_schema_ref(schema, scope_opts.get("database"))
    if not database:
        raise ValueError(
            "Snowflake database is unknown for this asset. "
            "Set dataset scope to DATABASE or DATABASE.SCHEMA on the connector."
        )
    if not sf_schema or not table:
        raise ValueError("Snowflake schema and table are required.")

    conn = None
    try:
        conn = open_connection(doc)
        # Ensure session context; then describe.
        with conn.cursor() as cur:
            cur.execute(f"USE DATABASE {_ident(database)}")
            cur.execute(f"USE SCHEMA {_ident(sf_schema)}")
            cur.execute(f"DESCRIBE TABLE {_ident(database)}.{_ident(sf_schema)}.{_ident(table)}")
            rows = _fetch_all(cur)
            # Also pull comments / nullability from information_schema when possible.
            info: dict[str, dict[str, Any]] = {}
            try:
                cur.execute(
                    """
                    SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, COMMENT, ORDINAL_POSITION
                    FROM IDENTIFIER(%s)
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                    """,
                    (f"{database}.INFORMATION_SCHEMA.COLUMNS", sf_schema, table),
                )
                # IDENTIFIER with bind can be flaky; fallback below.
            except Exception:  # noqa: BLE001
                try:
                    cur.execute(
                        f"""
                        SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, COMMENT, ORDINAL_POSITION
                        FROM {_ident(database)}.INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY ORDINAL_POSITION
                        """,
                        (sf_schema.upper(), table.upper()),
                    )
                    for r in _fetch_all(cur):
                        info[str(r[0]).upper()] = {
                            "nullable": str(r[1]).upper() == "YES",
                            "default": r[2],
                            "comment": r[3],
                            "position": r[4],
                        }
                except Exception as exc:  # noqa: BLE001
                    _log.debug("information_schema columns lookup failed: %s", exc)

        columns = []
        for i, row in enumerate(rows, start=1):
            # DESCRIBE TABLE: name, type, kind, null?, default, primary key, unique key, check, expression, comment, policy name, ...
            name = str(row[0]) if row else f"col_{i}"
            col_type = str(row[1]) if len(row) > 1 else "—"
            nullable = True
            if len(row) > 3 and row[3] is not None:
                nullable = str(row[3]).upper() in {"Y", "YES", "TRUE", "1"}
            default = row[4] if len(row) > 4 else None
            primary_key = False
            if len(row) > 5 and row[5] is not None:
                primary_key = str(row[5]).upper() in {"Y", "YES", "TRUE", "1"}
            comment = row[9] if len(row) > 9 else None
            meta = info.get(name.upper()) or {}
            columns.append(
                {
                    "position": int(meta.get("position") or i),
                    "name": name,
                    "type": col_type,
                    "nullable": meta.get("nullable", nullable),
                    "default": meta.get("default", default),
                    "primary_key": primary_key,
                    "comment": meta.get("comment", comment),
                    "metadata": None,
                }
            )

        return {
            "schema": f"{database}.{sf_schema}",
            "table": table,
            "database": database,
            "snowflake_schema": sf_schema,
            "table_type": "Table",
            "comment": None,
            "metadata": None,
            "columns": columns,
            "platform": "snowflake",
            "structure_source": "snowflake",
            "note": None if columns else "No columns returned by Snowflake DESCRIBE TABLE.",
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def table_structure_for_connector(connector_id: str, schema: str, table: str) -> dict[str, Any]:
    return table_structure_for_doc(load_connector_doc(connector_id), schema, table)
