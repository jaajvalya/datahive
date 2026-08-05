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


def _stage_fqn(database: str, schema: str, stage: str) -> str:
    stage_name = stage.lstrip("@")
    if stage_name.count(".") >= 2:
        return stage_name
    if database and schema:
        return f"{database}.{schema}.{stage_name}"
    if schema:
        return f"{schema}.{stage_name}"
    return stage_name


def list_stages_for_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """List stages visible to the connector (scoped database/schema preferred)."""
    scope_opts = parse_scope_options(_norm(doc.get("dataset_scope")))
    database = scope_opts.get("database") or "SALES_DB"
    schema = scope_opts.get("schema") or "RAW"
    conn = None
    stages: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        conn = open_connection(doc)
        with conn.cursor() as cur:
            try:
                cur.execute('USE WAREHOUSE "DEV_WH"')
            except Exception:  # noqa: BLE001
                pass
            queries = [
                f"SHOW STAGES IN SCHEMA {_ident(database)}.{_ident(schema)}",
                f"SHOW STAGES IN DATABASE {_ident(database)}",
                "SHOW STAGES",
            ]
            for q in queries:
                try:
                    cur.execute(q)
                except Exception:  # noqa: BLE001
                    continue
                cols = [d[0].lower() for d in (cur.description or [])]
                for row in _fetch_all(cur):
                    data = dict(zip(cols, row))
                    name = str(data.get("name") or _show_result_name(row, 1) or "").strip()
                    if not name:
                        continue
                    db = str(data.get("database_name") or database)
                    sch = str(data.get("schema_name") or schema)
                    fqn = _stage_fqn(db, sch, name)
                    key = fqn.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    stages.append(
                        {
                            "name": name,
                            "database": db,
                            "schema": sch,
                            "fqn": fqn,
                            "url": data.get("url") or "",
                            "type": data.get("type") or "INTERNAL",
                            "recommended": name.upper() == "RAW_STAGE",
                        }
                    )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # Always surface the example landing stage used by DataHive ELT into RAW.
    example_fqn = _stage_fqn(database, schema, "RAW_STAGE")
    if example_fqn.lower() not in seen:
        stages.insert(
            0,
            {
                "name": "RAW_STAGE",
                "database": database,
                "schema": schema,
                "fqn": example_fqn,
                "url": "",
                "type": "INTERNAL",
                "recommended": True,
                "exists": False,
                "note": (
                    "Recommended landing stage for files → RAW tables. "
                    "Requires CREATE STAGE on the schema (DATA_ENGINEER currently has USAGE only)."
                ),
            },
        )
    else:
        for s in stages:
            if s["fqn"].lower() == example_fqn.lower():
                s["exists"] = True
                s["recommended"] = True

    # User stage is always available without schema CREATE STAGE privilege.
    if "~" not in seen and "~/" not in seen:
        stages.append(
            {
                "name": "~",
                "database": "",
                "schema": "",
                "fqn": "~",
                "url": "",
                "type": "USER",
                "recommended": False,
                "exists": True,
                "note": "Personal user stage (@~). Usable without CREATE STAGE on SALES_DB.RAW.",
            }
        )

    for s in stages:
        s.setdefault("exists", True)
    return stages


