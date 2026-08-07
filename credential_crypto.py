"""Reversible credential encryption for connector_dtls (Fernet / AES).

Passwords and keys must be recoverable for later authentication, so this uses
authenticated encryption — not one-way hashing.

Key source (repo-root `.env`):
  DATAHIVE_SECRETS_KEY=<any long secret string>
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SENSITIVE_CONNECTOR_KEYS = (
    "api_key",
    "client_secret",
    "refresh_token",
    "secret_access_key",
    "service_account_json",
    "password",
    "private_key",
    "jdbc_url",
)


def _load_repo_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def secrets_key() -> str:
    _load_repo_dotenv()
    key = os.environ.get("DATAHIVE_SECRETS_KEY") or os.environ.get("ID360_SECRETS_KEY")
    if not key or not key.strip():
        raise RuntimeError(
            "DATAHIVE_SECRETS_KEY is not set in `.env`. "
            "Add a long random secret so connector passwords/keys can be encrypted."
        )
    return key.strip()


def _fernet() -> Fernet:
    derived = base64.urlsafe_b64encode(hashlib.sha256(secrets_key().encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_credentials(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(payload).decode("utf-8")


def decrypt_credentials(ciphertext: str) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Could not decrypt connector credentials. "
            "DATAHIVE_SECRETS_KEY may have changed since this document was saved."
        ) from exc
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("Decrypted credentials payload is not an object.")
    return data


def extract_sensitive_fields(doc: dict[str, Any]) -> dict[str, Any]:
    secrets: dict[str, Any] = {}
    for key in SENSITIVE_CONNECTOR_KEYS:
        value = doc.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        secrets[key] = value
    return secrets


def seal_connector_document(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Move sensitive fields into credentials_ciphertext and strip plaintext.
    Safe to call repeatedly (already-sealed docs are left alone).
    """
    payload = dict(doc)
    if payload.get("credentials_ciphertext") and not extract_sensitive_fields(payload):
        payload["credentials_encrypted"] = True
        for key in SENSITIVE_CONNECTOR_KEYS:
            payload.pop(key, None)
        return payload

    secrets = extract_sensitive_fields(payload)
    for key in SENSITIVE_CONNECTOR_KEYS:
        payload.pop(key, None)

    if secrets:
        payload["credentials_ciphertext"] = encrypt_credentials(secrets)
        payload["credentials_encrypted"] = True
        payload["credentials_keys"] = sorted(secrets.keys())
    else:
        payload.setdefault("credentials_encrypted", False)
    return payload


def unseal_connector_document(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy with plaintext secrets restored for authentication use.
    Never log or return this document to API clients.
    """
    out = dict(doc)
    ciphertext = out.get("credentials_ciphertext")
    if ciphertext:
        secrets = decrypt_credentials(str(ciphertext))
        out.update(secrets)
    return out


def connector_credentials_for_auth(doc: dict[str, Any]) -> dict[str, Any]:
    """Plaintext secret fields only (for driver auth / authorization)."""
    sealed = unseal_connector_document(doc)
    return extract_sensitive_fields(sealed)
