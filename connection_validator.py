"""Live connection validation for DataHive connector credentials.

Called before persisting a connector so invalid credentials fail fast.
Never logs secret values.
"""
from __future__ import annotations

import json
import re
from typing import Any


class ConnectionValidationError(Exception):
    """Raised when credentials cannot authenticate to the target system."""

    def __init__(self, message: str, *, platform: str = "", error_type: str = "auth"):
        super().__init__(message)
        self.platform = platform
        self.error_type = error_type


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _platform(payload: dict[str, Any]) -> str:
    raw = _norm(payload.get("cloud") or payload.get("connector_type") or payload.get("platform"))
    return raw.lower()


def _safe_error(exc: BaseException, *, fallback: str = "Connection failed") -> str:
    text = str(exc).strip() or fallback
    # Collapse multi-line driver dumps; keep first actionable sentence.
    text = re.sub(r"\s+", " ", text)
    if len(text) > 420:
        text = text[:417] + "..."
    return text


def validate_connector(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Attempt a real handshake with the target system.
    Returns a small success dict, or raises ConnectionValidationError / ValueError.
    """
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Empty connector payload.")

    platform = _platform(payload)
    mode = _norm(payload.get("mode")).lower()
    if mode == "upload" or platform in {"upload", "manualupload"}:
        return _validate_upload(payload)

    if platform in {"snowflake"}:
        return _validate_snowflake(payload)
    if platform in {"databricks", "dbx"}:
        return _validate_databricks(payload)
    if platform in {"sqlserver", "mssql"}:
        merged = dict(payload)
        merged["engine"] = "sqlserver"
        return _validate_rdbms(merged)
    if platform in {"mongodb", "mongo"}:
        return _validate_mongodb(payload)
    if platform in {"postgres", "postgresql", "pg", "local-postgres"}:
        # Dedicated PostgreSQL connector (host/port/db) vs local DataHive Postgres fallback.
        if _norm(payload.get("host") or payload.get("account_id")) or _norm(payload.get("database")):
            merged = dict(payload)
            merged["engine"] = "postgresql"
            return _validate_rdbms(merged)
        return _validate_postgres(payload)
    if platform in {"rdbms", "onprem", "on-prem", "database"}:
        return _validate_rdbms(payload)
    if platform in {"aws", "amazonwebservices"}:
        return _validate_aws(payload)
    if platform in {"gcp", "googlecloud"}:
        return _validate_gcp(payload)
    if platform in {"azure", "microsoftazure"}:
        return _validate_azure(payload)
    if platform in {"sharepoint", "microsoftsharepoint"}:
        return _validate_ms_graph(payload, kind="sharepoint")
    if platform in {"googledrive"}:
        return _validate_google_drive(payload)
    if platform in {"onedrive", "microsoftonedrive"}:
        return _validate_ms_graph(payload, kind="onedrive")

    raise ConnectionValidationError(
        f"Live validation is not configured for connector type '{platform or 'unknown'}'.",
        platform=platform or "unknown",
        error_type="unsupported",
    )


def _validate_upload(payload: dict[str, Any]) -> dict[str, Any]:
    name = _norm(payload.get("file_name"))
    if not name:
        raise ConnectionValidationError(
            "Upload file name is required.",
            platform="upload",
            error_type="validation",
        )
    return {
        "ok": True,
        "platform": "upload",
        "message": f"Upload ready for {name}",
        "details": {"file_name": name},
    }


def _parse_snowflake_account(account_id: str, region: str) -> str:
    account = account_id.strip()
    # Allow full locators like org-account or xy12345.us-east-1
    if "." in account or "-" in account:
        return account
    region = region.strip()
    if region:
        return f"{account}.{region}"
    return account


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


def _normalize_databricks_host(account_id: str) -> str:
    host = _norm(account_id).rstrip("/")
    if not host:
        raise ConnectionValidationError(
            "Databricks workspace URL is required.",
            platform="databricks",
            error_type="validation",
        )
    if not re.match(r"^https?://", host, flags=re.I):
        host = "https://" + host
    if not re.match(r"^https://[A-Za-z0-9._:-]+", host):
        raise ConnectionValidationError(
            "Databricks workspace URL looks invalid. "
            "Example: https://dbc-xxxxxxxx-xxxx.cloud.databricks.com",
            platform="databricks",
            error_type="validation",
        )
    return host


def _databricks_http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            body = ""
        detail = body.strip() or _safe_error(exc)
        if exc.code in {401, 403}:
            raise ConnectionValidationError(
                f"Databricks authentication failed ({exc.code}): {detail}",
                platform="databricks",
                error_type="auth",
            ) from exc
        raise ConnectionValidationError(
            f"Databricks API error ({exc.code}): {detail}",
            platform="databricks",
            error_type="api",
        ) from exc
    except urllib.error.URLError as exc:
        raise ConnectionValidationError(
            f"Could not reach Databricks workspace: {_safe_error(exc.reason if hasattr(exc, 'reason') else exc)}",
            platform="databricks",
            error_type="network",
        ) from exc


def _validate_databricks(payload: dict[str, Any]) -> dict[str, Any]:
    host = _normalize_databricks_host(
        _norm(payload.get("account_id") or payload.get("base_url") or payload.get("workspace_url"))
    )
    auth_type = _norm(payload.get("auth_type")).lower() or "api_key"
    token = ""

    if auth_type in {"api_key", "pat", "token", "personal_access_token"}:
        token = _norm(payload.get("api_key") or payload.get("token") or payload.get("access_token"))
        if not token:
            raise ConnectionValidationError(
                "Databricks personal access token is required.",
                platform="databricks",
                error_type="validation",
            )
    elif auth_type in {"oauth2", "service_principal"}:
        client_id = _norm(payload.get("client_id"))
        client_secret = _norm(payload.get("client_secret"))
        if not client_id or not client_secret:
            raise ConnectionValidationError(
                "Databricks service principal Client ID and Client secret are required.",
                platform="databricks",
                error_type="validation",
            )
        token_url = f"{host}/oidc/v1/token"
        body = (
            f"grant_type=client_credentials"
            f"&client_id={client_id}"
            f"&client_secret={client_secret}"
            f"&scope=all-apis"
        ).encode("utf-8")
        token_data = _databricks_http_json(
            token_url,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        token = _norm(token_data.get("access_token"))
        if not token:
            raise ConnectionValidationError(
                "Databricks OAuth token response did not include an access token.",
                platform="databricks",
                error_type="auth",
            )
    else:
        raise ConnectionValidationError(
            f"Unsupported Databricks auth type '{auth_type}'.",
            platform="databricks",
            error_type="validation",
        )

    # Lightweight authenticated probe — current user via SCIM.
    me = _databricks_http_json(
        f"{host}/api/2.0/preview/scim/v2/Me",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/scim+json, application/json",
        },
    )
    display = _norm(me.get("displayName") or me.get("userName") or me.get("id")) or "authenticated"
    return {
        "ok": True,
        "platform": "databricks",
        "message": f"Databricks credentials validated ({display})",
        "details": {
            "workspace_url": host,
            "auth_type": auth_type,
            "user": display,
            "region": _norm(payload.get("region")),
        },
    }


def _validate_snowflake(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        import snowflake.connector as sf
    except ImportError as exc:
        raise ConnectionValidationError(
            "Snowflake driver is not installed on the API server. "
            "Run: pip install snowflake-connector-python",
            platform="snowflake",
            error_type="dependency",
        ) from exc

    account_id = _norm(payload.get("account_id"))
    region = _norm(payload.get("region"))
    auth_type = _norm(payload.get("auth_type")).lower() or "password"
    user = _norm(payload.get("access_key_id") or payload.get("username") or payload.get("user"))
    password = _norm(payload.get("secret_access_key") or payload.get("password"))
    private_key_pem = _norm(payload.get("service_account_json") or payload.get("private_key"))
    client_id = _norm(payload.get("client_id"))
    client_secret = _norm(payload.get("client_secret"))
    scope = _norm(payload.get("dataset_scope"))

    if not account_id:
        raise ConnectionValidationError(
            "Snowflake account identifier is required.",
            platform="snowflake",
            error_type="validation",
        )
    if not user and auth_type != "oauth2":
        raise ConnectionValidationError(
            "Snowflake username is required.",
            platform="snowflake",
            error_type="validation",
        )

    account = _parse_snowflake_account(account_id, region)
    connect_kwargs: dict[str, Any] = {
        "account": account,
        "user": user or None,
        "login_timeout": 25,
        "network_timeout": 25,
        "client_session_keep_alive": False,
    }

    # Optional warehouse/database/role from scope: WAREHOUSE=...;DATABASE=...;ROLE=...
    # or DATABASE.SCHEMA pattern.
    for part in re.split(r"[;\n,]", scope):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            key_u = key.strip().upper()
            val = val.strip()
            if key_u in {"WAREHOUSE", "WH"} and val:
                connect_kwargs["warehouse"] = val
            elif key_u in {"DATABASE", "DB"} and val:
                connect_kwargs["database"] = val
            elif key_u == "SCHEMA" and val:
                connect_kwargs["schema"] = val
            elif key_u == "ROLE" and val:
                connect_kwargs["role"] = val
        elif part.count(".") == 1 and "warehouse" not in connect_kwargs:
            db, _, sch = part.partition(".")
            if db:
                connect_kwargs["database"] = db
            if sch:
                connect_kwargs["schema"] = sch
        elif part.upper().startswith("@") is False and "database" not in connect_kwargs:
            connect_kwargs.setdefault("database", part)

    if auth_type in {"password", "access_keys"}:
        if not password:
            raise ConnectionValidationError(
                "Snowflake password is required.",
                platform="snowflake",
                error_type="validation",
            )
        connect_kwargs["password"] = password
    elif auth_type == "key_pair":
        if not private_key_pem:
            raise ConnectionValidationError(
                "Snowflake private key (PEM) is required for key-pair auth.",
                platform="snowflake",
                error_type="validation",
            )
        try:
            connect_kwargs["private_key"] = _load_private_key_bytes(
                private_key_pem, password or None
            )
        except Exception as exc:  # noqa: BLE001
            raise ConnectionValidationError(
                f"Invalid Snowflake private key: {_safe_error(exc)}",
                platform="snowflake",
                error_type="validation",
            ) from exc
    elif auth_type == "oauth2":
        if not client_id or not client_secret:
            raise ConnectionValidationError(
                "Snowflake OAuth client ID and secret are required.",
                platform="snowflake",
                error_type="validation",
            )
        # Password grant / token exchange is tenant-specific; require an access token
        # when provided in api_key, otherwise fail with guidance.
        token = _norm(payload.get("api_key") or payload.get("access_token"))
        if not token:
            raise ConnectionValidationError(
                "Snowflake OAuth validation needs an access token (paste into API key) "
                "or use username/password or key-pair auth.",
                platform="snowflake",
                error_type="validation",
            )
        connect_kwargs.pop("user", None)
        connect_kwargs["authenticator"] = "oauth"
        connect_kwargs["token"] = token
    else:
        raise ConnectionValidationError(
            f"Unsupported Snowflake auth type '{auth_type}'.",
            platform="snowflake",
            error_type="validation",
        )

    conn = None
    try:
        conn = sf.connect(**{k: v for k, v in connect_kwargs.items() if v is not None})
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_USER()")
            row = cur.fetchone() or ("", "", "")
        return {
            "ok": True,
            "platform": "snowflake",
            "message": "Snowflake connection successful",
            "details": {
                "account": account,
                "user": user or None,
                "auth_type": auth_type,
                "snowflake_version": row[0],
                "current_account": row[1],
                "current_user": row[2],
                "database": connect_kwargs.get("database"),
                "warehouse": connect_kwargs.get("warehouse"),
            },
        }
    except ConnectionValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"Snowflake authentication failed: {_safe_error(exc)}",
            platform="snowflake",
            error_type="auth",
        ) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _validate_postgres(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        import psycopg
    except ImportError as exc:
        raise ConnectionValidationError(
            "psycopg is not installed on the API server.",
            platform="postgres",
            error_type="dependency",
        ) from exc

    # Prefer explicit form fields; fall back to local DataHive Postgres settings.
    host = _norm(payload.get("host") or payload.get("account_id"))
    port = _norm(payload.get("port") or "5432") or "5432"
    dbname = _norm(payload.get("database") or payload.get("dataset_scope")) or "postgres"
    user = _norm(payload.get("access_key_id") or payload.get("username") or payload.get("user"))
    password = _norm(payload.get("secret_access_key") or payload.get("password"))

    if not host:
        try:
            import postgres_store

            postgres_store.ping_postgres()
            return {
                "ok": True,
                "platform": "postgres",
                "message": "Local Postgres connection successful",
                "details": {"host": postgres_store.redacted_postgres_host()},
            }
        except Exception as exc:  # noqa: BLE001
            raise ConnectionValidationError(
                f"Postgres connection failed: {_safe_error(exc)}",
                platform="postgres",
                error_type="auth",
            ) from exc

    if not user:
        raise ConnectionValidationError(
            "Postgres username is required.",
            platform="postgres",
            error_type="validation",
        )

    try:
        with psycopg.connect(
            host=host,
            port=int(port) if str(port).isdigit() else 5432,
            dbname=dbname.split(",")[0].strip() or "postgres",
            user=user,
            password=password or None,
            connect_timeout=10,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
        return {
            "ok": True,
            "platform": "postgres",
            "message": "Postgres connection successful",
            "details": {"host": host, "database": dbname, "version": str(version)[:120]},
        }
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"Postgres authentication failed: {_safe_error(exc)}",
            platform="postgres",
            error_type="auth",
        ) from exc


def _validate_mongodb(payload: dict[str, Any]) -> dict[str, Any]:
    """Live-validate a MongoDB connection (on-prem / Atlas / self-hosted)."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError as exc:
        raise ConnectionValidationError(
            "pymongo is not installed. Run: pip install pymongo",
            platform="mongodb",
            error_type="dependency",
        ) from exc

    host = _norm(payload.get("host") or payload.get("account_id"))
    port_raw = _norm(payload.get("port") or "27017") or "27017"
    database = _norm(payload.get("database")) or "admin"
    user = _norm(payload.get("access_key_id") or payload.get("username") or payload.get("user"))
    password = _norm(payload.get("secret_access_key") or payload.get("password"))
    uri = _norm(payload.get("jdbc_url") or payload.get("connection_uri") or payload.get("mongodb_uri"))

    if uri:
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=8000)
            info = client.admin.command("ping")
            server = client.server_info()
            client.close()
            return {
                "ok": True,
                "platform": "mongodb",
                "message": "MongoDB connection successful",
                "details": {
                    "engine": "mongodb",
                    "via": "uri",
                    "version": str(server.get("version", ""))[:40],
                    "ping": info.get("ok"),
                },
            }
        except Exception as exc:  # noqa: BLE001
            raise ConnectionValidationError(
                f"MongoDB URI connection failed: {_safe_error(exc)}",
                platform="mongodb",
                error_type="auth",
            ) from exc

    if not host:
        raise ConnectionValidationError(
            "Host / IP is required for MongoDB.",
            platform="mongodb",
            error_type="validation",
        )
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConnectionValidationError(
            "Port must be a number.",
            platform="mongodb",
            error_type="validation",
        ) from exc

    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "serverSelectionTimeoutMS": 8000,
        "connectTimeoutMS": 8000,
    }
    if user:
        kwargs["username"] = user
        kwargs["password"] = password or ""
        kwargs["authSource"] = database

    try:
        client = MongoClient(**kwargs)
        info = client.admin.command("ping")
        server = client.server_info()
        # Touch the named database so authSource / privileges are exercised.
        _ = client[database].list_collection_names(max_time_ms=5000)
        client.close()
        return {
            "ok": True,
            "platform": "mongodb",
            "message": "MongoDB connection successful",
            "details": {
                "engine": "mongodb",
                "host": host,
                "port": port,
                "database": database,
                "version": str(server.get("version", ""))[:40],
                "ping": info.get("ok"),
            },
        }
    except PyMongoError as exc:
        raise ConnectionValidationError(
            f"MongoDB connection failed: {_safe_error(exc)}",
            platform="mongodb",
            error_type="auth",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"MongoDB connection failed: {_safe_error(exc)}",
            platform="mongodb",
            error_type="auth",
        ) from exc


