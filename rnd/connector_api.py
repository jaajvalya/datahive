"""
Local R&D API — persist connector form payloads into MongoDB.

Database : from MONGO_URI in repo-root `.env` (default path segment), else datahivepoc
Collection: MONGO_COLLECTION in `.env` (default `connectors`)
Connection errors: `connection_logs` (override via MONGO_CONNECTION_LOGS_COLLECTION)
Connection logs record both success and failure outcomes.

Run (from this directory):
    python3 -m venv .venv
    .venv/bin/pip install fastapi uvicorn pymongo
    .venv/bin/python connector_api.py

Listens on http://127.0.0.1:5055

Upload mode writes files to rnd/UPLOAD/ and metadata to MongoDB (connectors).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

_RND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _RND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mongo_store  # noqa: E402
import postgres_store  # noqa: E402

log = logging.getLogger("datahive.connector_api")

UPLOAD_DIR = _RND_DIR / "UPLOAD"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = frozenset(
    {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".parquet"}
)

mongo_store.load_repo_dotenv()
MONGO_URI = mongo_store.mongo_uri()
DB_NAME = mongo_store.database_name()
COLLECTION = mongo_store.connectors_collection_name()
CONNECTION_LOGS_COLLECTION = mongo_store.connection_logs_collection_name()


class ConnectionLogIn(BaseModel):
    user: str | None = None
    message: str = Field(..., min_length=1)
    event: str = "connection.error"
    outcome: str = "failure"
    error_type: str | None = "client"
    context: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _normalize_outcome(self) -> ConnectionLogIn:
        if self.outcome not in ("success", "failure"):
            self.outcome = "failure"
        if self.outcome == "success":
            self.error_type = None
        return self


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
    error_type: str | None = "server",
    context: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> None:
    try:
        mongo_store.log_connection_event(
            user,
            message,
            outcome="failure",
            event=event,
            error_type=error_type,
            context=context,
            http_status=http_status,
        )
    except RuntimeError as exc:
        log.error("connection_logs write failed: %s", exc)


def _ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _safe_stored_name(original: str) -> str:
    base = Path(original or "upload").name
    safe = re.sub(r"[^\w.\- ]", "_", base).strip() or "upload"
    return f"{uuid.uuid4().hex[:12]}_{safe}"


def _insert_connector_doc(doc: dict[str, Any]) -> dict[str, Any]:
    payload = dict(doc)
    payload.setdefault("connection_status", "connected")
    try:
        inserted_id = mongo_store.insert_connector_document(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    log_ok = True
    log_detail: str | None = None
    try:
        mongo_store.log_connection_event(
            str(payload.get("user") or "unknown"),
            f"Connection established — saved to {DB_NAME}.{COLLECTION}",
            outcome="success",
            event="connection.established",
            context={
                **mongo_store.connector_summary_context(payload),
                "connector_id": inserted_id,
                "connection_status": payload.get("connection_status"),
                "db": DB_NAME,
                "collection": COLLECTION,
            },
        )
    except RuntimeError as exc:
        log_ok = False
        log_detail = str(exc)
        log.error("connection_logs write after connector save failed: %s", exc)
    return {
        "ok": True,
        "id": inserted_id,
        "db": DB_NAME,
        "collection": COLLECTION,
        "connection_status": payload.get("connection_status"),
        "connection_log": log_ok,
        "connection_log_error": log_detail,
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


def get_collection():
    return mongo_store.connectors_collection()


_RECENT_CONNECTOR_FIELDS = (
    "cloud",
    "connector_type",
    "display_name",
    "mode",
    "region",
    "upload_notes",
    "user",
    "saved_at",
)


def _fetch_recent_connectors(limit: int) -> list[dict[str, Any]]:
    capped = min(max(limit, 1), 20)
    projection = {field: 1 for field in _RECENT_CONNECTOR_FIELDS}
    projection["_id"] = 0
    cursor = (
        get_collection()
        .find({}, projection)
        .sort([("saved_at", -1), ("_id", -1)])
        .limit(capped)
    )
    return list(cursor)


@app.get("/health")
def health(recent: int = 0) -> dict[str, Any]:
    try:
        get_collection()
        payload: dict[str, Any] = {
            "ok": True,
            "mongo": _redacted_mongo_uri(MONGO_URI),
            "db": DB_NAME,
            "collection": COLLECTION,
            "connection_logs_collection": CONNECTION_LOGS_COLLECTION,
        }
        try:
            postgres_store.ping_postgres()
            payload["postgres"] = postgres_store.redacted_postgres_host()
            payload["postgres_ok"] = True
            kw = postgres_store.postgres_dsn_kwargs()
            payload["postgres_target"] = (
                f"{kw['user']}@{kw['host']}:{kw['port']}/{kw['dbname']}"
            )
            try:
                payload["asset_counts"] = postgres_store.catalog_counts()
            except Exception as count_exc:  # noqa: BLE001
                payload["asset_counts_error"] = str(count_exc)
        except Exception as pg_exc:  # noqa: BLE001
            payload["postgres_ok"] = False
            payload["postgres_error"] = str(pg_exc)
        if recent > 0:
            payload["recent_connectors"] = _fetch_recent_connectors(recent)
        return payload
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}") from exc


@app.get("/api/assets/relevant")
def assets_relevant(
    request: Request,
    tab: str = "recently_verified",
    type: str | None = None,
) -> dict[str, Any]:
    if tab not in ("recently_verified", "my_drafts"):
        raise HTTPException(status_code=422, detail="invalid tab")
    try:
        return postgres_store.relevant_assets(_resolve_user(request), tab, type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL read failed: {exc}") from exc


@app.get("/api/assets/search")
def assets_search(
    request: Request,
    q: str = "",
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        return postgres_store.search_assets(
            _resolve_user(request), q, limit=limit, offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL search failed: {exc}") from exc


@app.get("/api/assets/discover")
def assets_discover(request: Request, limit: int = 100) -> dict[str, Any]:
    capped = min(max(limit, 1), 500)
    try:
        return postgres_store.discover_assets(_resolve_user(request), limit=capped)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL discover failed: {exc}") from exc


@app.get("/api/assets/schemas")
def assets_schemas() -> dict[str, Any]:
    try:
        return {
            "items": postgres_store.list_schemas(),
            "counts": postgres_store.catalog_counts(),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL schemas failed: {exc}") from exc


@app.get("/api/assets/counts")
def assets_counts() -> dict[str, Any]:
    try:
        return {"counts": postgres_store.catalog_counts()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL counts failed: {exc}") from exc


@app.get("/api/assets/tables")
def assets_tables(schema: str) -> dict[str, Any]:
    try:
        items = postgres_store.list_tables(schema)
        return {"schema": schema, "count": len(items), "items": items}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL tables failed: {exc}") from exc


@app.get("/api/assets/structure")
def assets_structure(schema: str, table: str) -> dict[str, Any]:
    try:
        return postgres_store.table_structure(schema, table)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL structure failed: {exc}") from exc


@app.post("/api/connection-logs")
def create_connection_log(body: ConnectionLogIn, request: Request) -> dict[str, bool]:
    try:
        mongo_store.log_connection_event(
            _resolve_user(request, body.user),
            body.message,
            outcome=body.outcome,  # validated success|failure
            event=body.event,
            error_type=body.error_type,
            context=body.context,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/connectors")
def save_connector(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not payload:
        raise HTTPException(status_code=400, detail="Empty payload")
    return _insert_connector_doc(dict(payload))


@app.get("/api/connectors/recent")
def list_recent_connectors(limit: int = 5) -> dict[str, Any]:
    """Return the newest saved connectors from MongoDB (db/collection from repo `.env` MONGO_URI)."""
    try:
        items = _fetch_recent_connectors(limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB read failed: {exc}") from exc
    return {"ok": True, "items": items, "db": DB_NAME, "collection": COLLECTION}


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
