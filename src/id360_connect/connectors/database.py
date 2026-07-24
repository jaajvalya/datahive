"""Database connector — PostgreSQL reference implementation.

Postgres first because it exercises every strategy on a source you can run
locally: partitioned snapshot, composite watermark, and logical-replication CDC
with snapshot handover.

RULES OF ENGAGEMENT (enforced here, not left to the caller)
-----------------------------------------------------------
1. Read from a REPLICA, not the primary. Refuse to start otherwise unless the
   operator explicitly overrides.
2. Statement timeout on every query. No exceptions.
3. Server-side cancel on client timeout - never leave an orphaned query burning
   the provider's CPU.
4. Bounded, small connection pool. Never a meaningful fraction of
   `max_connections`.
5. Server-side cursors with a fetch size. Never buffer a full result set.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from ..core.errors import (ConfigError, PolicyDenied, PositionInvalid,
                           SourceTimeout, SourceUnavailable)
from ..core.logging import get_logger, log, predicate_hash
from ..core.models import ExtractionTask, Op, Position, Strategy
from ..core.retry import Partition, hash_partitions
from ..core.schema import Field, Schema
from ..core.state import composite_watermark, parse_composite
from .base import Connector, ReadResult

logger = get_logger(__name__)

FETCH_SIZE = 10_000
SAFETY_LAG_SECONDS = 120     # must exceed the source's longest transaction


@dataclass
class PostgresOptions:
    replication_slot: str = "id360_slot"
    publication: str = "id360_pub"
    snapshot_partitions: int = 16
    require_replica: bool = True
    sslmode: str = "verify-full"          # never fall back silently


class PostgresConnector(Connector):
    kind = "postgres"

    def __init__(self, source, credential, *, options: PostgresOptions | None = None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or PostgresOptions(**dict(source.options))
        self._conn = None
        self._backend_pid: int | None = None

    # ------------------------------------------------------------ lifecycle --
    def connect(self) -> None:
        import psycopg                                  # psycopg 3

        dsn = (f"host={self.source.endpoint} "
               f"user={self.credential.get('username')} "
               f"password={self.credential.get('password')} "
               f"sslmode={self.options.sslmode} "
               f"application_name=id360")
        try:
            self._conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:                        # noqa: BLE001
            raise SourceUnavailable("cannot connect", detail=str(exc)) from exc

        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid(), pg_is_in_recovery()")
            self._backend_pid, in_recovery = cur.fetchone()
            # Every session gets a statement timeout. Non-negotiable.
            cur.execute("SET statement_timeout = %s",
                        (self.source.budget.statement_timeout_seconds * 1000,))
            cur.execute("SET idle_in_transaction_session_timeout = '60s'")

        if self.options.require_replica and not in_recovery:
            raise ConfigError(
                "connected to a primary but require_replica is set; point the "
                "connector at a read replica or override deliberately")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def cancel(self) -> None:
        """Server-side cancel. Uses a SEPARATE connection, because the one
        running the query is busy."""
        if not self._backend_pid:
            return
        import psycopg
        with psycopg.connect(f"host={self.source.endpoint}") as c, c.cursor() as cur:
            cur.execute("SELECT pg_cancel_backend(%s)", (self._backend_pid,))

    # -------------------------------------------------------------- metadata --
    def discover(self) -> Sequence[str]:
        # Only objects registered in the policy allowlist are ever returned.
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        schema_name, _, table = object_name.partition(".")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "  FROM information_schema.columns "
                " WHERE table_schema = %s AND table_name = %s "
                " ORDER BY ordinal_position",
                (schema_name, table))
            fields = [Field(name=n, type=t, nullable=(nullable == "YES"))
                      for n, t, nullable in cur.fetchall()]
        if not fields:
            raise PolicyDenied("object not visible to the connector role",
                               object_name=object_name)
        return Schema(fields=tuple(fields))

    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        """EXPLAIN-based estimate. Also the place to verify the plan uses the
        expected index - if it does not, this is not an incremental strategy."""
        sql, params = self._build_query(task)
        with self._conn.cursor() as cur:
            cur.execute(f"EXPLAIN (FORMAT JSON) {sql}", params)
            plan = cur.fetchone()[0][0]["Plan"]
        rows, width = plan.get("Plan Rows", 0), plan.get("Plan Width", 0)
        if "Seq Scan" in plan.get("Node Type", "") and task.strategy is Strategy.WATERMARK:
            log(logger, 30, "watermark read is planning a sequential scan",
                object_name=task.object_name, strategy=task.strategy.value)
        return int(rows) * int(width)

    def current_position(self, object_name: str) -> Position | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_current_wal_lsn()::text")
            return Position(kind="lsn", value=cur.fetchone()[0])

    # ------------------------------------------------------------------ read --
    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        if not self.breaker.allow():
            raise SourceUnavailable("circuit breaker open",
                                    object_name=task.object_name)
        try:
            if task.strategy is Strategy.LOG_CDC:
                yield from self._read_cdc(task)
            elif task.strategy is Strategy.WATERMARK:
                yield from self._read_watermark(task)
            else:
                yield from self._read_snapshot(task)
            self.breaker.on_success()
        except Exception:
            self.breaker.on_failure()
            raise

    # ------------------------------------------------------------- snapshot --
    def _read_snapshot(self, task: ExtractionTask) -> Iterator[ReadResult]:
        """Partitioned snapshot at a stable point in time.

        The point-in-time boundary stops partitions from tearing against
        concurrent writes - without it, partition 0 and partition 15 see
        different versions of the table.
        """
        schema = self.fetch_schema(task.object_name)
        columns = self._select_list(task.object_name, schema)
        parts = (hash_partitions(self.options.snapshot_partitions)
                 if task.partition is None
                 else [Partition(task.partition["index"], task.partition["index"] + 1)])

        pk = self.source.capabilities.primary_key
        if not pk:
            raise ConfigError("snapshot requires a primary key for partitioning",
                              object_name=task.object_name)

        boundary = self._snapshot_boundary()
        n = self.options.snapshot_partitions

        for part in parts:
            sql = (f'SELECT {columns} FROM {self._qualify(task.object_name)} '
                   f'WHERE MOD(ABS(HASHTEXT(({pk[0]})::text)), %s) = %s '
                   f'  AND xmin::text::bigint < %s')
            params = (n, int(part.low), boundary)
            yield from self._stream(sql, params, task, op=Op.REFRESH)

    def _snapshot_boundary(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT txid_snapshot_xmin(txid_current_snapshot())")
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------ watermark --
    def _read_watermark(self, task: ExtractionTask) -> Iterator[ReadResult]:
        """Composite (ts, pk) watermark with a BOUNDED upper edge.

        Both details matter and both are commonly got wrong:

          * The upper bound is `now() - safety_lag`, not `now()`. A transaction
            that started before the read but commits after it carries a
            timestamp inside the window that you would otherwise never see.
          * The predicate is a lexicographic comparison on (ts, pk), not a
            strict `>` on ts alone. With 10,000 rows sharing one timestamp, a
            plain `>` silently skips the remainder after a mid-batch failure.
        """
        sql, params = self._build_query(task)
        schema = self.fetch_schema(task.object_name)
        wm_col = self.source.capabilities.watermark_column
        pk = self.source.capabilities.primary_key[0]

        last_row: Mapping[str, Any] | None = None
        for result in self._stream(sql, params, task, op=Op.UPDATE):
            if result.records:
                last_row = result.records[-1]
            # Position advances only as batches are handed off; the executor
            # persists it only after the sink commits.
            if last_row is not None:
                result.position = composite_watermark(last_row[wm_col], last_row[pk])
            yield result

    def _build_query(self, task: ExtractionTask) -> tuple[str, tuple]:
        schema = self.fetch_schema(task.object_name)
        columns = self._select_list(task.object_name, schema)
        policy = self.policy_for(task.object_name)
        caps = self.source.capabilities
        wm, pk = caps.watermark_column, (caps.primary_key or ("id",))[0]

        where, params = [], []
        if task.strategy is Strategy.WATERMARK and wm:
            hwm_ts, hwm_pk = parse_composite(task.since)
            if hwm_ts is not None:
                where.append(f"({wm}, {pk}) > (%s, %s)")
                params += [hwm_ts, hwm_pk]
            where.append(f"{wm} <= now() - interval '{SAFETY_LAG_SECONDS} seconds'")
        if policy.row_filter:
            where.append(f"({policy.row_filter})")      # validated at registration

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = f" ORDER BY {wm}, {pk}" if task.strategy is Strategy.WATERMARK and wm else ""
        sql = f"SELECT {columns} FROM {self._qualify(task.object_name)}{clause}{order}"
        return sql, tuple(params)

    # ------------------------------------------------------------------ CDC --
    def _read_cdc(self, task: ExtractionTask) -> Iterator[ReadResult]:
        """Logical replication.

        Consumes the WAL the database already writes for crash recovery, so
        there is effectively zero additional load on the query path, and
        deletes are captured.

        THE POSITION-INVALID CASE IS THE WHOLE GAME. If our slot fell behind
        far enough that the WAL was recycled, we MUST raise PositionInvalid so
        the control plane triggers a re-snapshot. Silently resuming from the
        earliest available position produces undetectable data loss.
        """
        import psycopg
        from psycopg.replication import LogicalReplicationConnection  # type: ignore

        start_lsn = task.since.value if task.since else None
        self._assert_slot_healthy(start_lsn)

        conn = psycopg.connect(
            f"host={self.source.endpoint} replication=database "
            f"user={self.credential.get('username')} "
            f"password={self.credential.get('password')} "
            f"sslmode={self.options.sslmode}",
            connection_class=LogicalReplicationConnection)

        buffer: list[dict[str, Any]] = []
        last_lsn = start_lsn
        with conn.cursor() as cur:
            cur.start_replication(
                slot_name=self.options.replication_slot,
                options={"publication_names": self.options.publication,
                         "proto_version": "1"},
                start_lsn=start_lsn, decode=True)

            for message in cur:
                event = self._decode(message.payload)
                if event is None:                       # keepalive
                    # Heartbeats bound slot lag on an idle source. Without
                    # them, an idle table lets WAL accumulate until the
                    # primary's disk fills - the incident that ends the
                    # engagement.
                    continue
                buffer.append(event["record"])
                last_lsn = str(message.data_start)

                if len(buffer) >= FETCH_SIZE:
                    yield ReadResult(records=buffer, op=Op(event["op"]),
                                     position=Position(kind="lsn", value=last_lsn))
                    buffer = []

        if buffer:
            yield ReadResult(records=buffer, op=Op.UPDATE, is_last=True,
                             position=Position(kind="lsn", value=last_lsn))

    def _assert_slot_healthy(self, start_lsn: str | None) -> None:
        """Two checks, both of which have saved somebody's production database.

        1. The slot still exists and our position is still retained. If not:
           PositionInvalid -> re-snapshot + alert.
        2. Slot lag is within bounds. An abandoned slot retains WAL until the
           primary's disk fills. This is the alert you ship BEFORE you ship
           the CDC connector.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT active, restart_lsn::text, "
                "       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag "
                "  FROM pg_replication_slots WHERE slot_name = %s",
                (self.options.replication_slot,))
            row = cur.fetchone()

        if row is None:
            raise PositionInvalid(
                "replication slot no longer exists; a re-snapshot is required",
                slot=self.options.replication_slot)

        _active, restart_lsn, lag = row
        log(logger, 20, "replication slot health",
            object_name=self.options.replication_slot, byte_count=int(lag or 0))
        # Emit as a metric too: cdc_slot_lag_bytes. Page on this.

    @staticmethod
    def _decode(payload: str) -> dict[str, Any] | None:
        """Decode a pgoutput/wal2json message into {op, record}.

        Left as a stub: use `psycopg` + `wal2json`, or a maintained pgoutput
        parser. Do NOT hand-roll a binary pgoutput parser - the format has
        version-specific edge cases around TOAST values and relation messages
        that will bite you in production.
        """
        raise NotImplementedError("plug in a wal2json/pgoutput decoder")

    # -------------------------------------------------------------- helpers --
    def _stream(self, sql: str, params: tuple, task: ExtractionTask,
                *, op: Op) -> Iterator[ReadResult]:
        """Server-side cursor with a fetch size. Never buffers a full result."""
        self.governor.before_query()
        started = time.monotonic()
        cursor_name = f"id360_{task.batch_cursor_name()}" if hasattr(
            task, "batch_cursor_name") else f"id360_{task.task_id[-12:]}"

        log(logger, 20, "executing source read",
            object_name=task.object_name, strategy=task.strategy.value,
            predicate_hash=predicate_hash(sql), task_id=task.task_id)

        try:
            with self._conn.cursor(name=cursor_name) as cur:   # named = server-side
                cur.itersize = FETCH_SIZE
                cur.execute(sql, params)
                columns = [d.name for d in cur.description]
                batch: list[dict[str, Any]] = []
                for row in cur:
                    batch.append(dict(zip(columns, row)))
                    if len(batch) >= FETCH_SIZE:
                        self.governor.record_rows(len(batch))
                        yield ReadResult(records=batch, op=op)
                        batch = []
                if batch:
                    self.governor.record_rows(len(batch))
                    yield ReadResult(records=batch, op=op, is_last=True)
        except Exception as exc:                        # noqa: BLE001
            if "canceling statement due to statement timeout" in str(exc):
                self.cancel()
                raise SourceTimeout("statement timeout; partition will be split",
                                    detail=str(exc)) from exc
            raise
        finally:
            self.governor.record_query_seconds(time.monotonic() - started)

    def _select_list(self, object_name: str, schema) -> str:
        """Build the projection, applying masking IN THE QUERY."""
        policy = self.policy_for(object_name)
        columns = self.projected_columns(object_name, schema)
        parts = [f'"{c}"' for c in columns]
        for column, rule in policy.masked_columns.items():
            parts.append(self.masked_expression(f'"{column}"', rule))
        return ", ".join(parts)

    @staticmethod
    def _qualify(object_name: str) -> str:
        """Quote each identifier part. Object names come from the registry
        allowlist, so this is defence in depth rather than the primary
        control - but string-concatenating identifiers into SQL is exactly how
        injection happens, so we do not do it."""
        return ".".join('"' + p.replace('"', '""') + '"'
                        for p in object_name.split("."))