_RDBMS_DEFAULT_PORTS = {
    "postgresql": 5432,
    "postgres": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "sqlserver": 1433,
    "mssql": 1433,
    "oracle": 1521,
    "db2": 50000,
    "mongodb": 27017,
}


def _rdbms_probe_sql(engine: str) -> str:
    if engine in {"oracle"}:
        return "SELECT banner FROM v$version WHERE ROWNUM = 1"
    if engine in {"sqlserver", "mssql"}:
        return "SELECT @@VERSION"
    if engine in {"db2"}:
        return "SELECT service_level FROM TABLE(sysproc.env_get_inst_info()) AS x"
    return "SELECT 1"


def _validate_rdbms(payload: dict[str, Any]) -> dict[str, Any]:
    """Live-validate an on-premises RDBMS connection (Postgres/MySQL/SQL Server/Oracle/…)."""
    engine = _norm(payload.get("engine") or payload.get("dialect") or "postgresql").lower()
    if engine in {"postgres", "pg"}:
        engine = "postgresql"
    if engine in {"mssql"}:
        engine = "sqlserver"

    host = _norm(payload.get("host") or payload.get("account_id"))
    port_raw = _norm(payload.get("port"))
    database = _norm(payload.get("database"))
    user = _norm(payload.get("access_key_id") or payload.get("username") or payload.get("user"))
    password = _norm(payload.get("secret_access_key") or payload.get("password"))
    jdbc_url = _norm(payload.get("jdbc_url") or payload.get("connection_url"))

    if engine == "other":
        if not jdbc_url:
            raise ConnectionValidationError(
                "Connection URL is required for engine=Other.",
                platform="rdbms",
                error_type="validation",
            )
        return _validate_rdbms_url(jdbc_url, user=user, password=password)

    if not host:
        raise ConnectionValidationError(
            "Host / IP is required for on-premises RDBMS.",
            platform="rdbms",
            error_type="validation",
        )
    if not user:
        raise ConnectionValidationError(
            "Username is required for on-premises RDBMS.",
            platform="rdbms",
            error_type="validation",
        )
    if not database:
        raise ConnectionValidationError(
            "Database / service name is required.",
            platform="rdbms",
            error_type="validation",
        )

    try:
        port = int(port_raw) if port_raw else _RDBMS_DEFAULT_PORTS.get(engine, 5432)
    except ValueError as exc:
        raise ConnectionValidationError(
            "Port must be a number.",
            platform="rdbms",
            error_type="validation",
        ) from exc

    try:
        if engine == "postgresql":
            version = _rdbms_connect_postgres(host, port, database, user, password)
        elif engine in {"mysql", "mariadb"}:
            version = _rdbms_connect_mysql(host, port, database, user, password)
        elif engine == "sqlserver":
            version = _rdbms_connect_sqlserver(host, port, database, user, password)
        elif engine == "oracle":
            version = _rdbms_connect_oracle(host, port, database, user, password)
        elif engine == "db2":
            version = _rdbms_connect_db2(host, port, database, user, password)
        else:
            raise ConnectionValidationError(
                f"Unsupported RDBMS engine '{engine}'.",
                platform="rdbms",
                error_type="unsupported",
            )
    except ConnectionValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"RDBMS connection failed: {_safe_error(exc)}",
            platform="rdbms",
            error_type="auth",
        ) from exc

    return {
        "ok": True,
        "platform": "rdbms",
        "message": f"{engine} connection successful",
        "details": {
            "engine": engine,
            "host": host,
            "port": port,
            "database": database,
            "version": str(version)[:160],
        },
    }


