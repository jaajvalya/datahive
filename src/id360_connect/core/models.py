"""Core domain models shared by the control plane and the agent.

These types are the contract between planes. Everything that crosses the
agent <-> control-plane boundary is one of these, and none of them ever
carries bulk payload data or a secret value.
"""
from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:22]}"


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class SourceKind(str, enum.Enum):
    LAKEHOUSE = "lakehouse"
    LAKE = "lake"
    WAREHOUSE = "warehouse"
    DATABASE = "database"
    SHAREPOINT = "sharepoint"
    GOOGLE_DRIVE = "google_drive"
    ONEDRIVE = "onedrive"
    MAILBOX = "mailbox"


class Strategy(str, enum.Enum):
    """The five extraction strategies. Ordered cheapest-first in `PREFERENCE`."""
    STORAGE_NATIVE = "storage_native"   # S5 - read files / bulk unload
    LOG_CDC = "log_cdc"                 # S3 - redo/WAL/binlog
    CHANGE_FEED = "change_feed"         # S4 - delta token / change feed
    WATERMARK = "watermark"             # S2 - WHERE col > :hwm
    FULL_SNAPSHOT = "full_snapshot"     # S1 - read everything


#: Resolution order. Always pick the first strategy the source can support.
PREFERENCE: Sequence[Strategy] = (
    Strategy.STORAGE_NATIVE,
    Strategy.LOG_CDC,
    Strategy.CHANGE_FEED,
    Strategy.WATERMARK,
    Strategy.FULL_SNAPSHOT,
)


class Op(str, enum.Enum):
    INSERT = "I"
    UPDATE = "U"
    DELETE = "D"
    REFRESH = "R"      # emitted by a snapshot read


class Classification(str, enum.Enum):
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"
    INTERNAL = "internal"
    PUBLIC = "public"


class DriftPosture(str, enum.Enum):
    STRICT = "strict"
    EVOLVE = "evolve"
    PERMISSIVE = "permissive"


class RunOutcome(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DENIED = "DENIED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"


# --------------------------------------------------------------------------- #
# Position — the resume cursor, whatever shape the source uses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Position:
    """An opaque-to-the-framework resume position.

    `kind` tells the connector how to interpret `value`:
        lsn        -> Postgres LSN            "0/1A2B3C00"
        scn        -> Oracle SCN              "12345678"
        binlog     -> MySQL coordinates       "mysql-bin.000042:1547"
        snapshot   -> Iceberg/Delta version   "3481"
        delta_link -> Graph deltaLink         "https://graph.../delta?$deltatoken=..."
        page_token -> Drive page token        "CJ2..."
        watermark  -> composite (ts, pk)      {"ts": "...", "pk": "..."}
        offset     -> Kafka-style offsets     {"0": 118, "1": 92}

    Rules that the whole framework depends on:
      * Positions are NEVER parsed or synthesised by generic code.
      * A position is persisted only AFTER the sink acknowledges durability.
      * A position may never move backwards without a signed operator override.
    """
    kind: str
    value: Any
    captured_at: datetime = field(default_factory=utcnow)

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, "value": self.value},
                          sort_keys=True, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "Position":
        d = json.loads(raw)
        return cls(kind=d["kind"], value=d["value"])


# --------------------------------------------------------------------------- #
# Source registration
# --------------------------------------------------------------------------- #
@dataclass
class SourceCapabilities:
    """What a registered source can actually do.

    Populated at onboarding by a probe, then reviewed by a human. The strategy
    resolver reads only this - it never guesses from the source kind.
    """
    supports_storage_native: bool = False
    supports_log_cdc: bool = False
    supports_change_feed: bool = False
    watermark_column: str | None = None
    watermark_indexed: bool = False          # verified via EXPLAIN at onboarding
    primary_key: Sequence[str] = ()
    supports_bulk_unload: bool = False
    unload_stage_uri: str | None = None
    read_replica_available: bool = False
    dry_run_estimation: bool = False


@dataclass
class Budget:
    """Multi-axis budget. Rate limits stop spikes; cumulative caps stop leaks.
    You need both."""
    max_rows_per_second: float | None = None
    max_bytes_per_second: float | None = None
    max_queries_per_minute: float | None = None
    max_api_calls_per_minute: float | None = None
    max_bytes_per_day: int | None = None
    max_query_seconds_per_day: float | None = None
    max_concurrency: int = 4
    statement_timeout_seconds: int = 300


@dataclass
class ObjectPolicy:
    """Per-object contract. Masking and row filters are pushed INTO the source
    query - the framework never fetches a column it is not allowed to hold."""
    object_name: str
    columns: Sequence[str] = ()                     # empty = all permitted columns
    masked_columns: Mapping[str, str] = field(default_factory=dict)  # col -> rule id
    row_filter: str | None = None                   # validated SQL fragment
    classification: Classification = Classification.INTERNAL
    drift_posture: DriftPosture = DriftPosture.EVOLVE


