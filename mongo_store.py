"""Shared MongoDB persistence for connector / connection records."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient, uri_parser
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

_REPO_ROOT = Path(__file__).resolve().parent

_client: MongoClient | None = None
_db_name: str | None = None


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
    return os.environ.get("MONGO_COLLECTION", "connectors")


def connection_logs_collection_name() -> str:
    load_repo_dotenv()
    return os.environ.get("MONGO_CONNECTION_LOGS_COLLECTION", "connection_logs")


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


def insert_connector_document(doc: dict[str, Any]) -> str:
    payload = dict(doc)
    payload.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    try:
        result = connectors_collection().insert_one(payload)
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB write failed: {exc}") from exc
    return str(result.inserted_id)


def insert_connection_log(record: dict[str, Any]) -> None:
    try:
        connection_logs_collection().insert_one(record)
    except PyMongoError as exc:
        raise RuntimeError(f"MongoDB connection_logs write failed: {exc}") from exc