def _validate_rdbms_url(url: str, *, user: str, password: str) -> dict[str, Any]:
    """Best-effort URL validation via SQLAlchemy when available."""
    try:
        from sqlalchemy import create_engine, text
    except ImportError as exc:
        raise ConnectionValidationError(
            "SQLAlchemy is required for generic RDBMS URLs. "
            "Install with: pip install sqlalchemy",
            platform="rdbms",
            error_type="dependency",
        ) from exc

    connect_args: dict[str, Any] = {}
    # Prefer embedded credentials; otherwise inject user/password kwargs when driver supports it.
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        with engine.connect() as conn:
            if user and "://" in url and "@" not in url.split("://", 1)[1]:
                # Some drivers accept connect_args; if URL has no user, retry with query params is driver-specific.
                pass
            row = conn.execute(text("SELECT 1")).fetchone()
        return {
            "ok": True,
            "platform": "rdbms",
            "message": "RDBMS URL connection successful",
            "details": {"engine": "other", "probe": str(row[0]) if row else "ok"},
        }
    except Exception as exc:  # noqa: BLE001
        # Second attempt: rebuild URL with user/password if provided.
        if user and "://" in url and "@" not in url.split("://", 1)[1]:
            try:
                from urllib.parse import quote_plus

                scheme, rest = url.split("://", 1)
                auth = quote_plus(user)
                if password:
                    auth += ":" + quote_plus(password)
                rebuilt = f"{scheme}://{auth}@{rest}"
                engine = create_engine(rebuilt, pool_pre_ping=True)
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT 1")).fetchone()
                return {
                    "ok": True,
                    "platform": "rdbms",
                    "message": "RDBMS URL connection successful",
                    "details": {"engine": "other", "probe": str(row[0]) if row else "ok"},
                }
            except Exception as exc2:  # noqa: BLE001
                raise ConnectionValidationError(
                    f"RDBMS URL connection failed: {_safe_error(exc2)}",
                    platform="rdbms",
                    error_type="auth",
                ) from exc2
        raise ConnectionValidationError(
            f"RDBMS URL connection failed: {_safe_error(exc)}",
            platform="rdbms",
            error_type="auth",
        ) from exc


