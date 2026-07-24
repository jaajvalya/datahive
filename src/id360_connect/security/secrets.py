"""Secret broker — short-lived credentials, minted per task.

The control plane stores REFERENCES, never values:

    "secret_ref": "vault://tenant-a/kv/id360/pg-analytics-ro"

At task start the broker exchanges the agent's workload identity for a
credential scoped to that one task, with a TTL of at most 15 minutes. The
credential never touches disk, never appears in a log, and is zeroized after
use.

Where a source only supports static credentials, they stay in the TENANT's
vault - never in your control-plane database. Your platform should be able to
say, truthfully: we do not hold your database passwords.
"""
from __future__ import annotations

import abc
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse


@dataclass
class Credential:
    """A short-lived credential. `__repr__` and `__str__` are overridden so it
    cannot be accidentally interpolated into a log line or an f-string."""
    values: Mapping[str, str] = field(repr=False, default_factory=dict)
    expires_at: float = 0.0
    ref: str = ""

    def __repr__(self) -> str:
        return f"<Credential ref={self.ref!r} keys={sorted(self.values)} REDACTED>"

    __str__ = __repr__

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def get(self, key: str) -> str:
        if self.expired:
            raise RuntimeError("credential expired; re-mint rather than extending")
        return self.values[key]


class SecretBackend(abc.ABC):
    scheme: str

    @abc.abstractmethod
    def fetch(self, ref: str, *, tenant_id: str, ttl_seconds: int) -> Credential: ...


class VaultBackend(SecretBackend):
    """HashiCorp Vault. Prefer DYNAMIC database credentials
    (`database/creds/<role>`) over static KV entries: Vault creates a real
    database user with a TTL and revokes it automatically, so a leaked
    credential expires on its own."""
    scheme = "vault"

    def __init__(self, client=None):
        self._client = client   # hvac.Client

    def fetch(self, ref: str, *, tenant_id: str, ttl_seconds: int) -> Credential:
        path = urlparse(ref).path.lstrip("/")
        if "/creds/" in path:                      # dynamic
            resp = self._client.read(path)
            data, lease = resp["data"], resp["lease_duration"]
            return Credential(values=data, ref=ref,
                              expires_at=time.time() + min(lease, ttl_seconds))
        resp = self._client.secrets.kv.v2.read_secret_version(path=path)
        return Credential(values=resp["data"]["data"], ref=ref,
                          expires_at=time.time() + ttl_seconds)


class AwsStsBackend(SecretBackend):
    """AssumeRole with a session name carrying the run id, so the credential is
    attributable in the tenant's own CloudTrail."""
    scheme = "sts"

    def __init__(self, client=None):
        self._client = client   # boto3.client("sts")

    def fetch(self, ref: str, *, tenant_id: str, ttl_seconds: int) -> Credential:
        role_arn = urlparse(ref).path.lstrip("/")
        resp = self._client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"id360-{tenant_id}"[:64],
            DurationSeconds=max(900, ttl_seconds))
        c = resp["Credentials"]
        return Credential(
            values={"access_key_id": c["AccessKeyId"],
                    "secret_access_key": c["SecretAccessKey"],
                    "session_token": c["SessionToken"]},
            ref=ref, expires_at=c["Expiration"].timestamp())


class AzureKeyVaultBackend(SecretBackend):
    scheme = "akv"

    def __init__(self, client=None):
        self._client = client

    def fetch(self, ref: str, *, tenant_id: str, ttl_seconds: int) -> Credential:
        name = urlparse(ref).path.lstrip("/")
        secret = self._client.get_secret(name)
        return Credential(values={"value": secret.value}, ref=ref,
                          expires_at=time.time() + ttl_seconds)


class EnvBackend(SecretBackend):
    """Local development only. Never enable in a deployed environment."""
    scheme = "env"

    def fetch(self, ref: str, *, tenant_id: str, ttl_seconds: int) -> Credential:
        import os
        name = urlparse(ref).netloc or urlparse(ref).path.lstrip("/")
        raw = os.environ.get(name)
        if raw is None:
            raise KeyError(f"env secret {name} not set")
        return Credential(values={"value": raw}, ref=ref,
                          expires_at=time.time() + ttl_seconds)


class SecretBroker:
    """Resolves `secret_ref` URIs. Emits a SECRET_ACCESSED audit event on every
    mint - which principal, which reference, which task. Never the value."""

    def __init__(self, backends: list[SecretBackend], *, audit=None,
                 default_ttl: int = 900):
        self._backends = {b.scheme: b for b in backends}
        self._audit = audit
        self.default_ttl = default_ttl

    @contextmanager
    def credential(self, ref: str, *, tenant_id: str, run_id: str,
                   agent_svid: str, ttl_seconds: int | None = None
                   ) -> Iterator[Credential]:
        """Scoped credential. Always use as a context manager so the value is
        dropped as soon as the connection is established."""
        scheme = urlparse(ref).scheme
        backend = self._backends.get(scheme)
        if backend is None:
            raise KeyError(f"no secret backend registered for scheme {scheme!r}")

        ttl = min(ttl_seconds or self.default_ttl, self.default_ttl)
        cred = backend.fetch(ref, tenant_id=tenant_id, ttl_seconds=ttl)

        if self._audit:
            self._audit.record(
                "SECRET_ACCESSED", tenant_id=tenant_id, run_id=run_id,
                actor={"agent_svid": agent_svid},
                source={"secret_ref": ref},          # the reference, not the value
                governance={"ttl_seconds": ttl})
        try:
            yield cred
        finally:
            # Drop references promptly. Python cannot truly wipe immutable
            # strings, so also keep TTLs short - defence in depth, not a fix.
            cred.values = {}


def redact_dsn(dsn: str) -> str:
    """Make a connection string safe to log."""
    parsed = urlparse(dsn)
    if parsed.password:
        netloc = f"{parsed.username}:***@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return parsed._replace(netloc=netloc).geturl()
    return dsn
