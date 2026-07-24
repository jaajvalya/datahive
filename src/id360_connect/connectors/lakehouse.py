"""Lakehouse and lake connectors — Strategy 5a, zero-copy.

The table format's metadata IS an API. Read the manifest, get the file list,
read those Parquet files straight from object storage. The query engine is
never involved: zero warehouse credits, zero cluster time.

Two things that are easy to get wrong and expensive to get wrong:

1. DELETE FILES. Iceberg positional/equality deletes and Delta deletion
   vectors must be applied, or you resurrect deleted rows. Skip this and you
   are reading a DIFFERENT TABLE than the engine sees. This is the most common
   bug in hand-rolled lakehouse readers.

2. GOVERNANCE BYPASS. If row/column security is enforced at the engine layer,
   reading files directly bypasses the provider's security controls - a
   compliance problem even where it is technically permitted. Ask; do not
   assume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from ..core.errors import ConfigError, PolicyDenied
from ..core.logging import get_logger, log
from ..core.models import ExtractionTask, Op, Position, Strategy
from ..core.schema import Field, Schema
from .base import Connector, ReadResult

logger = get_logger(__name__)


@dataclass
class IcebergOptions:
    catalog_name: str = "default"
    catalog_uri: str = ""                 # REST / Glue / Hive / Nessie
    target_batch_rows: int = 50_000
    apply_deletes: bool = True            # do not turn this off


class IcebergConnector(Connector):
    kind = "iceberg"

    def __init__(self, source, credential, *, options: IcebergOptions | None = None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or IcebergOptions(**dict(source.options))
        self._catalog = None

    def connect(self) -> None:
        from pyiceberg.catalog import load_catalog
        # Where the catalog vends storage credentials (Unity Catalog, Lake
        # Formation), use them: scoped and short-lived, for free.
        self._catalog = load_catalog(self.options.catalog_name,
                                     uri=self.options.catalog_uri,
                                     **dict(self.credential.values))

    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        table = self._catalog.load_table(object_name)
        return Schema(fields=tuple(
            Field(name=f.name, type=str(f.field_type), nullable=not f.required)
            for f in table.schema().fields))

    def current_position(self, object_name: str) -> Position | None:
        table = self._catalog.load_table(object_name)
        snapshot = table.current_snapshot()
        return Position(kind="snapshot", value=str(snapshot.snapshot_id)) if snapshot else None

    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        """EXACT, and computed before reading anything: sum the file sizes left
        after partition and statistics pruning."""
        files = self._plan_files(task)
        return sum(f.file_size_in_bytes for f in files)

    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        table = self._catalog.load_table(task.object_name)
        policy = self.policy_for(task.object_name)
        schema = self.fetch_schema(task.object_name)
        columns = list(self.projected_columns(task.object_name, schema))

        if policy.masked_columns:
            # Direct file reads cannot apply a masking expression, so a masked
            # column must simply never be projected. If the contract requires a
            # transform rather than exclusion, that object must go through the
            # engine instead - fetch-then-mask is not a control.
            columns = [c for c in columns if c not in policy.masked_columns]

        if task.strategy is Strategy.CHANGE_FEED and task.since:
            scan = table.incremental_append_scan(
                from_snapshot_id_exclusive=int(task.since.value),
                to_snapshot_id=int(task.until.value) if task.until else None,
            ).select(*columns)
            op = Op.INSERT
        else:
            scan = table.scan(selected_fields=tuple(columns),
                              row_filter=policy.row_filter or None)
            op = Op.REFRESH

        current = table.current_snapshot()
        position = Position(kind="snapshot",
                            value=str(current.snapshot_id)) if current else None

        total = 0
        # to_arrow_batch_reader applies delete files for us. Reading the raw
        # Parquet paths and skipping this is the resurrection bug.
        for batch in scan.to_arrow_batch_reader():
            records = batch.to_pylist()
            total += len(records)
            self.governor.record_rows(len(records))
            self.governor.record_bytes(batch.nbytes)
            yield ReadResult(records=records, op=op, position=position,
                             source_bytes=batch.nbytes)

        log(logger, 20, "iceberg scan complete",
            object_name=task.object_name, row_count=total,
            strategy=task.strategy.value)

    def _plan_files(self, task: ExtractionTask):
        table = self._catalog.load_table(task.object_name)
        policy = self.policy_for(task.object_name)
        return list(table.scan(row_filter=policy.row_filter or None).plan_files())


@dataclass
class DeltaOptions:
    storage_options: dict = None
    use_change_data_feed: bool = True


class DeltaConnector(Connector):
    """Delta Lake via delta-rs (no Spark, no cluster)."""

    kind = "delta"

    def __init__(self, source, credential, *, options: DeltaOptions | None = None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or DeltaOptions(**dict(source.options))

    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def _table(self, object_name: str, version: int | None = None):
        from deltalake import DeltaTable
        uri = f"{self.source.endpoint.rstrip('/')}/{object_name}"
        return DeltaTable(uri, version=version,
                          storage_options=self.options.storage_options
                          or dict(self.credential.values))

    def fetch_schema(self, object_name: str) -> Schema:
        dt = self._table(object_name)
        return Schema(fields=tuple(
            Field(name=f.name, type=str(f.type), nullable=f.nullable)
            for f in dt.schema().fields))

    def current_position(self, object_name: str) -> Position | None:
        return Position(kind="snapshot", value=str(self._table(object_name).version()))

    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        dt = self._table(task.object_name)
        return sum(a.get("size_bytes", 0) for a in dt.get_add_actions().to_pylist())

    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        dt = self._table(task.object_name)
        schema = self.fetch_schema(task.object_name)
        columns = list(self.projected_columns(task.object_name, schema))
        version = dt.version()

        if (task.strategy is Strategy.CHANGE_FEED and task.since
                and self.options.use_change_data_feed):
            # Requires delta.enableChangeDataFeed=true on the source table.
            # If it is off, ask the provider to enable it - it is a one-line
            # table property and it converts a full scan into a delta read.
            reader = dt.load_cdf(starting_version=int(task.since.value) + 1,
                                 ending_version=version)
            op_column = "_change_type"
        else:
            reader = dt.to_pyarrow_dataset().scanner(columns=columns).to_reader()
            op_column = None

        position = Position(kind="snapshot", value=str(version))
        for batch in reader:
            records = batch.to_pylist()
            self.governor.record_rows(len(records))
            self.governor.record_bytes(batch.nbytes)
            yield ReadResult(records=records,
                             op=self._map_op(records, op_column),
                             position=position, source_bytes=batch.nbytes)

    @staticmethod
    def _map_op(records, op_column: str | None) -> Op:
        if not op_column or not records:
            return Op.REFRESH
        mapping = {"insert": Op.INSERT, "update_postimage": Op.UPDATE,
                   "delete": Op.DELETE}
        return mapping.get(records[0].get(op_column, ""), Op.UPDATE)