def _rdbms_connect_postgres(host: str, port: int, database: str, user: str, password: str) -> str:
    try:
        import psycopg
    except ImportError as exc:
        raise ConnectionValidationError(
            "psycopg is not installed. Run: pip install 'psycopg[binary]'",
            platform="rdbms",
            error_type="dependency",
        ) from exc
    with psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password or None,
        connect_timeout=10,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            return str(cur.fetchone()[0])


def _rdbms_connect_mysql(host: str, port: int, database: str, user: str, password: str) -> str:
    try:
        import pymysql
    except ImportError as exc:
        raise ConnectionValidationError(
            "pymysql is not installed. Run: pip install pymysql",
            platform="rdbms",
            error_type="dependency",
        ) from exc
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password or "",
        database=database,
        connect_timeout=10,
        read_timeout=10,
        write_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            return str(cur.fetchone()[0])
    finally:
        conn.close()


def _rdbms_connect_sqlserver(host: str, port: int, database: str, user: str, password: str) -> str:
    # Prefer pymssql (simpler install); fall back to pyodbc.
    try:
        import pymssql  # type: ignore
    except ImportError:
        pymssql = None  # type: ignore
    if pymssql is not None:
        conn = pymssql.connect(
            server=host,
            port=port,
            user=user,
            password=password or "",
            database=database,
            login_timeout=10,
            timeout=10,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT @@VERSION")
                row = cur.fetchone()
                return str(row[0] if row else "ok")
        finally:
            conn.close()

    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise ConnectionValidationError(
            "SQL Server driver missing. Install one of: pip install pymssql  OR  pip install pyodbc",
            platform="rdbms",
            error_type="dependency",
        ) from exc

    # Common free ODBC drivers; try a few names.
    drivers = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server",
    ]
    last_err: Exception | None = None
    for driver in drivers:
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
            f"UID={user};PWD={password or ''};TrustServerCertificate=yes;"
        )
        try:
            conn = pyodbc.connect(conn_str, timeout=10)
            try:
                cur = conn.cursor()
                cur.execute("SELECT @@VERSION")
                row = cur.fetchone()
                return str(row[0] if row else "ok")
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise ConnectionValidationError(
        f"SQL Server connection failed: {_safe_error(last_err or Exception('no ODBC driver worked'))}",
        platform="rdbms",
        error_type="auth",
    )


