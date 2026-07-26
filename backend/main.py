"""
ID360 Homepage — Backend API (FastAPI)
======================================

Enterprise-grade, pluggable backend for the ID360 personalized homepage
(front-end: ../frontend/index.html).

ID360 project mandates satisfied here
-------------------------------------
* FastAPI for the API surface.
* Security in transit .... TLS/HSTS guidance + strict CORS allow-list + secure headers.
* Security at rest ....... bcrypt password hashing; secrets from environment; SQLite
                           file intended to live on an encrypted volume (see NOTE below).
* No data leakage ........ Pydantic response models whitelist fields; generic client errors.
* Auditability ........... append-only JSON audit log for every security/data event.
* Traceability ........... per-request X-Request-ID + structured access logs (who/what/when/result).
* Abuse protection ....... per-client rate limiting (login + global) via slowapi.

Run
---
    pip install -r requirements.txt
    export ID360_JWT_SECRET="$(openssl rand -hex 32)"
    export ID360_SECRETS_KEY="$(openssl rand -hex 32)"
    export ID360_CORS_ORIGINS="http://localhost:5500,http://127.0.0.1:5500"
    uvicorn main:app --host 0.0.0.0 --port 8000
    # In production terminate TLS at the ingress / run uvicorn behind a TLS proxy.

NOTE (encryption at rest): SQLite itself is not encrypted. For production place
`id360.db` on an encrypted volume (LUKS/FDE), or swap the storage layer for a DB
with TDE (e.g. Postgres). The storage functions are isolated in the `store` section
to make that swap a drop-in change.

Demo credentials: username `mmadden`, password `Passw0rd!`
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mongo_store  # noqa: E402
from typing import Optional

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field, model_validator

# Rate limiting (optional at runtime, required in production).
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
    _HAS_SLOWAPI = True
except Exception:  # pragma: no cover - graceful degradation if not installed
    _HAS_SLOWAPI = False


# ============================================================
# Configuration (typed, from environment — never hard-code secrets)
# ============================================================
class Settings:
    def __init__(self) -> None:
        self.app_name = "ID360 Homepage API"
        self.jwt_secret = os.getenv("ID360_JWT_SECRET", "dev-only-insecure-change-me")
        self.jwt_alg = "HS256"
        self.secrets_key = os.getenv("ID360_SECRETS_KEY", "dev-only-insecure-change-me")
        self.access_ttl_min = int(os.getenv("ID360_ACCESS_TTL_MIN", "30"))
        self.db_path = os.getenv("ID360_DB_PATH", str(Path(__file__).with_name("id360.db")))
        self.audit_path = os.getenv("ID360_AUDIT_PATH", str(Path(__file__).with_name("audit.log")))
        origins = os.getenv(
            "ID360_CORS_ORIGINS",
            "http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080,null",
        )
        # "null" allows opening index.html directly from the file system (Origin: null).
        self.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]
        self.rate_limit = os.getenv("ID360_RATE_LIMIT", "120/minute")
        self.login_rate_limit = os.getenv("ID360_LOGIN_RATE_LIMIT", "10/minute")


settings = Settings()

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login", auto_error=False)


# ============================================================
# Structured logging + append-only audit trail
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
log = logging.getLogger("id360")


def audit(event: str, *, request_id: str, actor: str = "-", outcome: str = "ok", **extra) -> None:
    """Append-only audit record. Never logs secrets or PII bodies."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "request_id": request_id,
        "actor": actor,
        "outcome": outcome,
        **extra,
    }
    line = json.dumps(rec, separators=(",", ":"))
    try:
        with open(settings.audit_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:  # never let auditing crash the request; surface via app log
        log.error(json.dumps({"audit_write_failed": event}))
    log.info(line)


# ============================================================
# Storage layer (SQLite). Isolated so it can be swapped for Postgres/TDE.
# ============================================================
@contextmanager
def db():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                username TEXT PRIMARY KEY, display_name TEXT, first_name TEXT,
                initials TEXT, role TEXT, password_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS assets(
                id INTEGER PRIMARY KEY, owner TEXT, name TEXT, type TEXT, crumb TEXT,
                edited TEXT, verified INTEGER, warning INTEGER, tab TEXT
            );
            CREATE TABLE IF NOT EXISTS announcements(
                id INTEGER PRIMARY KEY, owner TEXT, severity TEXT, asset TEXT, verified INTEGER,
                label TEXT, title TEXT, body TEXT, author TEXT, time TEXT
            );
            CREATE TABLE IF NOT EXISTS resources(
                id INTEGER PRIMARY KEY, owner TEXT, who TEXT, av TEXT, when_txt TEXT,
                txt TEXT, tag TEXT, link TEXT
            );
            CREATE TABLE IF NOT EXISTS personalization(
                username TEXT PRIMARY KEY, persona TEXT, purpose TEXT
            );
            CREATE TABLE IF NOT EXISTS connections(
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                display_name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_version_path TEXT NOT NULL DEFAULT '',
                auth_type TEXT NOT NULL,
                credentials_ciphertext TEXT NOT NULL,
                database_name TEXT NOT NULL DEFAULT '',
                schema_name TEXT NOT NULL DEFAULT '',
                tables_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # ---- seed once ----
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)",
                ("mmadden", "Matt Madden", "Matt", "MM", "editor", pwd.hash("Passw0rd!")),
            )
            assets = [
                ("mmadden", "Food Beverage Order Analysis", "Dashboard",
                 "Dashboard › Food Beverage Order Analysis", "edited 3 months ago", 1, 1, "recently_verified"),
                ("mmadden", "Customer Acquisition Cost Metrics", "Dashboard",
                 "Dashboard › Customer Acquisition Cost", "edited 3 months ago", 1, 0, "recently_verified"),
                ("mmadden", "Consolidated_dashboard", "Dashboard",
                 "Dashboard › Cost Overruns Dashboard", "edited 7 months ago", 1, 0, "recently_verified"),
                ("mmadden", "Revenue by Region (draft)", "Query",
                 "Query › Revenue by Region", "edited yesterday", 0, 0, "my_drafts"),
                ("mmadden", "Churn Cohorts (draft)", "View",
                 "View › Churn Cohorts", "edited 2 days ago", 0, 0, "my_drafts"),
            ]
            conn.executemany(
                "INSERT INTO assets(owner,name,type,crumb,edited,verified,warning,tab) VALUES(?,?,?,?,?,?,?,?)",
                assets,
            )
            conn.executemany(
                "INSERT INTO announcements(owner,severity,asset,verified,label,title,body,author,time)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("mmadden", "warning", "beverages_order_customer", 1, "Warning", "Airflow DAG failed!",
                     "Airflow DAG atlan.airflow.com/dag/432948385 failed. Data is not refreshed on this "
                     "table and all downstream assets.", "rohan", "2 hours ago"),
                    ("mmadden", "error", "spend_overview", 1, "Error", "Data quality check breached",
                     "Null rate on revenue column exceeded threshold on the last run.", "ravi", "5 hours ago"),
                ],
            )
            conn.executemany(
                "INSERT INTO resources(owner,who,av,when_txt,txt,tag,link) VALUES(?,?,?,?,?,?,?)",
                [
                    ("mmadden", "ID360", "ID", "yesterday",
                     "@andrew what's the status of this data source with the failure?",
                     "#does-anyone-know", "INSTACART_BEVERAGES_ORDER_CUSTOMER"),
                    ("mmadden", "ravi", "RA", "yesterday",
                     "Added Instacart Revenue and Usage Statistics", "", "Spend Overview"),
                    ("mmadden", "ID360", "ID", "yesterday",
                     "@andrew why is Sparkling Grapefruit #1 — that looks wrong", "", ""),
                ],
            )
            conn.execute(
                "INSERT INTO personalization VALUES(?,?,?)", ("mmadden", "Data Analyst", "Reporting")
            )


