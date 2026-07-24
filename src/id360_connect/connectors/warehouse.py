"""Warehouse connector — Snowflake and BigQuery.

THE COST ARGUMENT, RESTATED
---------------------------
A 500 GB extract through a JDBC cursor can hold an X-Small warehouse for six
hours. The same extract via COPY INTO is roughly ten minutes of compute plus a
file read you do on your own time. Roughly an order of magnitude difference in
the PROVIDER's bill, for the same bytes delivered.

So the default path is: engine writes Parquet to a stage in one short parallel
burst, then goes idle; we read the files at our own pace.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from ..core.errors import BudgetExceeded, ConfigError, SourceTimeout
from ..core.logging import get_logger, log, predicate_hash
from ..core.models import ExtractionTask, Op, Position, Strategy
from ..core.schema import Field, Schema
from ..core.state import composite_watermark, parse_composite
from .base import Connector, ReadResult

logger = get_logger(__name__)
SAFETY_LAG_SECONDS = 120


@dataclass
class SnowflakeOptions:
    warehouse: str = "ID360_XS"           # dedicated, never shared with prod
    stage: str = "@ID360_STAGE"
    role: str = "ID360_READER"
    max_file_bytes: int = 128 * 1024 * 1024
    query_tag_prefix: str = "id360"


class SnowflakeConnector(Connector):
    """Strategy 5b — bulk unload to a stage, then read the files."""

    kind = "snowflake"

    def __init__(self, source, credential, *, options: SnowflakeOptions | None = None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or SnowflakeOptions(**dict(source.options))
        self._conn = None
        self._query_id: str | None = None

    # ------------------------------------------------------------ lifecycle --
    def connect(self) -> None:
        import snowflake.connector as sf
        self._conn = sf.connect(
            account=self.source.endpoint,
            user=self.credential.get("username"),
            private_key=self.credential.get("private_key"),   # keypair, not password
            role=self.options.role,
            warehouse=self.options.warehouse,
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS":
                    self.source.budget.statement_timeout_seconds,
                # Never let a runaway query sit in a queue burning credits.
                "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": 60,
            })

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def cancel(self) -> None:
        if self._query_id:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT SYSTEM$CANCEL_QUERY('{self._query_id}')")

    # -------------------------------------------------------------- metadata --
    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        with self._conn.cursor() as cur:
            cur.execute(f"DESCRIBE TABLE {self._qualify(object_name)}")
            fields = [Field(name=r[0], type=r[1], nullable=(r[3] == "Y"))
                      for r in cur.fetchall()]
        return Schema(fields=tuple(fields))

    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        """EXPLAIN gives estimated partitions/bytes without running the query."""
        sql = self._build_select(task)
        with self._conn.cursor() as cur:
            cur.execute(f"EXPLAIN USING JSON {sql}")
            plan = cur.fetchone()[0]
        try:
            import json
            return int(json.loads(plan)["GlobalStats"]["bytesAssigned"])
        except Exception:                               # noqa: BLE001
            return None

    # ------------------------------------------------------------------ read --
    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        estimated = self.estimate_bytes(task)
        if estimated:
            self.governor.check_estimate(estimated)     # refuse before running

        select_sql = self._build_select(task)
        run_path = f"{self.options.stage}/{task.object_name}/run={task.run_id}/"

        unload = f"""
            COPY INTO {run_path}
            FROM ({select_sql})
            FILE_FORMAT = (TYPE = PARQUET COMPRESSION = SNAPPY)
            MAX_FILE_SIZE = {self.options.max_file_bytes}
            HEADER = TRUE
            OVERWRITE = TRUE
        """

        self.governor.before_query()
        started = time.monotonic()
        log(logger, 20, "unloading to stage",
            object_name=task.object_name, strategy=task.strategy.value,
            predicate_hash=predicate_hash(select_sql), estimated_bytes=estimated or 0)

        with self._conn.cursor() as cur:
            # Query tag: the provider can filter their own billing telemetry to
            # "what did ID360 cost me". Do not make them reverse-engineer it.
            cur.execute(
                f"ALTER SESSION SET QUERY_TAG = "
                f"'{self.options.query_tag_prefix}:{task.run_id}:{task.task_id}'")
            try:
                cur.execute(unload)
                self._query_id = cur.sfqid
                rows = cur.fetchall()
            except Exception as exc:                    # noqa: BLE001
                if "timeout" in str(exc).lower():
                    self.cancel()
                    raise SourceTimeout("unload timed out; partition will be split",
                                        detail=str(exc)) from exc
                raise
        query_seconds = time.monotonic() - started
        self.governor.record_query_seconds(query_seconds)

        # The warehouse is now IDLE. Everything below runs on our own compute.
        total_bytes = sum(int(r[2]) for r in rows) if rows else 0
        self.governor.record_bytes(total_bytes)

        for stage_file in (r[0] for r in rows):
            records = self._read_stage_file(f"{run_path}{stage_file}")
            self.governor.record_rows(len(records))
            yield ReadResult(records=records,
                             op=Op.REFRESH if task.strategy is Strategy.FULL_SNAPSHOT
                             else Op.UPDATE,
                             source_bytes=total_bytes,
                             query_seconds=query_seconds,
                             position=self._new_position(task, records))

    # -------------------------------------------------------------- helpers --
    def _build_select(self, task: ExtractionTask) -> str:
        schema = self.fetch_schema(task.object_name)
        policy = self.policy_for(task.object_name)
        columns = self.projected_columns(task.object_name, schema)
        parts = [f'"{c}"' for c in columns]
        for column, rule in policy.masked_columns.items():
            parts.append(self.masked_expression(f'"{column}"', rule))

        where = []
        caps = self.source.capabilities
        if task.strategy is Strategy.WATERMARK and caps.watermark_column:
            pk = (caps.primary_key or ("ID",))[0]
            hwm_ts, hwm_pk = parse_composite(task.since)
            if hwm_ts is not None:
                where.append(f"({caps.watermark_column}, {pk}) > "
                             f"('{hwm_ts}', '{hwm_pk}')")
            where.append(f"{caps.watermark_column} <= "
                         f"DATEADD(second, -{SAFETY_LAG_SECONDS}, CURRENT_TIMESTAMP())")
        if policy.row_filter:
            where.append(f"({policy.row_filter})")

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return f'SELECT {", ".join(parts)} FROM {self._qualify(task.object_name)}{clause}'

    def _read_stage_file(self, path: str) -> list[dict[str, Any]]:
        """Read a Parquet file from the stage.

        Prefer reading directly from the underlying bucket with our own storage
        credentials - GET through the Snowflake client routes bytes through
        their infrastructure and can attract egress charges on their side.
        """
        raise NotImplementedError("wire to the external stage's object store")

    def _new_position(self, task, records) -> Position | None:
        caps = self.source.capabilities
        if task.strategy is not Strategy.WATERMARK or not records or not caps.watermark_column:
            return None
        last = records[-1]
        return composite_watermark(last[caps.watermark_column],
                                   last[(caps.primary_key or ("ID",))[0]])

    @staticmethod
    def _qualify(object_name: str) -> str:
        return ".".join('"' + p.replace('"', '""') + '"'
                        for p in object_name.split("."))


# --------------------------------------------------------------------------- #
# BigQuery
# --------------------------------------------------------------------------- #
@dataclass
class BigQueryOptions:
    project: str = ""
    use_storage_api: bool = True          # free-tier friendly and fast
    max_bytes_billed: int = 100 * 1024 ** 3
    location: str = "EU"


class BigQueryConnector(Connector):
    """Strategy 5c primarily — Storage Read API streams Arrow directly.

    BigQuery bills by BYTES SCANNED, so the levers are different from
    Snowflake: a partition filter is mandatory, not optional, and `SELECT *` on
    a wide table is a direct bill multiplier.
    """

    kind = "bigquery"

    def __init__(self, source, credential, *, options: BigQueryOptions | None = None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or BigQueryOptions(**dict(source.options))
        self._client = None

    def connect(self) -> None:
        from google.cloud import bigquery
        self._client = bigquery.Client(project=self.options.project)

    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        table = self._client.get_table(object_name)
        return Schema(fields=tuple(
            Field(name=f.name, type=f.field_type, nullable=(f.mode != "REQUIRED"))
            for f in table.schema))

    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        """dry_run returns the EXACT bytes billed, for free. There is no excuse
        for ever running an unpriced BigQuery query."""
        from google.cloud import bigquery
        sql = self._build_select(task)
        job = self._client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        return int(job.total_bytes_processed)

    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        from google.cloud import bigquery

        estimated = self.estimate_bytes(task)
        self.governor.check_estimate(estimated)
        if estimated > self.options.max_bytes_billed:
            raise BudgetExceeded("query exceeds max_bytes_billed",
                                 estimated_bytes=estimated,
                                 cap=self.options.max_bytes_billed)

        sql = self._build_select(task)
        config = bigquery.QueryJobConfig(
            maximum_bytes_billed=self.options.max_bytes_billed,
            labels={"app": "id360", "run_id": task.run_id[-32:].lower()},
        )
        self.governor.before_query()
        started = time.monotonic()
        job = self._client.query(sql, job_config=config, location=self.options.location)

        try:
            # to_arrow_iterable uses the Storage Read API when available -
            # columnar, streamed, and far cheaper than paging rows.
            for arrow_batch in job.result().to_arrow_iterable():
                records = arrow_batch.to_pylist()
                self.governor.record_rows(len(records))
                yield ReadResult(records=records, op=Op.UPDATE,
                                 source_bytes=job.total_bytes_processed or 0,
                                 position=self._new_position(task, records))
        finally:
            self.governor.record_query_seconds(time.monotonic() - started)
            self.governor.record_bytes(job.total_bytes_processed or 0)

    def cancel(self) -> None:
        # jobs.cancel - dropping the client is NOT enough; the job keeps running
        # and the provider keeps paying.
        ...

    def _build_select(self, task: ExtractionTask) -> str:
        schema = self.fetch_schema(task.object_name)
        policy = self.policy_for(task.object_name)
        columns = self.projected_columns(task.object_name, schema)
        parts = [f"`{c}`" for c in columns]
        for column, rule in policy.masked_columns.items():
            parts.append(self.masked_expression(f"`{column}`", rule))

        where = []
        caps = self.source.capabilities
        if task.strategy is Strategy.WATERMARK and caps.watermark_column:
            hwm_ts, _ = parse_composite(task.since)
            if hwm_ts:
                # Filter on the PARTITION column, not just the change column -
                # on BigQuery this is the difference between scanning a day and
                # scanning the table.
                where.append(f"_PARTITIONTIME >= TIMESTAMP('{hwm_ts}')")
                where.append(f"{caps.watermark_column} > TIMESTAMP('{hwm_ts}')")
            where.append(f"{caps.watermark_column} <= "
                         f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
                         f"INTERVAL {SAFETY_LAG_SECONDS} SECOND)")
        if policy.row_filter:
            where.append(f"({policy.row_filter})")

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return f'SELECT {", ".join(parts)} FROM `{task.object_name}`{clause}'

    def _new_position(self, task, records) -> Position | None:
        caps = self.source.capabilities
        if task.strategy is not Strategy.WATERMARK or not records:
            return None
        last = records[-1]
        return composite_watermark(last[caps.watermark_column],
                                   last[(caps.primary_key or ("id",))[0]])
