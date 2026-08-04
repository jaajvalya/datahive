"""Shared MongoDB persistence for connector / connection / query / glossary logs.

Collections (override via repo-root `.env`, URI from MONGO_URI):
  - connector_dtls          connection documents (secrets encrypted)
  - connection_log          connect / save outcomes
  - query_log               Insights SQL executions
  - glossary_upload_log     glossary file uploads
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pymongo import MongoClient, uri_parser
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

import credential_crypto

_REPO_ROOT = Path(__file__).resolve().parent
_log = logging.getLogger("datahive.mongo_store")

_client: MongoClient | None = None
_db_name: str | None = None

_SENSITIVE_LOG_KEYS = frozenset(
    {
        "api_key",
        "client_secret",
        "secret_access_key",
        "service_account_json",
        "password",
        "private_key",
        "credentials_ciphertext",
    }
)

Outcome = Literal["success", "failure"]


def load_repo_dotenv() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mongo_uri() -> str:
    load_repo_dotenv()
    return os.environ.get(
        "MONGO_URI",
        os.environ.get("DATAHIVE_MONGO_URI", "mongodb://127.0.0.1:27017"),
    )


def connectors_collection_name() -> str:
    load_repo_dotenv()
    return os.environ.get("MONGO_COLLECTION", "connector_dtls")


def connection_logs_collection_name() -> str:
    load_repo_dotenv()
    return os.environ.get("MONGO_CONNECTION_LOGS_COLLECTION", "connection_log")


def query_logs_collection_name() -> str:
    load_repo_dotenv()
    return os.environ.get("MONGO_QUERY_LOGS_COLLECTION", "query_log")


def glossary_collection_name() -> str:
    """Glossary upload inventory / audit (default: glossary_upload_log)."""
    load_repo_dotenv()
    return os.environ.get(
        "MONGO_GLOSSARY_UPLOAD_LOG_COLLECTION",
        os.environ.get("MONGO_GLOSSARY_COLLECTION", "glossary_upload_log"),
    )


def glossary_upload_logs_collection_name() -> str:
    return glossary_collection_name()


def database_name() -> str:
    global _db_name
    if _db_name is None:
        parsed = uri_parser.parse_uri(mongo_uri())
        _db_name = parsed.get("database") or "datahivepoc"
    return _db_name


def _get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(mongo_uri(), serverSelectionTimeoutMS=5000)
    return _client


def connectors_collection() -> Collection:
    client = _get_client()
    db = client[database_name()]
    db.command("ping")
    return db[connectors_collection_name()]


def connection_logs_collection() -> Collection:
    return _get_client()[database_name()][connection_logs_collection_name()]


def query_logs_collection() -> Collection:
    return _get_client()[database_name()][query_logs_collection_name()]


def glossary_collection() -> Collection:
    return _get_client()[database_name()][glossary_collection_name()]


def glossary_upload_logs_collection() -> Collection:
    return glossary_collection()


def insert_glossary_document(doc: dict[str, Any]) -> str:
    payload = dict(doc)
    payload.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("event", "glossary.upload")
    try:
        result = glossary_collection().insert_one(payload)
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB glossary_upload_log write failed: {exc}") from exc
    return str(result.inserted_id)


def insert_glossary_upload_log(record: dict[str, Any]) -> str:
    """Alias for glossary upload audit inserts into glossary_upload_log."""
    return insert_glossary_document(record)


def recent_glossary_documents(limit: int = 20) -> list[dict[str, Any]]:
    try:
        cursor = (
            glossary_collection()
            .find({}, {"_id": 1, "file_name": 1, "stored_file_name": 1,
                       "upload_relative_path": 1, "file_size": 1, "user": 1,
                       "notes": 1, "saved_at": 1, "term_count": 1, "apply": 1})
            .sort([("saved_at", -1), ("_id", -1)])
            .limit(max(1, min(limit, 100)))
        )
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB glossary read failed: {exc}") from exc
    items: list[dict[str, Any]] = []
    for doc in cursor:
        item = dict(doc)
        item["id"] = str(item.pop("_id"))
        items.append(item)
    return items


def sanitize_log_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    clean: dict[str, Any] = {}
    for key, value in context.items():
        if key in _SENSITIVE_LOG_KEYS:
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = sanitize_log_context(value)
        else:
            clean[key] = value
    return clean


def connector_summary_context(doc: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "connector_type",
        "cloud",
        "mode",
        "display_name",
        "connection_id",
        "upload_relative_path",
        "source",
        "owner",
    )
    return {k: doc[k] for k in keys if doc.get(k) is not None}


def insert_connector_document(doc: dict[str, Any]) -> str:
    """Persist a connector into connector_dtls with secrets encrypted at rest."""
    try:
        payload = credential_crypto.seal_connector_document(dict(doc))
    except RuntimeError as exc:
        raise RuntimeError(f"Credential encryption failed: {exc}") from exc
    payload.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    try:
        result = connectors_collection().insert_one(payload)
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB write failed: {exc}") from exc
    return str(result.inserted_id)


def get_connector_document(connector_id: str, *, with_secrets: bool = False) -> dict[str, Any] | None:
    """Load a connector_dtls document. Set with_secrets=True only for server-side auth."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(connector_id)
    except InvalidId as exc:
        raise ValueError(f"Invalid connector id: {connector_id}") from exc
    try:
        doc = connectors_collection().find_one({"_id": oid})
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB read failed: {exc}") from exc
    if not doc:
        return None
    item = dict(doc)
    item["id"] = str(item.pop("_id"))
    if with_secrets:
        return credential_crypto.unseal_connector_document(item)
    for key in credential_crypto.SENSITIVE_CONNECTOR_KEYS:
        item.pop(key, None)
    return item


def connector_credentials_for_auth(connector_id: str) -> dict[str, Any]:
    """Decrypt secrets from connector_dtls for authentication/authorization."""
    doc = get_connector_document(connector_id, with_secrets=True)
    if not doc:
        raise LookupError(f"Connector not found: {connector_id}")
    return credential_crypto.connector_credentials_for_auth(doc)


def insert_connection_log(record: dict[str, Any]) -> None:
    try:
        connection_logs_collection().insert_one(record)
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB connection_logs write failed: {exc}") from exc


def insert_query_log(record: dict[str, Any]) -> None:
    try:
        query_logs_collection().insert_one(record)
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB query_log write failed: {exc}") from exc


def log_connection_event(
    user: str,
    message: str,
    *,
    outcome: Outcome,
    event: str,
    error_type: str | None = None,
    context: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> None:
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user.strip() if user and user.strip() else "unknown",
        "outcome": outcome,
        "event": event,
        "message": message,
        "context": sanitize_log_context(context),
    }
    if error_type:
        record["error_type"] = error_type
    if http_status is not None:
        record["http_status"] = http_status
    insert_connection_log(record)


def append_connection_log(
    user: str,
    message: str,
    *,
    outcome: Outcome,
    event: str,
    error_type: str | None = None,
    context: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> None:
    """Best-effort connection_logs write; never raises."""
    try:
        log_connection_event(
            user,
            message,
            outcome=outcome,
            event=event,
            error_type=error_type,
            context=context,
            http_status=http_status,
        )
    except RuntimeError as exc:
        _log.error("connection_logs insert failed: %s", exc)


def append_query_log(record: dict[str, Any]) -> None:
    """Best-effort query_log write; never raises."""
    try:
        insert_query_log(record)
    except RuntimeError as exc:
        _log.error("query_log insert failed: %s", exc)