# ============================================================
# Credential encryption (Fernet). Key derived from ID360_SECRETS_KEY so
# operators can supply any string (matches the ID360_JWT_SECRET pattern)
# rather than a pre-formatted Fernet key.
# ============================================================
def _fernet() -> Fernet:
    derived = base64.urlsafe_b64encode(hashlib.sha256(settings.secrets_key.encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_credentials(fields: dict) -> str:
    payload = json.dumps(fields, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("utf-8")


def decrypt_credentials(ciphertext: str) -> dict:
    return json.loads(_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8"))


PERSONAS = ["Data Analyst", "Marketing"]
PURPOSES = ["Reporting", "Governance", "Discovery", "Onboarding", "Quality"]


# ============================================================
# Pydantic response models — the stable front-end contract.
# Only whitelisted fields ever reach the client (no data leakage).
# ============================================================
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class Me(BaseModel):
    display_name: str
    first_name: str
    initials: str
    greeting: str
    notifications: int


class Asset(BaseModel):
    name: str
    type: str
    crumb: str
    edited: str
    verified: bool
    warning: bool


class RelevantResponse(BaseModel):
    tab: str
    type: Optional[str] = None
    counts: dict[str, int]
    items: list[Asset]


class SearchItem(BaseModel):
    name: str
    type: str


class SearchResponse(BaseModel):
    query: str
    items: list[SearchItem]


class Announcement(BaseModel):
    severity: str
    asset: str
    verified: bool
    label: str
    title: str
    body: str
    author: str
    time: str


class Resource(BaseModel):
    who: str
    av: str
    when: str
    txt: str
    tag: str
    link: str


class NamedItem(BaseModel):
    name: str


class ListResponse(BaseModel):
    items: list


class PersonalizationIn(BaseModel):
    persona: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=64)


CONNECTOR_TYPES = {
    "googlecloud", "microsoftazure", "amazonwebservices", "snowflake",
    "databricks", "mongodb", "postgresql", "microsoftsqlserver", "mysql",
    "microsoftsharepoint", "mailbox", "manualupload", "microsoftonedrive", "googledrive",
}
AUTH_TYPES = {"api_key", "oauth2", "service_account"}


class ConnectionIn(BaseModel):
    connector_type: str
    display_name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    api_version_path: str = Field(default="", max_length=128)
    auth_type: str
    api_key: Optional[str] = Field(default=None, max_length=4096)
    client_id: Optional[str] = Field(default=None, max_length=512)
    client_secret: Optional[str] = Field(default=None, max_length=4096)
    service_account_json: Optional[str] = Field(default=None, max_length=65536)
    database_name: str = Field(default="", max_length=256)
    schema_name: str = Field(default="", max_length=256)
    tables: str = Field(default="", max_length=4096)  # comma-separated free text

    @model_validator(mode="after")
    def _validate(self):
        if self.connector_type not in CONNECTOR_TYPES:
            raise ValueError("unknown connector_type")
        if self.auth_type not in AUTH_TYPES:
            raise ValueError("unknown auth_type")
        if self.auth_type == "api_key" and not (self.api_key or "").strip():
            raise ValueError("api_key is required for auth_type=api_key")
        if self.auth_type == "oauth2" and not ((self.client_id or "").strip() and (self.client_secret or "").strip()):
            raise ValueError("client_id and client_secret are required for auth_type=oauth2")
        if self.auth_type == "service_account":
            raw = (self.service_account_json or "").strip()
            if not raw:
                raise ValueError("service_account_json is required for auth_type=service_account")
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("service_account_json must be valid JSON")
        return self

    def credential_fields(self) -> dict:
        if self.auth_type == "api_key":
            return {"api_key": self.api_key}
        if self.auth_type == "oauth2":
            return {"client_id": self.client_id, "client_secret": self.client_secret}
        return {"service_account_json": self.service_account_json}


class Connection(BaseModel):
    id: str
    connector_type: str
    display_name: str
    base_url: str
    api_version_path: str
    auth_type: str
    credentials_configured: bool
    database_name: str
    schema_name: str
    tables: list[str]
    created_at: str
    updated_at: str


# ============================================================
# Auth helpers
# ============================================================
def create_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_ttl_min)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def current_user(request: Request, token: Optional[str] = Depends(oauth2)) -> dict:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except JWTError:
        audit("auth.token_invalid", request_id=getattr(request.state, "request_id", "-"),
              outcome="denied")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    with db() as conn:
        row = conn.execute(
            "SELECT username,display_name,first_name,initials,role FROM users WHERE username=?",
            (claims["sub"],),
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return dict(row)


def require_role(*roles: str):
    def _dep(user: dict = Depends(current_user)) -> dict:
        if roles and user["role"] not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user
    return _dep


# ============================================================
# App + middleware
# ============================================================
app = FastAPI(title=settings.app_name, version="1.0.0", docs_url="/docs", redoc_url=None)

if _HAS_SLOWAPI:
    limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,   # strict allow-list, not "*"
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


@app.middleware("http")
async def trace_and_secure(request: Request, call_next):
    """Assigns X-Request-ID, times the request, adds security headers, and logs the outcome."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # never leak stack traces to the client
        audit("request.error", request_id=request_id, outcome="error",
              path=request.url.path, method=request.method)
        response = JSONResponse({"detail": "Internal server error"}, status_code=500)

    took_ms = round((time.perf_counter() - started) * 1000, 1)
    # Security headers (defense in depth; TLS/HSTS assume a TLS-terminating proxy).
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Cache-Control"] = "no-store"
    audit("request", request_id=request_id, method=request.method, path=request.url.path,
          status=response.status_code, ms=took_ms)
    return response


@app.on_event("startup")
def _startup() -> None:
    init_db()
    if settings.jwt_secret == "dev-only-insecure-change-me":
        log.warning(json.dumps({"warn": "ID360_JWT_SECRET is unset — using an INSECURE dev secret"}))
    if settings.secrets_key == "dev-only-insecure-change-me":
        log.warning(json.dumps({"warn": "ID360_SECRETS_KEY is unset — using an INSECURE dev secret"}))
    log.info(json.dumps({"startup": settings.app_name, "cors": settings.cors_origins}))


# ============================================================
# Routes
# ============================================================
V1 = "/api/v1"


@app.get(V1 + "/health")
def health():
    return {"status": "ok", "service": settings.app_name, "time": datetime.now(timezone.utc).isoformat()}


def _login_impl(request: Request, username: str, password: str) -> Token:
    rid = request.state.request_id
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    # constant-ish behaviour: verify against a dummy hash when user is missing
    hashed = row["password_hash"] if row else pwd.hash("invalid")
    if not row or not pwd.verify(password, hashed):
        audit("auth.login", request_id=rid, actor=username, outcome="denied")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    audit("auth.login", request_id=rid, actor=username, outcome="ok", role=row["role"])
    return Token(access_token=create_token(row["username"], row["role"]))


if _HAS_SLOWAPI:
    @app.post(V1 + "/auth/login", response_model=Token)
    @limiter.limit(settings.login_rate_limit)
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        return _login_impl(request, username, password)
else:  # pragma: no cover
    @app.post(V1 + "/auth/login", response_model=Token)
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        return _login_impl(request, username, password)


@app.post(V1 + "/auth/logout")
def logout(request: Request, user: dict = Depends(current_user)):
    # Stateless JWT: client discards the token. Recorded for audit.
    audit("auth.logout", request_id=request.state.request_id, actor=user["username"])
    return {"detail": "logged out"}


@app.get(V1 + "/me", response_model=Me)
def me(request: Request, user: dict = Depends(current_user)):
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM announcements WHERE owner=?", (user["username"],)).fetchone()["c"]
    audit("me.read", request_id=request.state.request_id, actor=user["username"])
    return Me(display_name=user["display_name"], first_name=user["first_name"],
              initials=user["initials"], greeting=greeting, notifications=int(n) + 3)


@app.get(V1 + "/assets/relevant", response_model=RelevantResponse)
def relevant(request: Request, tab: str = "recently_verified", type: Optional[str] = None,
             user: dict = Depends(current_user)):
    if tab not in ("recently_verified", "my_drafts"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid tab")
    with db() as conn:
        rows = conn.execute(
            "SELECT name,type,crumb,edited,verified,warning FROM assets WHERE owner=? AND tab=?",
            (user["username"], tab),
        ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    # Fill the chip set the design expects, even when a type has 0 in this tab.
    for t in ["View", "Column", "Query", "Term", "Category", "Glossary", "Dashboard"]:
        counts.setdefault(t, 0)
    items = [Asset(name=r["name"], type=r["type"], crumb=r["crumb"], edited=r["edited"],
                   verified=bool(r["verified"]), warning=bool(r["warning"]))
             for r in rows if not type or r["type"] == type]
    audit("assets.relevant", request_id=request.state.request_id, actor=user["username"],
          tab=tab, type=type or "*", n=len(items))
    return RelevantResponse(tab=tab, type=type, counts=counts, items=items)


@app.get(V1 + "/assets/search", response_model=SearchResponse)
def search(request: Request, q: str = "", limit: int = 10, offset: int = 0,
           user: dict = Depends(current_user)):
    q = (q or "").strip()
    limit = max(1, min(limit, 50))
    items: list[SearchItem] = []
    if q:
        like = f"%{q}%"
        with db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT name,type FROM assets WHERE owner=? AND (name LIKE ? OR type LIKE ?)"
                " LIMIT ? OFFSET ?",
                (user["username"], like, like, limit, max(0, offset)),
            ).fetchall()
        items = [SearchItem(name=r["name"], type=r["type"]) for r in rows]
    audit("assets.search", request_id=request.state.request_id, actor=user["username"],
          q_len=len(q), n=len(items))  # log query length only, not the query text
    return SearchResponse(query=q, items=items)


@app.get(V1 + "/announcements", response_model=ListResponse)
def announcements(request: Request, user: dict = Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT severity,asset,verified,label,title,body,author,time FROM announcements WHERE owner=?",
            (user["username"],),
        ).fetchall()
    items = [Announcement(severity=r["severity"], asset=r["asset"], verified=bool(r["verified"]),
                          label=r["label"], title=r["title"], body=r["body"], author=r["author"],
                          time=r["time"]) for r in rows]
    audit("announcements.read", request_id=request.state.request_id, actor=user["username"], n=len(items))
    return ListResponse(items=items)


@app.get(V1 + "/resources", response_model=ListResponse)
def resources(request: Request, user: dict = Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT who,av,when_txt,txt,tag,link FROM resources WHERE owner=?",
            (user["username"],),
        ).fetchall()
    items = [Resource(who=r["who"], av=r["av"], when=r["when_txt"], txt=r["txt"],
                      tag=r["tag"], link=r["link"]) for r in rows]
    audit("resources.read", request_id=request.state.request_id, actor=user["username"], n=len(items))
    return ListResponse(items=items)


@app.get(V1 + "/personas", response_model=ListResponse)
def personas(request: Request, user: dict = Depends(current_user)):
    return ListResponse(items=[NamedItem(name=p) for p in PERSONAS])


@app.get(V1 + "/purposes", response_model=ListResponse)
def purposes(request: Request, user: dict = Depends(current_user)):
    return ListResponse(items=[NamedItem(name=p) for p in PURPOSES])


@app.put(V1 + "/personalization")
def set_personalization(request: Request, body: PersonalizationIn,
                        user: dict = Depends(current_user)):
    if body.persona not in PERSONAS or body.purpose not in PURPOSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown persona/purpose")
    with db() as conn:
        conn.execute(
            "INSERT INTO personalization(username,persona,purpose) VALUES(?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET persona=excluded.persona, purpose=excluded.purpose",
            (user["username"], body.persona, body.purpose),
        )
    audit("personalization.update", request_id=request.state.request_id, actor=user["username"],
          persona=body.persona, purpose=body.purpose)
    return {"persona": body.persona, "purpose": body.purpose}


# ============================================================
# Connections — client-configured connector endpoints/credentials/scope.
# Credentials are encrypted at rest (encrypt_credentials/decrypt_credentials
# above) and NEVER returned by any route; only credentials_configured=True.
# ============================================================
def _row_to_connection(row: sqlite3.Row) -> Connection:
    return Connection(
        id=row["id"], connector_type=row["connector_type"], display_name=row["display_name"],
        base_url=row["base_url"], api_version_path=row["api_version_path"], auth_type=row["auth_type"],
        credentials_configured=True, database_name=row["database_name"], schema_name=row["schema_name"],
        tables=json.loads(row["tables_json"]), created_at=row["created_at"], updated_at=row["updated_at"],
    )


@app.post(V1 + "/connections", response_model=Connection, status_code=status.HTTP_201_CREATED)
def create_connection(request: Request, body: ConnectionIn, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc).isoformat()
    cid = uuid.uuid4().hex
    tables = [t.strip() for t in body.tables.split(",") if t.strip()]
    ciphertext = encrypt_credentials(body.credential_fields())
    with db() as conn:
        conn.execute(
            "INSERT INTO connections(id,owner,connector_type,display_name,base_url,api_version_path,"
            "auth_type,credentials_ciphertext,database_name,schema_name,tables_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, user["username"], body.connector_type, body.display_name, body.base_url,
             body.api_version_path, body.auth_type, ciphertext, body.database_name,
             body.schema_name, json.dumps(tables), now, now),
        )
    mongo_doc = {
        "user": user["username"],
        "owner": user["username"],
        "connection_id": cid,
        "source": "id360-backend",
        "connector_type": body.connector_type,
        "display_name": body.display_name,
        "base_url": body.base_url,
        "api_version_path": body.api_version_path,
        "auth_type": body.auth_type,
        "database_name": body.database_name,
        "schema_name": body.schema_name,
        "tables": tables,
        "mode": "cloud",
        "saved_at": now,
    }
    try:
        connector_id = mongo_store.insert_connector_document(mongo_doc)
    except RuntimeError as exc:
        mongo_store.append_connection_log(
            user["username"],
            str(exc),
            outcome="failure",
            event="connection.save_failed",
            error_type="mongodb",
            context=mongo_store.connector_summary_context(mongo_doc),
            http_status=503,
        )
        with db() as conn:
            conn.execute("DELETE FROM connections WHERE id=?", (cid,))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    mongo_store.append_connection_log(
        user["username"],
        f"Connection saved to {mongo_store.database_name()}.{mongo_store.connectors_collection_name()}",
        outcome="success",
        event="connection.saved",
        context={
            **mongo_store.connector_summary_context(mongo_doc),
            "connector_id": connector_id,
            "db": mongo_store.database_name(),
            "collection": mongo_store.connectors_collection_name(),
        },
    )
    audit("connections.create", request_id=request.state.request_id, actor=user["username"],
          id=cid, connector_type=body.connector_type, auth_type=body.auth_type)
    return Connection(id=cid, connector_type=body.connector_type, display_name=body.display_name,
                       base_url=body.base_url, api_version_path=body.api_version_path,
                       auth_type=body.auth_type, credentials_configured=True,
                       database_name=body.database_name, schema_name=body.schema_name,
                       tables=tables, created_at=now, updated_at=now)


@app.get(V1 + "/connections", response_model=ListResponse)
def list_connections(request: Request, user: dict = Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM connections WHERE owner=? ORDER BY created_at DESC", (user["username"],),
        ).fetchall()
    items = [_row_to_connection(r) for r in rows]
    audit("connections.list", request_id=request.state.request_id, actor=user["username"], n=len(items))
    return ListResponse(items=items)


@app.get(V1 + "/connections/{conn_id}", response_model=Connection)
def get_connection(request: Request, conn_id: str, user: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id=? AND owner=?", (conn_id, user["username"]),
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    audit("connections.read", request_id=request.state.request_id, actor=user["username"], id=conn_id)
    return _row_to_connection(row)


@app.put(V1 + "/connections/{conn_id}", response_model=Connection)
def update_connection(request: Request, conn_id: str, body: ConnectionIn, user: dict = Depends(current_user)):
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM connections WHERE id=? AND owner=?", (conn_id, user["username"]),
        ).fetchone()
        if not existing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        now = datetime.now(timezone.utc).isoformat()
        tables = [t.strip() for t in body.tables.split(",") if t.strip()]
        ciphertext = encrypt_credentials(body.credential_fields())
        conn.execute(
            "UPDATE connections SET connector_type=?,display_name=?,base_url=?,api_version_path=?,"
            "auth_type=?,credentials_ciphertext=?,database_name=?,schema_name=?,tables_json=?,updated_at=?"
            " WHERE id=? AND owner=?",
            (body.connector_type, body.display_name, body.base_url, body.api_version_path,
             body.auth_type, ciphertext, body.database_name, body.schema_name, json.dumps(tables),
             now, conn_id, user["username"]),
        )
        row = conn.execute("SELECT * FROM connections WHERE id=?", (conn_id,)).fetchone()
    audit("connections.update", request_id=request.state.request_id, actor=user["username"], id=conn_id)
    return _row_to_connection(row)


@app.delete(V1 + "/connections/{conn_id}")
def delete_connection(request: Request, conn_id: str, user: dict = Depends(current_user)):
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM connections WHERE id=? AND owner=?", (conn_id, user["username"]),
        ).fetchone()
        if not existing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        conn.execute("DELETE FROM connections WHERE id=? AND owner=?", (conn_id, user["username"]))
    audit("connections.delete", request_id=request.state.request_id, actor=user["username"], id=conn_id)
    return {"detail": "deleted"}


# Ensure schema/seed exist as soon as the module is imported (idempotent).
# The startup event above still runs under uvicorn; this guarantees the tables
# exist even when the app is imported directly (e.g. by tests or a WSGI loader).
init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
