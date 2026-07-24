"""Sink protocol — one writer contract, several backends.

THE CONTRACT
------------
`write_batch` + `commit` MUST be idempotent on `batch_id`. A replayed batch
overwrites byte-identically or is a no-op. This is what makes at-least-once
delivery from the agent produce effectively-exactly-once at the table level.

The sink path is DERIVED, never random:

    {root}/{connection_id}/{object}/_run={run_id}/{batch_id}.parquet

Deterministic path + idempotent commit = safe replay after any crash.
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..core.models import BatchMeta


@dataclass
class WriteResult:
    uri: str
    byte_count: int
    row_count: int
    content_sha256: str
    dek_key_id: str | None = None


class Sink(abc.ABC):
    name: str = "sink"

    @abc.abstractmethod
    def open(self, meta: BatchMeta) -> None:
        """Prepare for a run. Create staging, begin a transaction, etc."""

    @abc.abstractmethod
    def write_batch(self, records: Iterable[Mapping[str, Any]],
                    meta: BatchMeta) -> WriteResult:
        """Write one batch. Must be safe to call twice with the same batch_id."""

    @abc.abstractmethod
    def commit(self, meta: BatchMeta) -> None:
        """Make the batch visible. Must be idempotent on meta.batch_id."""

    @abc.abstractmethod
    def abort(self, meta: BatchMeta) -> None:
        """Discard uncommitted work for this run."""

    # -- shared helpers ---------------------------------------------------- #
    @staticmethod
    def object_path(meta: BatchMeta, *, root: str = "", ext: str = "parquet") -> str:
        safe_object = meta.object_name.replace("/", "_").replace(" ", "_")
        prefix = root.rstrip("/") + "/" if root else ""
        return (f"{prefix}{meta.connection_id}/{safe_object}/"
                f"_run={meta.run_id}/{meta.batch_id}.{ext}")

    @staticmethod
    def checksum(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()


class TableFormat(abc.ABC):
    """Abstraction over Iceberg / Delta commit semantics.

    With a real table format the commit is an ACID transaction, so a partial
    run is simply invisible to readers - you get snapshot isolation for free
    and never expose a half-loaded table.
    """

    @abc.abstractmethod
    def ensure_table(self, table: str, schema: Sequence[Any]) -> None: ...

    @abc.abstractmethod
    def append_files(self, table: str, files: Sequence[str], *,
                     batch_id: str) -> None:
        """Append data files in a single transaction, idempotent on batch_id
        (record batch_id in snapshot properties and skip if already present)."""

    @abc.abstractmethod
    def upsert_files(self, table: str, files: Sequence[str], *,
                     keys: Sequence[str], batch_id: str) -> None: ...