def _rdbms_connect_oracle(host: str, port: int, database: str, user: str, password: str) -> str:
    try:
        import oracledb  # type: ignore
    except ImportError as exc:
        raise ConnectionValidationError(
            "oracledb is not installed. Run: pip install oracledb",
            platform="rdbms",
            error_type="dependency",
        ) from exc
    dsn = oracledb.makedsn(host, port, service_name=database)
    conn = oracledb.connect(user=user, password=password or "", dsn=dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(_rdbms_probe_sql("oracle"))
            row = cur.fetchone()
            return str(row[0] if row else "ok")
    finally:
        conn.close()


def _rdbms_connect_db2(host: str, port: int, database: str, user: str, password: str) -> str:
    try:
        import ibm_db  # type: ignore
    except ImportError as exc:
        raise ConnectionValidationError(
            "ibm_db is not installed. Run: pip install ibm_db",
            platform="rdbms",
            error_type="dependency",
        ) from exc
    conn_str = (
        f"DATABASE={database};HOSTNAME={host};PORT={port};PROTOCOL=TCPIP;"
        f"UID={user};PWD={password or ''};"
    )
    conn = ibm_db.connect(conn_str, "", "")
    try:
        stmt = ibm_db.exec_immediate(conn, "SELECT 1 FROM SYSIBM.SYSDUMMY1")
        row = ibm_db.fetch_tuple(stmt)
        return str(row[0] if row else "ok")
    finally:
        ibm_db.close(conn)


def _validate_aws(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise ConnectionValidationError(
            "boto3 is not installed on the API server. Run: pip install boto3",
            platform="aws",
            error_type="dependency",
        ) from exc

    auth_type = _norm(payload.get("auth_type")).lower() or "access_keys"
    region = _norm(payload.get("region")) or "us-east-1"
    access_key = _norm(payload.get("access_key_id"))
    secret_key = _norm(payload.get("secret_access_key"))
    role_arn = _norm(payload.get("role_arn"))

    try:
        if auth_type in {"access_keys"}:
            if not access_key or not secret_key:
                raise ConnectionValidationError(
                    "AWS access key ID and secret access key are required.",
                    platform="aws",
                    error_type="validation",
                )
            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
            )
            sts = session.client("sts")
            ident = sts.get_caller_identity()
        elif auth_type in {"iam_role", "assume_role"}:
            if not role_arn:
                raise ConnectionValidationError(
                    "IAM role ARN is required.",
                    platform="aws",
                    error_type="validation",
                )
            if access_key and secret_key:
                base = boto3.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=region,
                )
            else:
                base = boto3.Session(region_name=region)
            sts = base.client("sts")
            assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="datahive-validate")
            creds = assumed["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=region,
            )
            ident = session.client("sts").get_caller_identity()
        else:
            raise ConnectionValidationError(
                f"Unsupported AWS auth type '{auth_type}'.",
                platform="aws",
                error_type="validation",
            )
        return {
            "ok": True,
            "platform": "aws",
            "message": "AWS credentials validated (STS GetCallerIdentity)",
            "details": {
                "account": ident.get("Account"),
                "arn": ident.get("Arn"),
                "region": region,
                "auth_type": auth_type,
            },
        }
    except ConnectionValidationError:
        raise
    except (BotoCoreError, ClientError, Exception) as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"AWS authentication failed: {_safe_error(exc)}",
            platform="aws",
            error_type="auth",
        ) from exc


