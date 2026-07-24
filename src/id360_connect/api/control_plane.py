"""Control plane — FastAPI.

Carries task descriptors, watermarks, schema fingerprints, metrics and audit
events. All small, all structured.

IT NEVER CARRIES BULK DATA. Rows and file bytes go agent -> sink over a path
that does not traverse this service. Consequences:

  * The control plane is not in the data blast radius. A compromise here leaks
    metadata, not customer records.
  * It scales with the NUMBER OF JOBS, not with data volume.
  * Data residency is satisfiable: agent and sink stay in-country while this
    service can sit elsewhere.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field as PField

from ..core.audit import AuditEvent, AuditLedger, InMemoryAuditStore
from ..core.errors import ID360Error
from ..core.logging import bind, configure, get_logger
from ..core.models import (Budget, Classification, DriftPosture, ExtractionTask,
                           ObjectPolicy, Position, SourceCapabilities,
                           SourceKind, SourceRef, Strategy, new_id, utcnow)
from ..core.state import InMemoryStateStore
from ..connectors.base import explain_resolution, resolve_strategy

configure()
logger = get_logger(__name__)

app = FastAPI(
    title="ID360 Connector Control Plane",
    version="1.0.0",
    description="Metadata plane for ID360 data connectors. Never carries bulk data.",
)

# Replace with Postgres-backed implementations in production.
STATE = InMemoryStateStore()
LEDGER = AuditLedger(InMemoryAuditStore())
REGISTRY: dict[str, SourceRef] = {}


# --------------------------------------------------------------------------- #
# Authentication — workload identity, not a shared key
# --------------------------------------------------------------------------- #
class AgentPrincipal(BaseModel):
    svid: str
    tenant_id: str
    agent_version: str
    owner_principal: str


async def current_agent(
    x_agent_svid: str = Header(..., alias="X-Agent-SVID"),
    x_agent_version: str = Header("unknown", alias="X-Agent-Version"),
) -> AgentPrincipal:
    """In production this is derived from the VERIFIED mTLS client certificate,
    not from a header. The SVID encodes the tenant, so an agent physically
    cannot lease a task for a different tenant.

    Headers are used here only to keep the reference implementation runnable.
    """
    try:
        # spiffe://id360/ns/agents/tenant/<tenant_id>/sa/<name>
        parts = x_agent_svid.split("/")
        tenant_id = parts[parts.index("tenant") + 1]
    except (ValueError, IndexError):
        raise HTTPException(status_code=401, detail={"code": "id360.auth.bad_svid"})
    return AgentPrincipal(svid=x_agent_svid, tenant_id=tenant_id,
                          agent_version=x_agent_version,
                          owner_principal="unknown")


def tenant_scoped(source_id: str, agent: AgentPrincipal) -> SourceRef:
    """Tenant isolation, enforced in one place rather than by convention in
    every handler."""
    source = REGISTRY.get(source_id)
    if source is None or source.tenant_id != agent.tenant_id:
        # 404, not 403 - do not confirm the existence of another tenant's
        # resources to an unauthorized caller.
        raise HTTPException(status_code=404, detail={"code": "id360.not_found"})
    return source


# --------------------------------------------------------------------------- #
# Error handling — sanitized at the boundary
# --------------------------------------------------------------------------- #
@app.exception_handler(ID360Error)
async def id360_error_handler(request: Request, exc: ID360Error) -> JSONResponse:
    """A source's error message can contain row data
    (`Key (email)=(alice@example.com)`). `exc.detail` is logged internally
    under redaction; only `exc.public()` crosses the boundary.
    """
    logger.warning("request failed", exc_info=exc, extra={"id360": {"code": exc.code}})
    return JSONResponse(status_code=exc.http_status, content=exc.public())


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ObjectPolicyIn(BaseModel):
    object_name: str
    columns: Sequence[str] = ()
    masked_columns: Mapping[str, str] = {}
    row_filter: str | None = None
    classification: Classification = Classification.INTERNAL
    drift_posture: DriftPosture = DriftPosture.EVOLVE


class ConnectionIn(BaseModel):
    tenant_id: str
    kind: SourceKind
    display_name: str
    endpoint: str
    secret_ref: str = PField(..., description="Pointer only. Never a secret value.")
    capabilities: Mapping[str, Any] = {}
    budget: Mapping[str, Any] = {}
    objects: Sequence[ObjectPolicyIn] = ()
    blackout_cron: Sequence[str] = ()
    options: Mapping[str, Any] = {}


class ConnectionOut(BaseModel):
    connection_id: str
    tenant_id: str
    kind: SourceKind
    display_name: str
    endpoint_fingerprint: str
    objects: Sequence[str]
    policy_version: int


class TaskOut(BaseModel):
    task_id: str
    run_id: str
    connection_id: str
    object_name: str
    strategy: Strategy
    since: Mapping[str, Any] | None = None
    lease_expires_at: datetime | None = None


class CommitIn(BaseModel):
    task_id: str
    run_id: str
    batch_id: str
    object_name: str
    connection_id: str
    position: Mapping[str, Any] | None = None
    row_count: int = 0
    byte_count: int = 0
    query_seconds: float = 0.0
    sink_uri: str = ""
    content_sha256: str = ""
    dek_key_id: str | None = None
    schema_version: int = 1
    predicate_hash: str = ""
    columns: Sequence[str] = ()
    masked_columns: Sequence[Mapping[str, str]] = ()


# --------------------------------------------------------------------------- #
# Connection registry
# --------------------------------------------------------------------------- #
@app.post("/v1/connections", response_model=ConnectionOut, status_code=201)
async def create_connection(payload: ConnectionIn) -> ConnectionOut:
    if not payload.secret_ref or "://" not in payload.secret_ref:
        raise HTTPException(400, {"code": "id360.config.bad_secret_ref"})
    if any(k in payload.secret_ref.lower() for k in ("password=", "pwd=", "token=")):
        # A literal credential in the reference field is a configuration error
        # we refuse rather than store.
        raise HTTPException(400, {"code": "id360.config.secret_value_supplied"})

    source = SourceRef(
        connection_id=new_id("conn"),
        tenant_id=payload.tenant_id,
        kind=payload.kind,
        display_name=payload.display_name,
        endpoint=payload.endpoint,
        secret_ref=payload.secret_ref,
        capabilities=SourceCapabilities(**payload.capabilities),
        budget=Budget(**payload.budget),
        objects={o.object_name: ObjectPolicy(**o.model_dump())
                 for o in payload.objects},
        blackout_cron=tuple(payload.blackout_cron),
        options=dict(payload.options),
    )
    REGISTRY[source.connection_id] = source
    LEDGER.record(AuditEvent.CONNECTION_CREATED, tenant_id=source.tenant_id,
                  source={"connection_id": source.connection_id,
                          "kind": source.kind.value,
                          "endpoint_fingerprint": source.endpoint_fingerprint()},
                  governance={"objects": sorted(source.objects),
                              "policy_version": source.policy_version})
    return _to_out(source)


@app.get("/v1/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: str,
                         agent: AgentPrincipal = Depends(current_agent)) -> ConnectionOut:
    return _to_out(tenant_scoped(connection_id, agent))


@app.get("/v1/connections/{connection_id}/strategy")
async def strategy_report(connection_id: str, object_name: str,
                          agent: AgentPrincipal = Depends(current_agent)) -> dict:
    """Why did this object end up on this strategy?

    Surfacing this at onboarding is what turns "our extract is expensive" into
    an actionable conversation with the provider.
    """
    return explain_resolution(tenant_scoped(connection_id, agent), object_name)


# --------------------------------------------------------------------------- #
# Task lease / commit — the delivery-semantics core
# --------------------------------------------------------------------------- #
@app.post("/v1/tasks/lease", response_model=list[TaskOut])
async def lease_tasks(max_tasks: int = 1,
                      agent: AgentPrincipal = Depends(current_agent)) -> list[TaskOut]:
    """Hand out work. The lease has a TTL; if the agent dies, the lease expires
    and another agent picks the task up from the last committed checkpoint."""
    bind(tenant_id=agent.tenant_id)
    leased: list[TaskOut] = []

    for source in REGISTRY.values():
        if source.tenant_id != agent.tenant_id:
            continue
        for object_name in source.objects:
            if len(leased) >= max_tasks:
                break
            checkpoint = STATE.get_checkpoint(source.tenant_id,
                                              source.connection_id, object_name)
            task = ExtractionTask.create(
                run_id=new_id("run"), source=source, object_name=object_name,
                strategy=resolve_strategy(source, object_name,
                                          bootstrap=checkpoint.position is None),
                since=checkpoint.position)
            lease = STATE.acquire_lease(task, agent.svid)
            if lease is None:
                continue
            task.lease_expires_at = lease.expires_at

            LEDGER.record(AuditEvent.TASK_LEASED, tenant_id=source.tenant_id,
                          run_id=task.run_id,
                          actor={"agent_svid": agent.svid,
                                 "agent_version": agent.agent_version},
                          source={"connection_id": source.connection_id,
                                  "object": object_name},
                          extraction={"strategy": task.strategy.value})
            leased.append(TaskOut(
                task_id=task.task_id, run_id=task.run_id,
                connection_id=task.connection_id, object_name=task.object_name,
                strategy=task.strategy,
                since=({"kind": task.since.kind, "value": task.since.value}
                       if task.since else None),
                lease_expires_at=lease.expires_at))
    return leased


@app.post("/v1/tasks/{task_id}/heartbeat")
async def heartbeat(task_id: str,
                    agent: AgentPrincipal = Depends(current_agent)) -> dict:
    ok = STATE.renew_lease(task_id, agent.svid)
    if not ok:
        # The lease was reclaimed - the agent must stop, or two agents will
        # process the same task concurrently.
        raise HTTPException(409, {"code": "id360.lease.lost"})
    return {"ok": True}


@app.post("/v1/tasks/commit")
async def commit(payload: CommitIn,
                 agent: AgentPrincipal = Depends(current_agent)) -> dict:
    """THE atomic operation of the whole system.

    Advance the watermark + append the audit record + mark the batch durable.
    All or nothing, and ONLY after the sink has acknowledged durability.

    Advancing the position before the sink commit is the single most common
    correctness bug in hand-rolled connectors, and it is silent: you lose data
    and nothing errors.
    """
    source = tenant_scoped(payload.connection_id, agent)
    bind(run_id=payload.run_id, tenant_id=agent.tenant_id)

    checkpoint = STATE.get_checkpoint(agent.tenant_id, payload.connection_id,
                                      payload.object_name)
    new_position = (Position(kind=payload.position["kind"],
                             value=payload.position["value"])
                    if payload.position else None)

    updated = STATE.commit(checkpoint, batch_id=payload.batch_id,
                           new_position=new_position)

    policy = source.objects.get(payload.object_name)
    LEDGER.record(
        AuditEvent.EXTRACT_COMMIT, tenant_id=agent.tenant_id,
        run_id=payload.run_id, batch_id=payload.batch_id,
        actor={"agent_svid": agent.svid, "agent_version": agent.agent_version,
               "owner_principal": agent.owner_principal},
        source={"connection_id": source.connection_id, "kind": source.kind.value,
                "endpoint_fingerprint": source.endpoint_fingerprint(),
                "object": payload.object_name},
        extraction={"predicate_hash": payload.predicate_hash,
                    "columns": list(payload.columns),
                    "masked_columns": list(payload.masked_columns),
                    "position_to": new_position.to_json() if new_position else None},
        volume={"row_count": payload.row_count, "byte_count": payload.byte_count,
                "query_seconds": payload.query_seconds},
        destination={"uri": payload.sink_uri,
                     "content_sha256": payload.content_sha256,
                     "dek_key_id": payload.dek_key_id},
        governance={"policy_version": source.policy_version,
                    "schema_version": payload.schema_version,
                    "classification": policy.classification.value if policy else None},
    )
    STATE.release_lease(payload.task_id, agent.svid)
    return {"ok": True,
            "position": updated.position.to_json() if updated.position else None}


@app.post("/v1/tasks/{task_id}/fail")
async def fail_task(task_id: str, code: str, run_id: str,
                    connection_id: str, object_name: str,
                    agent: AgentPrincipal = Depends(current_agent)) -> dict:
    """Failures are recorded with a SANITIZED reason. The full detail lives in
    the agent's logs under redaction, keyed by run_id."""
    LEDGER.record(AuditEvent.EXTRACT_FAILED, tenant_id=agent.tenant_id,
                  run_id=run_id, outcome="FAILED", reason=code,
                  actor={"agent_svid": agent.svid},
                  source={"connection_id": connection_id, "object": object_name})
    STATE.release_lease(task_id, agent.svid)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
@app.get("/v1/audit/verify")
async def verify_chain(agent: AgentPrincipal = Depends(current_agent)) -> dict:
    return {"tenant_id": agent.tenant_id,
            "records_verified": LEDGER.verify(agent.tenant_id)}


@app.get("/v1/audit/head")
async def audit_head(agent: AgentPrincipal = Depends(current_agent)) -> dict:
    """Publish this to an EXTERNAL trust anchor on a schedule. Without an
    external anchor the chain proves ordering but not immutability against a
    privileged insider."""
    return LEDGER.publish_head(agent.tenant_id)


@app.get("/v1/health")
async def health() -> dict:
    return {"status": "ok", "ts": utcnow().isoformat()}


def _to_out(source: SourceRef) -> ConnectionOut:
    return ConnectionOut(
        connection_id=source.connection_id, tenant_id=source.tenant_id,
        kind=source.kind, display_name=source.display_name,
        endpoint_fingerprint=source.endpoint_fingerprint(),
        objects=sorted(source.objects), policy_version=source.policy_version)
