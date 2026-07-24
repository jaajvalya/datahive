"""Append-only, hash-chained audit ledger.

Separate from application logs. Logs are for debugging and are allowed to be
lossy; the ledger is a record of custody and is not.

Integrity model
---------------
    hash_n = sha256(hash_{n-1} || canonical_json(record_n))

The chain head is published periodically to a *separate trust anchor* - a
different cloud account, or a timestamping authority. That is what makes
tampering detectable rather than merely discouraged: an attacker who can
rewrite the ledger cannot also rewrite the published heads.

Storage: WORM. S3 Object Lock in compliance mode means your own root account
cannot delete it, which is precisely the property an auditor wants to hear.
"""
from __future__ import annotations

import abc
import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterator, Mapping

from .errors import AuditChainBroken
from .models import utcnow

GENESIS = "sha256:" + "0" * 64


class AuditEvent(str):
    """Event names. Denials and refusals are as important as successes -
    auditors ask what you declined to read."""
    CONNECTION_CREATED = "CONNECTION_CREATED"
    CONNECTION_UPDATED = "CONNECTION_UPDATED"
    CONNECTION_DELETED = "CONNECTION_DELETED"
    POLICY_CHANGED = "POLICY_CHANGED"
    SECRET_ACCESSED = "SECRET_ACCESSED"
    TASK_LEASED = "TASK_LEASED"
    POLICY_DENIED = "POLICY_DENIED"
    SCHEMA_DRIFT_DETECTED = "SCHEMA_DRIFT_DETECTED"
    EXTRACT_COMMIT = "EXTRACT_COMMIT"
    EXTRACT_FAILED = "EXTRACT_FAILED"
    BUDGET_BREACH = "BUDGET_BREACH"
    WATERMARK_OVERRIDE = "WATERMARK_OVERRIDE"
    SINK_DELETE = "SINK_DELETE"
    KEY_ROTATED = "KEY_ROTATED"
    KEY_REVOKED = "KEY_REVOKED"
    RECONCILE_OK = "RECONCILE_OK"
    RECONCILE_FAILED = "RECONCILE_FAILED"


@dataclass
class AuditRecord:
    seq: int
    ts: datetime
    event: str
    tenant_id: str
    prev_hash: str = GENESIS
    hash: str = ""
    run_id: str | None = None
    batch_id: str | None = None
    actor: Mapping[str, Any] = field(default_factory=dict)
    source: Mapping[str, Any] = field(default_factory=dict)
    extraction: Mapping[str, Any] = field(default_factory=dict)
    volume: Mapping[str, Any] = field(default_factory=dict)
    destination: Mapping[str, Any] = field(default_factory=dict)
    governance: Mapping[str, Any] = field(default_factory=dict)
    outcome: str = "SUCCESS"
    reason: str | None = None            # sanitized; never a raw driver message

    def canonical(self) -> str:
        d = asdict(self)
        d.pop("hash", None)
        return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)

    def compute_hash(self) -> str:
        payload = self.prev_hash + self.canonical()
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class AuditStore(abc.ABC):
    """Backend for the ledger. Implementations must be append-only."""

    @abc.abstractmethod
    def append(self, record: AuditRecord) -> None: ...

    @abc.abstractmethod
    def head(self, tenant_id: str) -> tuple[int, str]:
        """Return (last_seq, last_hash) for a tenant, or (0, GENESIS)."""

    @abc.abstractmethod
    def iter_records(self, tenant_id: str, *, start: int = 1) -> Iterator[AuditRecord]: ...


class InMemoryAuditStore(AuditStore):
    """Reference implementation for tests and local development.

    Production: replace with a WORM object-store writer (S3 Object Lock in
    compliance mode / Azure immutable blob), writing both JSON lines for the
    chain and Parquet for analysis.
    """

    def __init__(self) -> None:
        self._by_tenant: dict[str, list[AuditRecord]] = {}
        self._lock = threading.Lock()

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self._by_tenant.setdefault(record.tenant_id, []).append(record)

    def head(self, tenant_id: str) -> tuple[int, str]:
        with self._lock:
            recs = self._by_tenant.get(tenant_id) or []
            return (recs[-1].seq, recs[-1].hash) if recs else (0, GENESIS)

    def iter_records(self, tenant_id: str, *, start: int = 1) -> Iterator[AuditRecord]:
        for r in list(self._by_tenant.get(tenant_id, [])):
            if r.seq >= start:
                yield r


class AuditLedger:
    """Writer + verifier. One instance per process; safe for concurrent use."""

    def __init__(self, store: AuditStore):
        self.store = store
        self._lock = threading.Lock()

    def record(self, event: str, *, tenant_id: str, **fields: Any) -> AuditRecord:
        with self._lock:
            seq, prev = self.store.head(tenant_id)
            rec = AuditRecord(seq=seq + 1, ts=utcnow(), event=event,
                              tenant_id=tenant_id, prev_hash=prev, **fields)
            rec.hash = rec.compute_hash()
            self.store.append(rec)
            return rec

    def verify(self, tenant_id: str) -> int:
        """Re-walk the chain. Returns the number of records verified.
        Raises AuditChainBroken on the first mismatch - treat as an incident."""
        prev, count = GENESIS, 0
        for rec in self.store.iter_records(tenant_id):
            if rec.prev_hash != prev:
                raise AuditChainBroken("prev_hash mismatch", seq=rec.seq)
            if rec.compute_hash() != rec.hash:
                raise AuditChainBroken("record hash mismatch", seq=rec.seq)
            prev, count = rec.hash, count + 1
        return count

    def publish_head(self, tenant_id: str) -> dict[str, Any]:
        """Emit the current chain head for external anchoring.

        Ship this to a different account / cloud / timestamping authority on a
        schedule. Without an external anchor the chain proves ordering but not
        immutability against a privileged insider.
        """
        seq, h = self.store.head(tenant_id)
        return {"tenant_id": tenant_id, "seq": seq, "hash": h,
                "published_at": utcnow().isoformat()}


# ------------------------------------------------------------------ helper ---
def commit_record(*, tenant_id: str, run_id: str, batch_id: str,
                  agent_svid: str, owner: str, source, object_name: str,
                  strategy: str, predicate_hash: str, columns, masked,
                  position_from, position_to, row_count: int, byte_count: int,
                  query_seconds: float, sink_uri: str, content_sha256: str,
                  dek_key_id: str, policy_version: int, schema_version: int,
                  classification: str, legal_basis: str | None = None) -> dict:
    """Build the field payload for an EXTRACT_COMMIT record.

    Everything an auditor needs to answer, for any row: who pulled it, when,
    from where, under which policy, which predicate selected it, where it
    landed, and whether it was masked.
    """
    return dict(
        run_id=run_id, batch_id=batch_id,
        actor={"agent_svid": agent_svid, "owner_principal": owner},
        source={"connection_id": source.connection_id, "kind": source.kind.value,
                "endpoint_fingerprint": source.endpoint_fingerprint(),
                "object": object_name},
        extraction={"strategy": strategy, "predicate_hash": predicate_hash,
                    "columns": list(columns), "masked_columns": masked,
                    "position_from": position_from.to_json() if position_from else None,
                    "position_to": position_to.to_json() if position_to else None},
        volume={"row_count": row_count, "byte_count": byte_count,
                "query_seconds": query_seconds},
        destination={"uri": sink_uri, "content_sha256": content_sha256,
                     "dek_key_id": dek_key_id},
        governance={"policy_version": policy_version, "schema_version": schema_version,
                    "classification": classification, "legal_basis": legal_basis},
    )