def _google_oauth_post(form: dict[str, str]) -> dict[str, Any]:
    """POST to Google's OAuth token endpoint; returns JSON body or raises HTTPError."""
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _validate_gcp_oauth(payload: dict[str, Any], *, project: str) -> dict[str, Any]:
    """Validate GCP OAuth client credentials (optional refresh token for live token)."""
    import urllib.error

    client_id = _norm(payload.get("client_id"))
    client_secret = _norm(payload.get("client_secret"))
    if not client_id or not client_secret:
        raise ConnectionValidationError(
            "GCP OAuth client ID and secret are required.",
            platform="gcp",
            error_type="validation",
        )

    refresh = _norm(payload.get("refresh_token") or payload.get("api_key"))
    if refresh:
        try:
            data = _google_oauth_post(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                }
            )
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                err_body = json.loads(raw)
            except json.JSONDecodeError:
                err_body = {}
            detail = err_body.get("error_description") or err_body.get("error") or raw
            raise ConnectionValidationError(
                f"GCP OAuth refresh failed: {_safe_error(Exception(detail))}",
                platform="gcp",
                error_type="auth",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ConnectionValidationError(
                f"GCP OAuth validation failed: {_safe_error(exc)}",
                platform="gcp",
                error_type="auth",
            ) from exc

        if not data.get("access_token"):
            raise ConnectionValidationError(
                "GCP OAuth token refresh did not return an access token.",
                platform="gcp",
                error_type="auth",
            )
        return {
            "ok": True,
            "platform": "gcp",
            "message": "GCP OAuth refresh succeeded",
            "details": {
                "project": project,
                "auth_type": "oauth2",
                "token_type": data.get("token_type"),
            },
        }

    # Client ID + secret alone cannot finish a user consent flow. Probe Google's
    # token endpoint with a dummy authorization code: invalid_client means the
    # pair is wrong; invalid_grant / redirect_uri_mismatch means the client was
    # accepted.
    try:
        _google_oauth_post(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": "datahive_validation_probe",
                "grant_type": "authorization_code",
                "redirect_uri": "http://localhost",
            }
        )
        # Extremely unlikely with a dummy code — treat as success if it somehow works.
        return {
            "ok": True,
            "platform": "gcp",
            "message": "GCP OAuth client accepted",
            "details": {"project": project, "auth_type": "oauth2"},
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            err_body = json.loads(raw)
        except json.JSONDecodeError:
            err_body = {}
        err = _norm(err_body.get("error")).lower()
        if err == "invalid_client":
            raise ConnectionValidationError(
                "GCP OAuth client ID or secret is invalid.",
                platform="gcp",
                error_type="auth",
            ) from exc
        if err in {"invalid_grant", "redirect_uri_mismatch", "invalid_request", "unauthorized_client"}:
            return {
                "ok": True,
                "platform": "gcp",
                "message": "GCP OAuth client ID and secret accepted",
                "details": {
                    "project": project,
                    "auth_type": "oauth2",
                    "probe": err,
                },
            }
        detail = err_body.get("error_description") or err_body.get("error") or raw
        raise ConnectionValidationError(
            f"GCP OAuth validation failed: {_safe_error(Exception(detail))}",
            platform="gcp",
            error_type="auth",
        ) from exc
    except ConnectionValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"GCP OAuth validation failed: {_safe_error(exc)}",
            platform="gcp",
            error_type="auth",
        ) from exc