@dataclass
class SourceRef:
    """A registered connection. Note `secret_ref`: a pointer, never a value."""
    connection_id: str
    tenant_id: str
    kind: SourceKind
    display_name: str
    endpoint: str                     # host/uri; fingerprinted for the audit ledger
    secret_ref: str                   # e.g. "vault://tenant-a/kv/id360/pg-ro"
    capabilities: SourceCapabilities = field(default_factory=SourceCapabilities)
    budget: Budget = field(default_factory=Budget)
    objects: Mapping[str, ObjectPolicy] = field(default_factory=dict)
    blackout_cron: Sequence[str] = ()
    options: Mapping[str, Any] = field(default_factory=dict)
    policy_version: int = 1

    def endpoint_fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(self.endpoint.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Tasks and batches
# --------------------------------------------------------------------------- #
@dataclass
class ExtractionTask:
    """A unit of work leased by an agent. Small, serialisable, no secrets."""
    task_id: str
    run_id: str
    tenant_id: str
    connection_id: str
    object_name: str
    strategy: Strategy
    since: Position | None = None
    until: Position | None = None
    partition: Mapping[str, Any] | None = None   # e.g. {"index": 3, "of": 16}
    lease_expires_at: datetime | None = None
    attempt: int = 1
    policy_version: int = 1

    @classmethod
    def create(cls, *, run_id: str, source: SourceRef, object_name: str,
               strategy: Strategy, since: Position | None = None,
               partition: Mapping[str, Any] | None = None) -> "ExtractionTask":
        return cls(
            task_id=new_id("tsk"),
            run_id=run_id,
            tenant_id=source.tenant_id,
            connection_id=source.connection_id,
            object_name=object_name,
            strategy=strategy,
            since=since,
            partition=partition,
            policy_version=source.policy_version,
        )


@dataclass
class BatchMeta:
    """Accompanies every batch handed to a sink. This is what makes commits
    idempotent: the sink path is derived from (run_id, batch_id), so a replay
    rewrites the same bytes to the same place."""
    batch_id: str
    run_id: str
    tenant_id: str
    connection_id: str
    object_name: str
    strategy: Strategy
    schema_version: int
    classification: Classification
    position_from: Position | None
    position_to: Position | None
    row_count: int = 0
    byte_count: int = 0
    sequence: int = 0

    @classmethod
    def create(cls, task: ExtractionTask, *, schema_version: int,
               classification: Classification, sequence: int = 0) -> "BatchMeta":
        return cls(
            batch_id=new_id("bat"),
            run_id=task.run_id,
            tenant_id=task.tenant_id,
            connection_id=task.connection_id,
            object_name=task.object_name,
            strategy=task.strategy,
            schema_version=schema_version,
            classification=classification,
            position_from=task.since,
            position_to=task.until,
            sequence=sequence,
        )


# --------------------------------------------------------------------------- #
# Canonical record envelope
# --------------------------------------------------------------------------- #
ENVELOPE_PREFIX = "_id360_"


def envelope_columns() -> Sequence[str]:
    return (
        "_id360_source_id", "_id360_object", "_id360_op",
        "_id360_extract_ts", "_id360_source_ts", "_id360_position",
        "_id360_batch_id", "_id360_run_id", "_id360_schema_ver",
        "_id360_row_hash", "_id360_pii_class",
    )


def row_hash(record: Mapping[str, Any]) -> str:
    """Stable hash over business columns only. Used for reconciliation, so it
    must exclude envelope columns and must be order-independent."""
    business = {k: v for k, v in record.items() if not k.startswith(ENVELOPE_PREFIX)}
    canonical = json.dumps(business, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def wrap(record: Mapping[str, Any], *, meta: BatchMeta, op: Op,
         source_ts: datetime | None = None,
         position: Position | None = None) -> dict[str, Any]:
    """Attach the canonical provenance envelope to a source record."""
    out = dict(record)
    out.update({
        "_id360_source_id": meta.connection_id,
        "_id360_object": meta.object_name,
        "_id360_op": op.value,
        "_id360_extract_ts": utcnow(),
        "_id360_source_ts": source_ts,
        "_id360_position": position.to_json() if position else None,
        "_id360_batch_id": meta.batch_id,
        "_id360_run_id": meta.run_id,
        "_id360_schema_ver": meta.schema_version,
        "_id360_row_hash": row_hash(record),
        "_id360_pii_class": meta.classification.value,
    })
    return out


def wrap_many(records: Iterable[Mapping[str, Any]], *, meta: BatchMeta, op: Op,
              position: Position | None = None) -> Iterable[dict[str, Any]]:
    for r in records:
        yield wrap(r, meta=meta, op=op, position=position)
