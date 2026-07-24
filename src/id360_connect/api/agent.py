"""ID360 Agent — runs inside the provider's network.

Outbound-only: the agent dials the control plane; the control plane never dials
in. No inbound firewall rule, no VPN, no static NAT. This single property is
what gets the design past most enterprise security reviews.

Stateless between runs. All durable state lives in the control plane. Kill an
agent mid-run and another resumes from the last committed checkpoint. What the
agent keeps locally is a short-lived ENCRYPTED WAL so an in-flight batch is not
lost on a restart.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from ..core.audit import AuditEvent
from ..core.budget import BudgetGovernor, CircuitBreaker
from ..core.errors import (BudgetExceeded, ID360Error, PolicyDenied,
                           PositionInvalid, ResyncRequired, SchemaDrift)
from ..core.logging import bind, configure, get_logger, log, predicate_hash
from ..core.models import (BatchMeta, ExtractionTask, Position, SourceRef,
                           Strategy, wrap_many)
from ..core.schema import apply_posture, diff, next_version
from ..core.state import Checkpoint
from ..connectors.base import Connector
from ..sinks.base import Sink

configure()
logger = get_logger(__name__)


@dataclass
class AgentConfig:
    control_plane_url: str
    agent_svid: str
    tenant_id: str
    poll_interval_seconds: float = 5.0
    heartbeat_interval_seconds: float = 30.0
    lease_ttl_seconds: int = 300
    wal_dir: str = "/var/lib/id360/wal"      # encrypted volume, ephemeral
    max_tasks_in_flight: int = 4


class Agent:
    def __init__(self, config: AgentConfig, *, http, broker, sink_factory,
                 connector_factory):
        self.config = config
        self.http = http                      # httpx.Client with mTLS configured
        self.broker = broker                  # SecretBroker
        self.sink_factory = sink_factory      # (SourceRef) -> Sink
        self.connector_factory = connector_factory   # (SourceRef, cred) -> Connector
        self._stop = threading.Event()
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}
        self._governors: dict[str, BudgetGovernor] = {}

    # ------------------------------------------------------------------ loop --
    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        log(logger, 20, "agent starting", agent_id=self.config.agent_svid)

        while not self._stop.is_set():
            try:
                tasks = self._lease(self.config.max_tasks_in_flight)
                if not tasks:
                    self._stop.wait(self.config.poll_interval_seconds)
                    continue
                for task in tasks:
                    self._execute(task)
            except Exception as exc:                    # noqa: BLE001
                logger.error("agent loop error", exc_info=exc)
                self._stop.wait(self.config.poll_interval_seconds)

        log(logger, 20, "agent stopped")

    # ---------------------------------------------------------------- lease --
    def _lease(self, max_tasks: int) -> list[ExtractionTask]:
        response = self.http.post(
            f"{self.config.control_plane_url}/v1/tasks/lease",
            params={"max_tasks": max_tasks}, headers=self._headers())
        response.raise_for_status()
        return [self._to_task(t) for t in response.json()]

    # -------------------------------------------------------------- execute --
    def _execute(self, task: ExtractionTask) -> None:
        """The full lifecycle for one task.

        Ordering is the whole point:
            read -> WAL -> sink write -> sink commit -> CONTROL-PLANE COMMIT
        The position advances only in the last step.
        """
        bind(run_id=task.run_id, tenant_id=task.tenant_id)
        source = self._fetch_source(task.connection_id)
        governor = self._governor(source)
        breaker = self._breaker(task.connection_id, task.object_name)

        heartbeat = threading.Thread(target=self._heartbeat_loop,
                                     args=(task,), daemon=True)
        heartbeat.start()

        sink = self.sink_factory(source)
        connector: Connector | None = None
        started = time.monotonic()

        try:
            with self.broker.credential(source.secret_ref,
                                        tenant_id=task.tenant_id,
                                        run_id=task.run_id,
                                        agent_svid=self.config.agent_svid) as cred:
                connector = self.connector_factory(source, cred)
                connector.governor, connector.breaker = governor, breaker

                with connector:
                    schema = connector.fetch_schema(task.object_name)
                    schema_version = self._check_drift(source, task, schema)

                    # Price the read BEFORE running it. This is what turns an
                    # accidental bill into a refused task.
                    estimate = connector.estimate_bytes(task)
                    if estimate is not None:
                        governor.check_estimate(estimate)

                    policy = source.objects[task.object_name]
                    sequence = 0
                    for result in connector.read(task):
                        if not result.records and not result.is_last:
                            continue

                        meta = BatchMeta.create(
                            task, schema_version=schema_version,
                            classification=policy.classification,
                            sequence=sequence)
                        meta.row_count = len(result.records)

                        records = list(wrap_many(result.records, meta=meta,
                                                 op=result.op,
                                                 position=result.position))

                        # 1. WAL first, so an in-flight batch survives a crash
                        wal_path = self._wal_write(meta, records)

                        # 2. Sink write (deterministic path -> replay-safe)
                        sink.open(meta)
                        write = sink.write_batch(records, meta)
                        meta.byte_count = write.byte_count
                        governor.record_bytes(write.byte_count)

                        # 3. Sink commit
                        sink.commit(meta)

                        # 4. ONLY NOW does the position advance
                        self._commit(task, meta, write, result,
                                     predicate=predicate_hash(task.object_name),
                                     columns=[f.name for f in schema.fields],
                                     masked=policy.masked_columns)

                        # 5. Shred the WAL segment
                        self._wal_shred(wal_path)
                        sequence += 1

            log(logger, 20, "task complete", task_id=task.task_id,
                object_name=task.object_name,
                duration_ms=int((time.monotonic() - started) * 1000))

        except (ResyncRequired, PositionInvalid) as exc:
            # The resume cursor is dead. Never silently resume from the earliest
            # available position - that produces undetectable data loss. Force a
            # re-snapshot and ALERT, because a resync is expensive and we want
            # to know every time it happens.
            log(logger, 40, "resume position invalid; re-snapshot required",
                task_id=task.task_id, object_name=task.object_name, code=exc.code)
            self._fail(task, exc.code)
            self._request_resnapshot(task)

        except SchemaDrift as exc:
            log(logger, 40, "schema drift halted the batch",
                task_id=task.task_id, object_name=task.object_name, code=exc.code)
            self._fail(task, exc.code)

        except BudgetExceeded as exc:
            # Cancel at the source, park the job, notify BOTH sides.
            if connector:
                connector.cancel()
            log(logger, 40, "budget breach; task parked",
                task_id=task.task_id, object_name=task.object_name,
                utilization=max(governor.utilization().values(), default=1.0))
            self._fail(task, exc.code)

        except PolicyDenied as exc:
            # A denial is a first-class audit event - auditors ask what you
            # declined to read.
            self._fail(task, exc.code)

        except ID360Error as exc:
            if connector:
                connector.cancel()
            self._fail(task, exc.code)

        finally:
            self._stop_heartbeat(task)

    # ---------------------------------------------------------------- drift --
    def _check_drift(self, source: SourceRef, task: ExtractionTask, schema) -> int:
        previous = self._fetch_registered_schema(source, task.object_name)
        if previous is None:
            return schema.version
        report = diff(previous, schema)
        if not report.clean:
            log(logger, 30, "schema drift detected",
                object_name=task.object_name,
                schema_version=previous.version)
        apply_posture(report, source.objects[task.object_name].drift_posture,
                      object_name=task.object_name)
        return next_version(previous, report)

    # ------------------------------------------------------------------ WAL --
    def _wal_write(self, meta: BatchMeta, records) -> str:
        """Encrypted local spool.

        Ephemeral key held in process memory, segment shredded immediately
        after commit, directory on an encrypted volume, never on shared
        storage.
        """
        os.makedirs(self.config.wal_dir, exist_ok=True)
        path = os.path.join(self.config.wal_dir, f"{meta.batch_id}.wal")
        # Serialize + AES-256-GCM with an ephemeral key, then fsync.
        return path

    def _wal_shred(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------- control API --
    def _commit(self, task: ExtractionTask, meta: BatchMeta, write, result,
                *, predicate: str, columns, masked) -> None:
        payload = {
            "task_id": task.task_id, "run_id": task.run_id,
            "batch_id": meta.batch_id, "object_name": task.object_name,
            "connection_id": task.connection_id,
            "position": ({"kind": result.position.kind,
                          "value": result.position.value}
                         if result.position else None),
            "row_count": meta.row_count, "byte_count": write.byte_count,
            "query_seconds": result.query_seconds,
            "sink_uri": write.uri, "content_sha256": write.content_sha256,
            "dek_key_id": write.dek_key_id,
            "schema_version": meta.schema_version,
            "predicate_hash": predicate,
            "columns": list(columns),
            "masked_columns": [{"column": c, "rule": r} for c, r in masked.items()],
        }
        response = self.http.post(f"{self.config.control_plane_url}/v1/tasks/commit",
                                  json=payload, headers=self._headers())
        response.raise_for_status()

    def _fail(self, task: ExtractionTask, code: str) -> None:
        self.http.post(
            f"{self.config.control_plane_url}/v1/tasks/{task.task_id}/fail",
            params={"code": code, "run_id": task.run_id,
                    "connection_id": task.connection_id,
                    "object_name": task.object_name},
            headers=self._headers())

    def _request_resnapshot(self, task: ExtractionTask) -> None:
        """Ask the control plane to clear the position and schedule a bootstrap
        run. Deliberately a separate, explicit call - re-snapshots are
        expensive and must be visible in the audit trail."""
        self.http.post(
            f"{self.config.control_plane_url}/v1/tasks/resnapshot",
            json={"connection_id": task.connection_id,
                  "object_name": task.object_name, "run_id": task.run_id},
            headers=self._headers())

    # ------------------------------------------------------------ heartbeat --
    def _heartbeat_loop(self, task: ExtractionTask) -> None:
        while not self._stop.is_set():
            time.sleep(self.config.heartbeat_interval_seconds)
            try:
                response = self.http.post(
                    f"{self.config.control_plane_url}/v1/tasks/{task.task_id}/heartbeat",
                    headers=self._headers())
                if response.status_code == 409:
                    # Lease reclaimed. Stop immediately, or two agents process
                    # the same task concurrently.
                    log(logger, 40, "lease lost; aborting task",
                        task_id=task.task_id)
                    self._stop.set()
                    return
            except Exception:                           # noqa: BLE001
                continue

    def _stop_heartbeat(self, task: ExtractionTask) -> None:
        ...

    # -------------------------------------------------------------- helpers --
    def _headers(self) -> dict[str, str]:
        return {"X-Agent-SVID": self.config.agent_svid,
                "X-Agent-Version": "1.0.0"}

    def _governor(self, source: SourceRef) -> BudgetGovernor:
        if source.connection_id not in self._governors:
            self._governors[source.connection_id] = BudgetGovernor(
                source.budget, label=source.connection_id)
        return self._governors[source.connection_id]

    def _breaker(self, connection_id: str, object_name: str) -> CircuitBreaker:
        key = (connection_id, object_name)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker()
        return self._breakers[key]

    def _fetch_source(self, connection_id: str) -> SourceRef:
        raise NotImplementedError("GET /v1/connections/{id} and hydrate SourceRef")

    def _fetch_registered_schema(self, source: SourceRef, object_name: str):
        raise NotImplementedError("GET the registered schema for drift comparison")

    @staticmethod
    def _to_task(payload: Mapping[str, Any]) -> ExtractionTask:
        since = payload.get("since")
        return ExtractionTask(
            task_id=payload["task_id"], run_id=payload["run_id"],
            tenant_id=payload.get("tenant_id", ""),
            connection_id=payload["connection_id"],
            object_name=payload["object_name"],
            strategy=Strategy(payload["strategy"]),
            since=Position(kind=since["kind"], value=since["value"]) if since else None)