def _validate_gcp(payload: dict[str, Any]) -> dict[str, Any]:
    auth_type = _norm(payload.get("auth_type")).lower() or "service_account"
    project = _norm(payload.get("account_id"))
    if not project:
        raise ConnectionValidationError(
            "GCP project ID is required.",
            platform="gcp",
            error_type="validation",
        )

    if auth_type == "api_key":
        api_key = _norm(payload.get("api_key"))
        if not api_key:
            raise ConnectionValidationError(
                "GCP API key is required.",
                platform="gcp",
                error_type="validation",
            )
        # Lightweight live check against a public discovery endpoint.
        try:
            import urllib.error
            import urllib.request

            url = (
                "https://www.googleapis.com/discovery/v1/apis/bigquery/v2/rest"
                f"?key={api_key}"
            )
            with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
                if resp.status >= 400:
                    raise ConnectionValidationError(
                        f"GCP API key rejected (HTTP {resp.status}).",
                        platform="gcp",
                        error_type="auth",
                    )
            return {
                "ok": True,
                "platform": "gcp",
                "message": "GCP API key accepted",
                "details": {"project": project, "auth_type": "api_key"},
            }
        except ConnectionValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectionValidationError(
                f"GCP API key validation failed: {_safe_error(exc)}",
                platform="gcp",
                error_type="auth",
            ) from exc

    if auth_type == "oauth2":
        return _validate_gcp_oauth(payload, project=project)

    sa_raw = _norm(payload.get("service_account_json"))
    if not sa_raw:
        raise ConnectionValidationError(
            "GCP service account JSON is required.",
            platform="gcp",
            error_type="validation",
        )
    try:
        info = json.loads(sa_raw)
    except json.JSONDecodeError as exc:
        raise ConnectionValidationError(
            "Service account JSON is not valid JSON.",
            platform="gcp",
            error_type="validation",
        ) from exc

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:
        raise ConnectionValidationError(
            "google-auth is not installed on the API server. Run: pip install google-auth",
            platform="gcp",
            error_type="dependency",
        ) from exc

    try:
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"],
        )
        creds.refresh(Request())
        return {
            "ok": True,
            "platform": "gcp",
            "message": "GCP service account token acquired",
            "details": {
                "project": project,
                "client_email": info.get("client_email"),
                "auth_type": "service_account",
                "token_valid": bool(creds.token),
            },
        }
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"GCP authentication failed: {_safe_error(exc)}",
            platform="gcp",
            error_type="auth",
        ) from exc


