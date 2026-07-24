"""State store — watermarks, positions, leases, schema versions.

All durable state lives in the control plane, not the agent. Agents are cattle:
kill one mid-run and another resumes from the last committed checkpoint.

THE INVARIANT THAT MATTERS MOST
-------------------------------
The position advances only AFTER the sink acknowledges durability. Never the
other way round. This is the single most common correctness bug in hand-rolled
connectors, and it is silent - you lose data and nothing errors.
"""
from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from .errors import PositionRegression
from .models import ExtractionTask, Position, utcnow


@dataclass
class Checkpoint:
    tenant_id: str
    connection_id: str
    object_name: str
    position: Position | None = None
    schema_version: int = 1
    updated_at: datetime = field(default_factory=utcnow)
    last_run_id: str | None = None
    #: Batches already committed for the current run, for idempotent replay.
    committed_batches: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.connection_id, self.object_name)


@dataclass
class Lease:
    task_id: str
    agent_id: str
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at


class StateStore(abc.ABC):
    @abc.abstractmethod
    def get_checkpoint(self, tenant_id: str, connection_id: str,
                       object_name: str) -> Checkpoint: ...

    @abc.abstractmethod
    def commit(self, cp: Checkpoint, *, batch_id: str,
               new_position: Position | None, allow_regression: bool = False) -> Checkpoint: ...

    @abc.abstractmethod
    def acquire_lease(self, task: ExtractionTask, agent_id: str,
                      ttl_seconds: int = 300) -> Lease | None: ...

    @abc.abstractmethod
    def renew_lease(self, task_id: str, agent_id: str, ttl_seconds: int = 300) -> bool: ...

    @abc.abstractmethod
    def release_lease(self, task_id: str, agent_id: str) -> None: ...


class InMemoryStateStore(StateStore):
    """Reference implementation.

    Production: Postgres. `commit` becomes a single transaction doing
        SELECT ... FOR UPDATE on the checkpoint row
        + INSERT into committed_batches (unique on batch_id -> idempotency)
        + UPDATE position
        + INSERT audit record
    All or nothing. The unique constraint on batch_id is what makes a replayed
    batch a no-op rather than a duplicate.
    """

    def __init__(self) -> None:
        self._cps: dict[tuple[str, str, str], Checkpoint] = {}
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()

    def get_checkpoint(self, tenant_id, connection_id, object_name) -> Checkpoint:
        with self._lock:
            key = (tenant_id, connection_id, object_name)
            if key not in self._cps:
                self._cps[key] = Checkpoint(tenant_id, connection_id, object_name)
            return self._cps[key]

    def commit(self, cp: Checkpoint, *, batch_id: str,
               new_position: Position | None,
               allow_regression: bool = False) -> Checkpoint:
        with self._lock:
            current = self._cps.setdefault(cp.key, cp)

            # Idempotent replay: a batch that already committed is a no-op.
            if batch_id in current.committed_batches:
                return current

            if new_position is not None and current.position is not None:
                if not allow_regression and _regresses(current.position, new_position):
                    raise PositionRegression(
                        "refusing to move the position backwards without an "
                        "operator override",
                        object_name=cp.object_name,
                        current=current.position.to_json(),
                        proposed=new_position.to_json())

            current.committed_batches.add(batch_id)
            if new_position is not None:
                current.position = new_position
            current.updated_at = utcnow()
            current.last_run_id = cp.last_run_id or current.last_run_id
            return current

    # -- leases ------------------------------------------------------------ #
    def acquire_lease(self, task, agent_id, ttl_seconds=300) -> Lease | None:
        with self._lock:
            existing = self._leases.get(task.task_id)
            if existing and not existing.expired():
                return None
            lease = Lease(task.task_id, agent_id,
                          utcnow() + timedelta(seconds=ttl_seconds))
            self._leases[task.task_id] = lease
            return lease

    def renew_lease(self, task_id, agent_id, ttl_seconds=300) -> bool:
        with self._lock:
            lease = self._leases.get(task_id)
            if not lease or lease.agent_id != agent_id or lease.expired():
                return False
            lease.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
            return True

    def release_lease(self, task_id, agent_id) -> None:
        with self._lock:
            lease = self._leases.get(task_id)
            if lease and lease.agent_id == agent_id:
                del self._leases[task_id]


def _regresses(current: Position, proposed: Position) -> bool:
    """Best-effort ordering check.

    Positions are opaque, so this only compares kinds it understands. Unknown
    kinds are allowed through - the connector owns ordering semantics for its
    own cursor type and enforces them itself.
    """
    if current.kind != proposed.kind:
        return False
    if current.kind in ("lsn", "scn", "snapshot", "offset_int"):
        try:
            return _numeric(proposed.value) < _numeric(current.value)
        except (TypeError, ValueError):
            return False
    if current.kind == "watermark":
        try:
            return str(proposed.value.get("ts")) < str(current.value.get("ts"))
        except AttributeError:
            return False
    return False


def _numeric(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    if "/" in s:                       # Postgres LSN "0/1A2B3C00"
        hi, lo = s.split("/", 1)
        return float(int(hi, 16) << 32 | int(lo, 16))
    return float(int(s))


# --------------------------------------------------------------------------- #
# Watermark helpers
# --------------------------------------------------------------------------- #
def bounded_window(position: Position | None, *, safety_lag_seconds: int = 120
                   ) -> tuple[Any, datetime]:
    """Return (lower_bound, upper_bound) for a watermark read.

    The upper bound is `now - safety_lag`, NOT `now`. A transaction that began
    before the read but commits after it carries a timestamp inside the window
    that you would otherwise never see. `safety_lag` must exceed the source's
    longest expected transaction.
    """
    upper = utcnow() - timedelta(seconds=safety_lag_seconds)
    lower = position.value if position else None
    return lower, upper


def composite_watermark(ts: Any, pk: Any) -> Position:
    """Composite (timestamp, primary key) watermark.

    A strict `>` on a non-unique timestamp column silently skips rows when a
    batch fails midway through a group of rows sharing the same timestamp.
    The composite form makes the predicate a total order:

        WHERE (updated_at, id) > (:hwm_ts, :hwm_pk)
    """
    return Position(kind="watermark", value={"ts": str(ts), "pk": str(pk)})


def parse_composite(position: Position | None) -> tuple[Any, Any]:
    if not position or position.kind != "watermark":
        return None, None
    v: Mapping[str, Any] = position.value
    return v.get("ts"), v.get("pk")
