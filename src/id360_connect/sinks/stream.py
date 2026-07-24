"""Kafka sink.

Idempotency comes from the producer, not from the application:

  * `enable.idempotence=true` + `acks=all` gives exactly-once *per producer
    session* against broker-side retries.
  * A transactional producer with a stable `transactional.id` derived from
    (connection, object, partition) makes the batch commit atomic.
  * Message key = business primary key, so log compaction keeps the latest
    state per entity and a replay converges.

The `_id360_*` envelope travels in headers, not the value, so a consumer can
route and filter without deserializing the payload.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..core.errors import CommitFailed, SinkUnavailable
from ..core.models import BatchMeta
from .base import Sink, WriteResult


@dataclass
class StreamConfig:
    topic_template: str = "id360.{connection_id}.{object}"
    key_fields: Sequence[str] = ()
    transactional: bool = True
    linger_ms: int = 50
    compression: str = "zstd"


class StreamSink(Sink):
    name = "stream"

    def __init__(self, producer, config: StreamConfig):
        self.producer = producer      # confluent_kafka.Producer, idempotence on
        self.config = config
        self._in_txn = False

    def open(self, meta: BatchMeta) -> None:
        if self.config.transactional and not self._in_txn:
            self.producer.begin_transaction()
            self._in_txn = True

    def write_batch(self, records: Iterable[Mapping[str, Any]],
                    meta: BatchMeta) -> WriteResult:
        topic = self.config.topic_template.format(
            connection_id=meta.connection_id,
            object=meta.object_name.replace(".", "_"))
        count = size = 0
        try:
            for record in records:
                key = self._key(record)
                value = json.dumps(
                    {k: v for k, v in record.items()
                     if not k.startswith("_id360_")}, default=str).encode()
                self.producer.produce(
                    topic=topic, key=key, value=value,
                    headers=self._headers(record, meta))
                count += 1
                size += len(value) + (len(key) if key else 0)
            self.producer.flush()
        except Exception as exc:                        # noqa: BLE001
            raise SinkUnavailable("kafka produce failed", detail=str(exc)) from exc

        return WriteResult(uri=f"kafka://{topic}", byte_count=size,
                           row_count=count, content_sha256=self.checksum(b""))

    def commit(self, meta: BatchMeta) -> None:
        if not self.config.transactional:
            return
        try:
            self.producer.commit_transaction()
            self._in_txn = False
        except Exception as exc:                        # noqa: BLE001
            raise CommitFailed("kafka transaction commit failed",
                               detail=str(exc)) from exc

    def abort(self, meta: BatchMeta) -> None:
        if self._in_txn:
            self.producer.abort_transaction()
            self._in_txn = False

    # -------------------------------------------------------------- helpers --
    def _key(self, record: Mapping[str, Any]) -> bytes | None:
        if not self.config.key_fields:
            return None
        return "|".join(str(record.get(f, "")) for f in self.config.key_fields).encode()

    @staticmethod
    def _headers(record: Mapping[str, Any], meta: BatchMeta) -> list[tuple[str, bytes]]:
        headers = [(k, str(v).encode())
                   for k, v in record.items() if k.startswith("_id360_") and v is not None]
        headers.append(("id360_batch_id", meta.batch_id.encode()))
        return headers
