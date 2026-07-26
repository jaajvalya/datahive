"""
Local R&D API — persist connector form payloads into MongoDB.

Database : from MONGO_URI in repo-root `.env` (default path segment), else datahivepoc
Collection: MONGO_COLLECTION in `.env` (default `connectors`)
Connection errors: `connection_logs` (override via MONGO_CONNECTION_LOGS_COLLECTION)

Run (from this directory):
    python3 -m venv .venv
    .venv/bin/pip install fastapi uvicorn pymongo
    .venv/bin/python connector_api.py

Listens on http://127.0.0.1:5055

Upload mode writes files to rnd/UPLOAD/ and metadata to MongoDB (connectors).
"""
from __future__ import annotations

import logging
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, uri_parser
from pymongo.errors import PyMongoError

log = logging.getLogger("datahive.connector_api")

_RND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _RND_DIR.parent
UPLOAD_DIR = _RND_DIR / "UPLOAD"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = frozenset(
    {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".parquet"}
)


def _load_repo_dotenv() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_repo_dotenv()

MONGO_URI = os.environ.get(
    "MONGO_URI",
    os.environ.get("DATAHIVE_MONGO_URI", "mongodb://127.0.0.1:27017"),
)
_parsed_uri = uri_parser.parse_uri(MONGO_URI)
DB_NAME = _parsed_uri.get("database") or "datahivepoc"
COLLECTION = os.environ.get("MONGO_COLLECTION", "connectors")
CONNECTION_LOGS_COLLECTION = os.environ.get(
    "MONGO_CONNECTION_LOGS_COLLECTION", "connection_logs"
)

_SENSITIVE_LOG_KEYS = frozenset(
    {
        "api_key",
        "client_secret",
        "secret_access_key",
        "service_account_json",
        "password",
        "credentials_ciphertext",
    }
)


class ConnectionLogIn(BaseModel):
    user: str | None = None
    message: str = Field(..., min_length=1)
    event: str = "connection.error"
    error_type: str = "client"
    context: dict[str, Any] | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_log_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    clean: dict[str, Any] = {}
    for key, value in context.items():
        if key in _SENSITIVE_LOG_KEYS:
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = _sanitize_log_context(value)
        else:
            clean[key] = value
    return clean


def _resolve_user(request: Request | None, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    if request is not None:
        header_user = request.headers.get("X-DataHive-User")
        if header_user and header_user.strip():
            return header_user.strip()
        state_user = getattr(request.state, "user", None)
        if isinstance(state_user, str) and state_user.strip():
            return state_user.strip()
    return "unknown"


def log_connection_failure(
    user: str,
    message: str,
    *,
    event: str = "connection.error",
    error_type: str = "server",
    context: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> None:
    record: dict[str, Any] = {
        "timestamp": _utc_now_iso(),
        "user": user,
        "event": event,
        "error_type": error_type,
        "message": message,
        "context": _sanitize_log_context(context),
    }
    if http_status is not None:
        record["http_status"] = http_status
    try:
        get_connection_logs_collection().insert_one(record)
    except Exception as exc:  # noqa: BLE001 — logging must not break API responses
        log.warning("connection_logs insert failed: %s", exc)


def _ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _safe_stored_name(original: str) -> str:
    base = Path(original or "upload").name
    safe = re.sub(r"[^\w.\- ]", "_", base).strip() or "upload"
    return f"{uuid.uuid4().hex[:12]}_{safe}"


def _insert_connector_doc(doc: dict[str, Any]) -> dict[str, Any]:
    doc.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    try:
        result = get_collection().insert_one(doc)
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB write failed: {exc}") from exc
    return {
        "ok": True,
        "id": str(result.inserted_id),
        "db": DB_NAME,
        "collection": COLLECTION,
    }


async def _write_upload_file(upload: UploadFile, dest: Path) -> int:
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                )
            out.write(chunk)
    return size


def _redacted_mongo_uri(uri: str) -> str:
    if "://" not in uri or "@" not in uri:
        return uri
    scheme, rest = uri.split("://", 1)
    _, host_part = rest.rsplit("@", 1)
    return f"{scheme}://***@{host_part}"


app = FastAPI(title="DataHive RND Connector Saver")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def attach_request_user(request: Request, call_next):
    request.state.user = request.headers.get("X-DataHive-User") or "unknown"
    return await call_next(request)


@app.exception_handler(HTTPException)
async def connection_http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    message = detail if isinstance(detail, str) else json.dumps(detail)
    log_connection_failure(
        _resolve_user(request),
        message,
        event="connection.http_error",
        error_type="server",
        http_status=exc.status_code,
        context={
            "path": request.url.path,
            "method": request.method,
        },
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def connection_unhandled_exception_handler(request: Request, exc: Exception):
    log_connection_failure(
        _resolve_user(request),
        str(exc),
        event="connection.unhandled_error",
        error_type="server",
        http_status=500,
        context={
            "path": request.url.path,
            "method": request.method,
            "exception_type": type(exc).__name__,
        },
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})

_client: MongoClient | None = None


def get_collection():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    # Fail fast if mongod is down
    _client.admin.command("ping")
    return _client[DB_NAME][COLLECTION]


def get_connection_logs_collection():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    return _client[DB_NAME][CONNECTION_LOGS_COLLECTION]


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        get_collection()
        return {
            "ok": True,
            "mongo": _redacted_mongo_uri(MONGO_URI),
            "db": DB_NAME,
            "collection": COLLECTION,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}") from exc


@app.post("/api/connection-logs")
def create_connection_log(body: ConnectionLogIn, request: Request) -> dict[str, bool]:
    log_connection_failure(
        _resolve_user(request, body.user),
        body.message,
        event=body.event,
        error_type=body.error_type,
        context=body.context,
    )
    return {"ok": True}


@app.post("/api/connectors")
def save_connector(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not payload:
        raise HTTPException(status_code=400, detail="Empty payload")
    return _insert_connector_doc(dict(payload))


@app.post("/api/connectors/upload")
async def save_connector_upload(
    file: UploadFile = File(...),
    metadata: str = Form(...),
) -> dict[str, Any]:
    try:
        meta = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid metadata JSON.") from exc
    if not isinstance(meta, dict) or not meta:
        raise HTTPException(status_code=400, detail="Metadata must be a non-empty object.")

    original_name = file.filename or meta.get("file_name") or "upload"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {suffix or '(none)'}.",
        )

    _ensure_upload_dir()
    stored_name = _safe_stored_name(original_name)
    dest = UPLOAD_DIR / stored_name
    bytes_written = await _write_upload_file(file, dest)

    doc = dict(meta)
    doc["mode"] = doc.get("mode") or "upload"
    doc["file_name"] = original_name
    doc["stored_file_name"] = stored_name
    doc["upload_relative_path"] = f"UPLOAD/{stored_name}"
    doc["file_size"] = bytes_written
    doc["file_type"] = file.content_type or doc.get("file_type") or ""

    result = _insert_connector_doc(doc)
    result["upload_relative_path"] = doc["upload_relative_path"]
    result["stored_file_name"] = stored_name
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5055, log_level="info")
