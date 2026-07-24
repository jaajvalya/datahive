"""Object-store sink — Parquet on S3 / ADLS / GCS / MinIO / HDFS,
optionally registered into Iceberg or Delta.

Deliberate choices:

  * Target file size 128-512 MB. Small files are the most common self-inflicted
    performance problem in a lakehouse, and they compound - every downstream
    query pays.
  * ZSTD level 3. Better ratios than Snappy at comparable speed for most
    tabular data. Switch to Snappy if downstream readers are latency-sensitive.
  * Sort within the file on the most common filter column so min/max statistics
    actually prune.
  * Envelope encryption applied before upload; the wrapped DEK is stored beside
    the object and the key id goes into the audit record.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..core.crypto import KeyProvider, encrypt
from ..core.errors import SinkUnavailable
from ..core.models import BatchMeta
from .base import Sink, TableFormat, WriteResult


@dataclass
class ObjectStoreConfig:
    root_uri: str                          # s3://bucket/prefix
    compression: str = "zstd"
    compression_level: int = 3
    target_file_bytes: int = 256 * 1024 * 1024
    sort_by: Sequence[str] = ()
    encrypt_payload: bool = True
    table_format: str | None = None        # None | "iceberg" | "delta"


class ObjectStoreClient:
    """Thin storage abstraction so the sink is backend-agnostic.

    Implement with boto3 / azure-storage-blob / google-cloud-storage /
    pyarrow.fs.HadoopFileSystem. Keep this interface small - it is the only
    thing a new storage backend has to satisfy.
    """

    def put(self, uri: str, data: bytes, *, metadata: Mapping[str, str] | None = None) -> None:
        raise NotImplementedError

    def exists(self, uri: str) -> bool:
        raise NotImplementedError

    def delete_prefix(self, uri_prefix: str) -> None:
        raise NotImplementedError


class ObjectStoreSink(Sink):
    name = "object_store"

    def __init__(self, config: ObjectStoreConfig, client: ObjectStoreClient,
                 *, key_provider: KeyProvider | None = None,
                 table_format: TableFormat | None = None):
        self.config = config
        self.client = client
        self.key_provider = key_provider
        self.table_format = table_format
        self._pending: dict[str, list[str]] = {}   # run_id -> written uris

    # ---------------------------------------------------------------- open --
    def open(self, meta: BatchMeta) -> None:
        self._pending.setdefault(meta.run_id, [])
        if self.table_format and self.config.table_format:
            self.table_format.ensure_table(meta.object_name, [])

    # --------------------------------------------------------------- write --
    def write_batch(self, records: Iterable[Mapping[str, Any]],
                    meta: BatchMeta) -> WriteResult:
        table = self._to_arrow(records)
        if self.config.sort_by:
            table = table.sort_by([(c, "ascending") for c in self.config.sort_by
                                   if c in table.column_names])

        payload = self._to_parquet_bytes(table)
        checksum = self.checksum(payload)
        dek_key_id = None
        metadata = {
            "id360_batch_id": meta.batch_id,
            "id360_run_id": meta.run_id,
            "id360_object": meta.object_name,
            "id360_schema_version": str(meta.schema_version),
            "id360_classification": meta.classification.value,
            "id360_content_sha256": checksum,
        }

        if self.config.encrypt_payload and self.key_provider:
            dek, wrapped = self.key_provider.generate_dek(meta.tenant_id)
            # AAD binds the ciphertext to this tenant + batch, so a ciphertext
            # cannot be replayed into a different batch.
            aad = f"{meta.tenant_id}:{meta.batch_id}".encode()
            payload = encrypt(payload, dek, aad=aad)
            dek_key_id = wrapped.key_id
            metadata["id360_wrapped_dek"] = wrapped.wrapped_dek.hex()
            metadata["id360_dek_key_id"] = wrapped.key_id

        uri = self.object_path(meta, root=self.config.root_uri)

        # Idempotent replay: a deterministic path means the same batch rewrites
        # the same object. If it is already there, skip the upload entirely.
        if self.client.exists(uri):
            return WriteResult(uri=uri, byte_count=len(payload),
                               row_count=table.num_rows, content_sha256=checksum,
                               dek_key_id=dek_key_id)
        try:
            self.client.put(uri, payload, metadata=metadata)
        except Exception as exc:                       # noqa: BLE001
            raise SinkUnavailable("object store write failed",
                                  detail=str(exc), uri_prefix=self.config.root_uri) from exc

        self._pending.setdefault(meta.run_id, []).append(uri)
        return WriteResult(uri=uri, byte_count=len(payload),
                           row_count=table.num_rows, content_sha256=checksum,
                           dek_key_id=dek_key_id)

    # -------------------------------------------------------------- commit --
    def commit(self, meta: BatchMeta) -> None:
        """For a plain object-store sink the PUT is the commit.

        With a table format, register the files in a single ACID transaction -
        a partial run stays invisible to readers.
        """
        files = self._pending.get(meta.run_id, [])
        if self.table_format and files:
            self.table_format.append_files(meta.object_name, files,
                                           batch_id=meta.batch_id)
        self._pending[meta.run_id] = []

    def abort(self, meta: BatchMeta) -> None:
        """Objects for an aborted run are left in place and garbage-collected
        by lifecycle policy. Deleting on the failure path risks removing data
        that a concurrent, successful retry has already committed."""
        self._pending.pop(meta.run_id, None)

    # -------------------------------------------------------------- helpers --
    @staticmethod
    def _to_arrow(records: Iterable[Mapping[str, Any]]):
        import pyarrow as pa
        rows = list(records)
        if not rows:
            return pa.table({})
        return pa.Table.from_pylist(rows)

    def _to_parquet_bytes(self, table) -> bytes:
        import pyarrow.parquet as pq
        buf = io.BytesIO()
        pq.write_table(
            table, buf,
            compression=self.config.compression,
            compression_level=self.config.compression_level,
            use_dictionary=True,
            write_statistics=True,          # required for downstream pruning
            data_page_size=1024 * 1024,
            version="2.6",
        )
        return buf.getvalue()


class IcebergTableFormat(TableFormat):
    """Sketch using pyiceberg.

    Idempotency: record `batch_id` in the snapshot summary and skip the commit
    if a snapshot with that id already exists. That makes a replayed commit a
    no-op instead of a duplicate append.
    """

    def __init__(self, catalog):
        self.catalog = catalog

    def ensure_table(self, table: str, schema) -> None:
        if not self.catalog.table_exists(table):
            self.catalog.create_table(table, schema=schema)

    def append_files(self, table: str, files, *, batch_id: str) -> None:
        tbl = self.catalog.load_table(table)
        for snap in tbl.snapshots():
            if (snap.summary or {}).get("id360_batch_id") == batch_id:
                return                                   # already committed
        with tbl.transaction() as tx:
            tx.add_files(files, snapshot_properties={"id360_batch_id": batch_id})

    def upsert_files(self, table: str, files, *, keys, batch_id: str) -> None:
        tbl = self.catalog.load_table(table)
        with tbl.transaction() as tx:
            tx.upsert(files, join_cols=list(keys),
                      snapshot_properties={"id360_batch_id": batch_id})