def list_stage_files_for_doc(
    doc: dict[str, Any],
    stage_fqn: str,
    *,
    pattern: str = "",
) -> list[dict[str, Any]]:
    """LIST files for a Snowflake stage."""
    stage = stage_fqn.lstrip("@")
    conn = None
    try:
        conn = open_connection(doc)
        with conn.cursor() as cur:
            try:
                cur.execute('USE WAREHOUSE "DEV_WH"')
            except Exception:  # noqa: BLE001
                pass
            loc = f"@{stage}"
            if pattern:
                loc = f"{loc}/{pattern.lstrip('/')}"
            cur.execute(f"LIST {loc}")
            cols = [d[0].lower() for d in (cur.description or [])]
            files = []
            for row in _fetch_all(cur):
                data = dict(zip(cols, row)) if cols else {}
                name = str(data.get("name") or (row[0] if row else "") or "")
                if not name:
                    continue
                # LIST returns stage/path; keep relative path after stage root when possible.
                rel = name
                marker = stage.split(".")[-1].lower() + "/"
                lower = name.lower()
                if marker in lower:
                    rel = name[lower.index(marker) + len(marker) :]
                elif "/" in name:
                    rel = name.split("/", 1)[-1]
                size = data.get("size") if "size" in data else (row[1] if len(row) > 1 else None)
                md5 = data.get("md5") if "md5" in data else None
                last_modified = (
                    data.get("last_modified")
                    if "last_modified" in data
                    else (row[3] if len(row) > 3 else None)
                )
                ext = ""
                base = rel.rsplit("/", 1)[-1]
                if "." in base:
                    ext = base.rsplit(".", 1)[-1].lower()
                files.append(
                    {
                        "name": name,
                        "path": rel,
                        "size": size,
                        "md5": md5,
                        "last_modified": str(last_modified) if last_modified is not None else None,
                        "extension": ext,
                        "stage_fqn": stage,
                        "stage_location": f"@{stage}/{rel}",
                    }
                )
            return files
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def ensure_raw_stage_grant_sql(database: str = "SALES_DB", schema: str = "RAW") -> str:
    return (
        f"-- Run as ACCOUNTADMIN / SYSADMIN (or schema owner)\n"
        f"USE ROLE ACCOUNTADMIN;\n"
        f"USE DATABASE {database};\n"
        f"USE SCHEMA {schema};\n"
        f"CREATE STAGE IF NOT EXISTS RAW_STAGE\n"
        f"  DIRECTORY = (ENABLE = TRUE)\n"
        f"  COMMENT = 'DataHive landing stage for files loaded into {database}.{schema}';\n"
        f"GRANT USAGE ON DATABASE {database} TO ROLE DATA_ENGINEER;\n"
        f"GRANT USAGE ON SCHEMA {database}.{schema} TO ROLE DATA_ENGINEER;\n"
        f"GRANT CREATE STAGE ON SCHEMA {database}.{schema} TO ROLE DATA_ENGINEER;\n"
        f"GRANT READ, WRITE ON STAGE {database}.{schema}.RAW_STAGE TO ROLE DATA_ENGINEER;\n"
        f"-- Optional if Ensure should keep working for this role:\n"
        f"-- GRANT CREATE STAGE ON SCHEMA {database}.{schema} TO ROLE DATA_ENGINEER;"
    )


def ensure_raw_stage_for_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Create SALES_DB.RAW.RAW_STAGE (or scoped equivalent) if the role allows it."""
    scope_opts = parse_scope_options(_norm(doc.get("dataset_scope")))
    database = scope_opts.get("database") or "SALES_DB"
    schema = scope_opts.get("schema") or "RAW"
    stage = "RAW_STAGE"
    fqn = _stage_fqn(database, schema, stage)
    grant_sql = ensure_raw_stage_grant_sql(database, schema)
    conn = None
    try:
        conn = open_connection(doc)
        with conn.cursor() as cur:
            try:
                cur.execute('USE WAREHOUSE "DEV_WH"')
            except Exception:  # noqa: BLE001
                pass
            cur.execute(f"USE DATABASE {_ident(database)}")
            cur.execute(f"USE SCHEMA {_ident(schema)}")
            cur.execute(
                f"""
                CREATE STAGE IF NOT EXISTS {_ident(stage)}
                DIRECTORY = (ENABLE = TRUE)
                COMMENT = 'DataHive landing stage for files loaded into {database}.{schema}'
                """
            )
        return {
            "ok": True,
            "created_or_exists": True,
            "fqn": fqn,
            "database": database,
            "schema": schema,
            "name": stage,
        }
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "Insufficient privileges" in msg or "42501" in msg or "CREATE STAGE" in msg:
            raise PermissionError(
                "Role DATA_ENGINEER can USE schema "
                f"{database}.{schema} but cannot CREATE STAGE. "
                "Ask an admin to run:\n\n"
                + grant_sql
            ) from exc
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def suggest_table_name_from_file(path: str) -> str:
    base = (path or "stage_file").rsplit("/", 1)[-1]
    base = re.sub(r"\.(csv|tsv|json|jsonl|parquet|gz|zip|xml)$", "", base, flags=re.I)
    base = re.sub(r"\.(csv|tsv|json|jsonl|parquet)$", "", base, flags=re.I)
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_")
    if not safe:
        safe = "STAGE_FILE"
    if safe[0].isdigit():
        safe = "T_" + safe
    return safe.upper()


def file_format_for_extension(ext: str) -> dict[str, str]:
    e = (ext or "").lower().lstrip(".")
    if e in {"parquet"}:
        return {
            "name": "DH_PARQUET_FF",
            "ddl": "TYPE = PARQUET",
            "copy_options": "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE",
        }
    if e in {"json", "jsonl", "ndjson"}:
        return {
            "name": "DH_JSON_FF",
            "ddl": "TYPE = JSON STRIP_OUTER_ARRAY = TRUE",
            "copy_options": "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE",
        }
    if e in {"tsv"}:
        return {
            "name": "DH_TSV_FF",
            "ddl": "TYPE = CSV FIELD_DELIMITER = '\\t' SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' NULL_IF = ('', 'NULL')",
            "copy_options": "",
        }
    # default CSV
    return {
        "name": "DH_CSV_FF",
        "ddl": "TYPE = CSV FIELD_DELIMITER = ',' SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' NULL_IF = ('', 'NULL') ERROR_ON_COLUMN_COUNT_MISMATCH = FALSE",
        "copy_options": "",
    }
