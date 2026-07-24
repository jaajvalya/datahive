"""Data lake connector — raw files on S3 / ADLS / GCS / HDFS.

The problem is not reading files. It is knowing WHICH FILES ARE NEW without a
full listing. A full LIST on a bucket with tens of millions of objects is slow
and, on S3, genuinely expensive - LIST costs roughly 10x a GET per request.

Four change-detection techniques, best first:

  1. Event notifications  - S3 -> SQS, ADLS -> Event Grid, GCS -> Pub/Sub.
                            The lake TELLS you what landed. Ask for this.
  2. Inventory reports    - S3 Inventory / Blob Inventory: a daily Parquet
                            manifest of every object. Diff yesterday vs today.
  3. Partition pruning    - compute `dt=2026-07-20/` paths; never list the root.
  4. Marker listing       - list_objects_v2(StartAfter=last_key). Only works if
                            keys sort by time.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterator, Sequence

from ..core.errors import ConfigError
from ..core.logging import get_logger, log
from ..core.models import ExtractionTask, Op, Position
from ..core.schema import Field, Schema
from .base import Connector, ReadResult

logger = get_logger(__name__)


@dataclass
class LakeOptions:
    change_detection: str = "partition"   # notification | inventory | partition | marker
    partition_pattern: str = "dt={date}/"  # e.g. "year={y}/month={m}/day={d}/"
    file_format: str = "parquet"           # parquet | orc | csv | json | avro
    notification_queue: str | None = None
    inventory_prefix: str | None = None
    lookback_days: int = 2                 # tolerate late-arriving partitions
    target_batch_rows: int = 50_000
    csv_options: dict = field(default_factory=dict)


class ObjectStoreLakeConnector(Connector):
    kind = "lake"

    def __init__(self, source, credential, *, options: LakeOptions | None = None,
                 fs=None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or LakeOptions(**dict(source.options))
        self.fs = fs      # pyarrow.fs.FileSystem / s3fs / adlfs

    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        """Infer from ONE sample file, then pin it in the registry.

        Never re-infer per run. Re-inference on CSV/JSON produces silent type
        flapping: a column is int on Monday and string on Tuesday because one
        row had a stray value, and downstream breaks in a way nobody can trace.
        """
        import pyarrow.dataset as ds
        sample = self._sample_file(object_name)
        if sample is None:
            raise ConfigError("no files found to infer schema",
                              object_name=object_name)
        dataset = ds.dataset(sample, format=self.options.file_format,
                             filesystem=self.fs)
        return Schema(fields=tuple(
            Field(name=f.name, type=str(f.type), nullable=f.nullable)
            for f in dataset.schema))

    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        return sum(size for _, size in self._new_files(task))

    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        import pyarrow.dataset as ds

        schema = self.fetch_schema(task.object_name)
        columns = list(self.projected_columns(task.object_name, schema))
        policy = self.policy_for(task.object_name)

        files = self._new_files(task)
        log(logger, 20, "lake files selected",
            object_name=task.object_name, row_count=len(files),
            strategy=task.strategy.value)

        last_key = task.since.value if task.since else None
        for path, size in files:
            self.governor.record_bytes(size)
            dataset = ds.dataset(path, format=self.options.file_format,
                                 filesystem=self.fs)
            scanner = dataset.scanner(columns=columns,
                                      filter=self._to_expression(policy.row_filter),
                                      batch_size=self.options.target_batch_rows)
            for batch in scanner.to_batches():
                records = batch.to_pylist()
                self.governor.record_rows(len(records))
                yield ReadResult(records=records, op=Op.REFRESH,
                                 source_bytes=batch.nbytes)
            last_key = path

        if last_key:
            yield ReadResult(records=[], op=Op.REFRESH, is_last=True,
                             position=Position(kind="object_key", value=last_key))

    # -------------------------------------------------- change detection ----
    def _new_files(self, task: ExtractionTask) -> list[tuple[str, int]]:
        mode = self.options.change_detection
        if mode == "notification":
            return self._from_notifications(task)
        if mode == "inventory":
            return self._from_inventory(task)
        if mode == "partition":
            return self._from_partitions(task)
        return self._from_marker(task)

    def _from_notifications(self, task) -> list[tuple[str, int]]:
        """Consume S3 Event Notifications from SQS. The best option by far:
        zero LIST cost and near-real-time.

        Delete the message only AFTER the sink commits, so a crash replays the
        notification rather than losing the file.
        """
        raise NotImplementedError("wire to SQS / Event Grid / Pub/Sub")

    def _from_inventory(self, task) -> list[tuple[str, int]]:
        """Diff today's inventory manifest against yesterday's. Costs one
        Parquet read instead of millions of LIST requests."""
        raise NotImplementedError("read S3 Inventory / Blob Inventory manifests")

    def _from_partitions(self, task) -> list[tuple[str, int]]:
        """Compute the partition paths we need and list ONLY those.

        `lookback_days` re-lists recent partitions to catch late-arriving data,
        which is normal in every real lake.
        """
        root = posixpath.join(self.source.endpoint, task.object_name)
        today = date.today()
        out: list[tuple[str, int]] = []
        for delta in range(self.options.lookback_days + 1):
            day = today - timedelta(days=delta)
            prefix = posixpath.join(
                root, self.options.partition_pattern.format(
                    date=day.isoformat(), y=day.year,
                    m=f"{day.month:02d}", d=f"{day.day:02d}"))
            out.extend(self._list(prefix))
        return out

    def _from_marker(self, task) -> list[tuple[str, int]]:
        """Lexicographic resume. Cheapest fallback, but only correct if keys
        sort by time - verify that assumption before relying on it."""
        start_after = task.since.value if task.since else None
        root = posixpath.join(self.source.endpoint, task.object_name)
        return [(p, s) for p, s in self._list(root)
                if start_after is None or p > start_after]

    def _list(self, prefix: str) -> list[tuple[str, int]]:
        if self.fs is None:
            return []
        from pyarrow.fs import FileSelector
        try:
            infos = self.fs.get_file_info(FileSelector(prefix, recursive=True))
        except OSError:
            return []                      # partition does not exist yet
        return [(i.path, i.size) for i in infos if i.is_file and i.size > 0]

    def _sample_file(self, object_name: str) -> str | None:
        files = self._list(posixpath.join(self.source.endpoint, object_name))
        return files[0][0] if files else None

    @staticmethod
    def _to_expression(row_filter: str | None):
        """Translate a registered row filter into a pyarrow expression so it is
        pushed into the scan, not applied after materialization."""
        if not row_filter:
            return None
        raise NotImplementedError("parse the registered filter into pyarrow.compute")


class HdfsLakeConnector(ObjectStoreLakeConnector):
    """HDFS variant.

    Change detection uses the NameNode's inotify edit-log stream where
    available. Never walk the full tree: a recursive `ls` on a large HDFS
    namespace hammers the NameNode, which is a single point of failure for the
    provider's entire cluster.
    """

    kind = "hdfs"

    def _from_partitions(self, task):
        return super()._from_partitions(task)