def _validate_azure(payload: dict[str, Any]) -> dict[str, Any]:
    return _validate_ms_graph(payload, kind="azure")


def _validate_ms_graph(payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    tenant = _norm(payload.get("tenant_id"))
    client_id = _norm(payload.get("client_id"))
    client_secret = _norm(payload.get("client_secret"))
    auth_type = _norm(payload.get("auth_type")).lower()

    if kind == "azure" and not tenant:
        raise ConnectionValidationError(
            "Azure tenant ID is required.",
            platform=kind,
            error_type="validation",
        )
    if not tenant:
        # SharePoint / OneDrive often still need tenant for app auth.
        tenant = "common"

    if auth_type in {"oauth2", "service_principal", ""} or kind == "azure":
        if not client_id or not client_secret:
            raise ConnectionValidationError(
                "Client ID and client secret are required.",
                platform=kind,
                error_type="validation",
            )
    else:
        raise ConnectionValidationError(
            f"Unsupported {kind} auth type '{auth_type}'.",
            platform=kind,
            error_type="validation",
        )

    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    scope = (
        "https://graph.microsoft.com/.default"
        if kind in {"sharepoint", "onedrive"}
        else "https://management.azure.com/.default"
    )
    body = (
        f"client_id={client_id}"
        f"&client_secret={client_secret}"
        f"&scope={scope}"
        f"&grant_type=client_credentials"
    ).encode("utf-8")

    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not data.get("access_token"):
            raise ConnectionValidationError(
                f"{kind} token response did not include an access token.",
                platform=kind,
                error_type="auth",
            )
        return {
            "ok": True,
            "platform": kind,
            "message": f"{kind} credentials validated (token acquired)",
            "details": {
                "tenant_id": tenant,
                "token_type": data.get("token_type"),
                "expires_in": data.get("expires_in"),
                "auth_type": auth_type or "service_principal",
            },
        }
    except ConnectionValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        detail = _safe_error(exc)
        if hasattr(exc, "read"):
            try:
                detail = _safe_error(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
        raise ConnectionValidationError(
            f"{kind} authentication failed: {detail}",
            platform=kind,
            error_type="auth",
        ) from exc


def _validate_google_drive(payload: dict[str, Any]) -> dict[str, Any]:
    auth_type = _norm(payload.get("auth_type")).lower() or "oauth2"
    if auth_type == "service_account":
        # Reuse GCP SA token path with Drive scope.
        sa_raw = _norm(payload.get("service_account_json"))
        if not sa_raw:
            raise ConnectionValidationError(
                "Google Drive service account JSON is required.",
                platform="googledrive",
                error_type="validation",
            )
        try:
            info = json.loads(sa_raw)
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"],
            )
            creds.refresh(Request())
            return {
                "ok": True,
                "platform": "googledrive",
                "message": "Google Drive service account token acquired",
                "details": {"client_email": info.get("client_email")},
            }
        except ImportError as exc:
            raise ConnectionValidationError(
                "google-auth is not installed on the API server. Run: pip install google-auth",
                platform="googledrive",
                error_type="dependency",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ConnectionValidationError(
                f"Google Drive authentication failed: {_safe_error(exc)}",
                platform="googledrive",
                error_type="auth",
            ) from exc

    client_id = _norm(payload.get("client_id"))
    client_secret = _norm(payload.get("client_secret"))
    if not client_id or not client_secret:
        raise ConnectionValidationError(
            "Google Drive OAuth client ID and secret are required.",
            platform="googledrive",
            error_type="validation",
        )
    refresh = _norm(payload.get("refresh_token") or payload.get("api_key"))
    if not refresh:
        raise ConnectionValidationError(
            "Google Drive OAuth needs a refresh token to validate live, "
            "or use service account auth.",
            platform="googledrive",
            error_type="validation",
        )
    try:
        data = _google_oauth_post(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }
        )
        if not data.get("access_token"):
            raise ConnectionValidationError(
                "Google Drive token refresh did not return an access token.",
                platform="googledrive",
                error_type="auth",
            )
        return {
            "ok": True,
            "platform": "googledrive",
            "message": "Google Drive OAuth refresh succeeded",
            "details": {"token_type": data.get("token_type")},
        }
    except ConnectionValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConnectionValidationError(
            f"Google Drive authentication failed: {_safe_error(exc)}",
            platform="googledrive",
            error_type="auth",
        ) from exc
