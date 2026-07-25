"""
Local R&D API — persist connector form payloads into MongoDB.

Database : datahivepoc
Collection: connectors

Run (from this directory):
    python3 -m venv .venv
    .venv/bin/pip install fastapi uvicorn pymongo
    .venv/bin/python connector_api.py

Requires local mongod on mongodb://127.0.0.1:27017
Listens on http://127.0.0.1:5055
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from pymongo.errors import PyMongoError

MONGO_URI = os.environ.get("DATAHIVE_MONGO_URI", "mongodb://127.0.0.1:27017")
DB_NAME = "datahivepoc"
COLLECTION = "connectors"

app = FastAPI(title="DataHive RND Connector Saver")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: MongoClient | None = None


def get_collection():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    # Fail fast if mongod is down
    _client.admin.command("ping")
    return _client[DB_NAME][COLLECTION]


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        get_collection()
        return {"ok": True, "mongo": MONGO_URI, "db": DB_NAME, "collection": COLLECTION}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}") from exc


@app.post("/api/connectors")
def save_connector(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not payload:
        raise HTTPException(status_code=400, detail="Empty payload")

    doc = dict(payload)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5055, log_level="info")
