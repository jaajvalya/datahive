"""Tests for the invariants the whole framework depends on.

These are not "does the code run" tests. Each one pins a property that, if it
broke, would cause silent data loss, duplication, or a leak - the failures that
do not announce themselves.

    python3 -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from id360_connect.core.audit import (AuditEvent, AuditLedger, GENESIS,
                                      InMemoryAuditStore)
from id360_connect.core.budget import (AdaptiveConcurrency, BudgetGovernor,
                                       CircuitBreaker, TokenBucket)
from id360_connect.core.errors import (AuditChainBroken, BudgetExceeded,
                                       PolicyDenied, PositionRegression)
from id360_connect.core.logging import RedactingJsonFormatter, scrub
from id360_connect.core.models import (Budget, Classification, DriftPosture,
                                       ExtractionTask, ObjectPolicy, Op,
                                       Position, SourceCapabilities, SourceKind,
                                       SourceRef, Strategy, row_hash, wrap)
from id360_connect.core.schema import (Field, Schema, apply_posture, diff)
from id360_connect.core.state import (InMemoryStateStore, composite_watermark,
                                      bounded_window)
from id360_connect.connectors.base import resolve_strategy


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def source() -> SourceRef:
    return SourceRef(
        connection_id="conn_test", tenant_id="tnt_a", kind=SourceKind.DATABASE,
        display_name="test", endpoint="db.example.internal",
        secret_ref="vault://tnt_a/kv/pg",
        capabilities=SourceCapabilities(primary_key=("id",)),
        budget=Budget(max_bytes_per_day=1_000_000),
        objects={"sales.orders": ObjectPolicy(
            object_name="sales.orders",
            masked_columns={"email": "sha256"},
            classification=Classification.CONFIDENTIAL)},
    )


@pytest.fixture
def task(source) -> ExtractionTask:
    return ExtractionTask.create(run_id="run_1", source=source,
                                 object_name="sales.orders",
                                 strategy=Strategy.WATERMARK)


# --------------------------------------------------------------------------- #
# Delivery semantics
# --------------------------------------------------------------------------- #
def test_replayed_batch_is_a_noop(source, task):
    """At-least-once from the agent must become exactly-once at the table.

    A crash between the sink write and the control-plane commit causes the
    batch to be replayed. The second commit must not advance anything twice.
    """
    store = InMemoryStateStore()
    cp = store.get_checkpoint("tnt_a", "conn_test", "sales.orders")

    first = store.commit(cp, batch_id="bat_1",
                         new_position=Position("lsn", "0/100"))
    assert first.position.value == "0/100"

    # Same batch id arrives again after a crash.
    again = store.commit(cp, batch_id="bat_1",
                         new_position=Position("lsn", "0/999"))
    assert again.position.value == "0/100", "replay must not advance the position"


def test_position_cannot_regress_silently():
    """A watermark that moves backwards re-reads data and, worse, hides the
    fact that something went wrong upstream. Require an explicit override."""
    store = InMemoryStateStore()
    cp = store.get_checkpoint("tnt_a", "conn_test", "sales.orders")
    store.commit(cp, batch_id="b1", new_position=Position("lsn", "0/200"))

    with pytest.raises(PositionRegression):
        store.commit(cp, batch_id="b2", new_position=Position("lsn", "0/100"))

    # With an explicit operator override it is permitted - and auditable.
    updated = store.commit(cp, batch_id="b3",
                           new_position=Position("lsn", "0/100"),
                           allow_regression=True)
    assert updated.position.value == "0/100"


def test_lease_prevents_two_agents_on_one_task(source, task):
    store = InMemoryStateStore()
    assert store.acquire_lease(task, "agent-a") is not None
    assert store.acquire_lease(task, "agent-b") is None, "double-lease allowed"

    store.release_lease(task.task_id, "agent-a")
    assert store.acquire_lease(task, "agent-b") is not None


def test_expired_lease_is_reclaimable(source, task):
    store = InMemoryStateStore()
    lease = store.acquire_lease(task, "agent-a", ttl_seconds=1)
    lease.expires_at -= timedelta(seconds=5)          # simulate a dead agent
    assert store.acquire_lease(task, "agent-b") is not None


# --------------------------------------------------------------------------- #
# Watermark correctness
# --------------------------------------------------------------------------- #
def test_watermark_window_is_bounded_below_now():
    """The upper bound must be in the past. A transaction that started before
    the read but commits after it carries a timestamp inside the window that we
    would otherwise never see."""
    from id360_connect.core.models import utcnow
    _lower, upper = bounded_window(None, safety_lag_seconds=120)
    assert upper < utcnow(), "upper bound must lag now()"
    assert (utcnow() - upper).total_seconds() >= 119


def test_composite_watermark_carries_the_tiebreaker():
    """A strict `>` on a non-unique timestamp silently skips rows after a
    mid-batch failure. The pk is what makes the predicate a total order."""
    pos = composite_watermark("2026-07-20T09:00:00Z", 4711)
    assert pos.kind == "watermark"
    assert pos.value["ts"] == "2026-07-20T09:00:00Z"
    assert pos.value["pk"] == "4711"


# --------------------------------------------------------------------------- #
# Strategy resolution
# --------------------------------------------------------------------------- #
def test_unindexed_watermark_is_not_incremental(source):
    """A watermark column without an index is a full table scan wearing a
    costume. The resolver must not dignify it as an incremental strategy."""
    source.capabilities.watermark_column = "updated_at"
    source.capabilities.watermark_indexed = False
    assert resolve_strategy(source, "sales.orders") is Strategy.FULL_SNAPSHOT

    source.capabilities.watermark_indexed = True
    assert resolve_strategy(source, "sales.orders") is Strategy.WATERMARK


def test_resolver_prefers_the_cheapest_available(source):
    caps = source.capabilities
    caps.watermark_column, caps.watermark_indexed = "updated_at", True
    assert resolve_strategy(source, "sales.orders") is Strategy.WATERMARK

    caps.supports_change_feed = True
    assert resolve_strategy(source, "sales.orders") is Strategy.CHANGE_FEED

    caps.supports_log_cdc = True
    assert resolve_strategy(source, "sales.orders") is Strategy.LOG_CDC

    caps.supports_storage_native = True
    assert resolve_strategy(source, "sales.orders") is Strategy.STORAGE_NATIVE


# --------------------------------------------------------------------------- #
# Policy / non-exposure
# --------------------------------------------------------------------------- #
def test_unregistered_object_is_denied(source):
    """Default deny. An object absent from the registry is not readable - that
    is what makes the allowlist meaningful."""
    from id360_connect.connectors.base import Connector

    class Probe(Connector):
        def discover(self): return []
        def fetch_schema(self, o): return Schema()
        def read(self, t): return iter(())

    probe = Probe(source, credential=None)
    probe.policy_for("sales.orders")                   # registered: fine
    with pytest.raises(PolicyDenied):
        probe.policy_for("hr.salaries")                # not registered: denied


def test_masked_columns_are_never_projected(source):
    """If we may not hold the value we never fetch it. Fetch-then-drop is a
    leak with extra steps."""
    from id360_connect.connectors.base import Connector

    class Probe(Connector):
        def discover(self): return []
        def fetch_schema(self, o): return Schema()
        def read(self, t): return iter(())

    schema = Schema(fields=(Field("id", "int64"), Field("email", "string"),
                            Field("amount", "float64")))
    columns = Probe(source, None).projected_columns("sales.orders", schema)
    assert "email" not in columns
    assert set(columns) == {"id", "amount"}


def test_log_formatter_drops_non_allowlisted_fields():
    """An allowlist, not a denylist - a denylist always misses the field
    somebody added last week."""
    import logging
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "read done",
                               None, None)
    record.id360 = {"row_count": 5, "customer_email": "alice@example.com"}
    out = RedactingJsonFormatter().format(record)
    assert '"row_count": 5' in out
    assert "alice@example.com" not in out


@pytest.mark.parametrize("text", [
    "password=hunter2",
    "Authorization: Bearer abc123def456ghi789",
    "postgres://user:s3cret@db.internal:5432/app",
    "api_key: sk-livesomethinglong",
])
def test_secrets_are_scrubbed(text):
    assert "[REDACTED]" in scrub(text)


# --------------------------------------------------------------------------- #
# Budget guardrails
# --------------------------------------------------------------------------- #
def test_estimate_refuses_before_reading_anything():
    """The highest-value guardrail: turn an accidental bill into a refused
    task, before a single byte moves."""
    governor = BudgetGovernor(Budget(max_bytes_per_day=1000))
    governor.check_estimate(500)                       # fine
    with pytest.raises(BudgetExceeded):
        governor.check_estimate(5000)


def test_daily_cap_stops_a_slow_leak():
    """Rate limits stop spikes; cumulative caps stop leaks. You need both."""
    governor = BudgetGovernor(Budget(max_bytes_per_day=1000))
    governor.record_bytes(900)
    with pytest.raises(BudgetExceeded):
        governor.record_bytes(200)


def test_aimd_halves_on_throttle():
    limiter = AdaptiveConcurrency(initial=8, ceiling=16)
    limiter.on_throttle()
    assert limiter.limit == 4
    limiter.on_throttle()
    assert limiter.limit == 2
    limiter.on_success()
    assert limiter.limit == 3, "additive increase, multiplicative decrease"


def test_token_bucket_refuses_impossible_requests():
    bucket = TokenBucket(rate=10, capacity=10)
    assert bucket.try_consume(10)
    assert not bucket.try_consume(10)
    with pytest.raises(BudgetExceeded):
        bucket.consume(1000, timeout=0.1)


def test_circuit_breaker_is_scoped_and_recovers():
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=0.05)
    breaker.on_failure()
    assert breaker.allow()
    breaker.on_failure()
    assert not breaker.allow(), "should open after threshold"

    import time
    time.sleep(0.06)
    assert breaker.allow(), "half-open probe after cooldown"
    breaker.on_success()
    assert breaker.state == CircuitBreaker.CLOSED


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def test_audit_chain_verifies():
    ledger = AuditLedger(InMemoryAuditStore())
    for i in range(5):
        ledger.record(AuditEvent.EXTRACT_COMMIT, tenant_id="tnt_a",
                      run_id=f"run_{i}", volume={"row_count": i})
    assert ledger.verify("tnt_a") == 5


def test_tampering_breaks_the_chain():
    """The point of the hash chain: an altered record cannot hide."""
    store = InMemoryAuditStore()
    ledger = AuditLedger(store)
    for i in range(3):
        ledger.record(AuditEvent.EXTRACT_COMMIT, tenant_id="tnt_a",
                      volume={"row_count": i})

    records = list(store.iter_records("tnt_a"))
    records[1].volume = {"row_count": 999_999}          # falsify a row count

    with pytest.raises(AuditChainBroken):
        ledger.verify("tnt_a")


def test_first_record_starts_from_genesis():
    ledger = AuditLedger(InMemoryAuditStore())
    rec = ledger.record(AuditEvent.CONNECTION_CREATED, tenant_id="tnt_a")
    assert rec.prev_hash == GENESIS
    assert rec.hash.startswith("sha256:")


def test_tenants_have_independent_chains():
    ledger = AuditLedger(InMemoryAuditStore())
    ledger.record(AuditEvent.TASK_LEASED, tenant_id="tnt_a")
    b = ledger.record(AuditEvent.TASK_LEASED, tenant_id="tnt_b")
    assert b.prev_hash == GENESIS, "tenant chains must not interleave"


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
def test_row_hash_excludes_envelope_and_is_order_independent():
    """Reconciliation compares hash sets. If the hash included envelope columns
    (which carry timestamps) nothing would ever match."""
    a = row_hash({"id": 1, "name": "x"})
    b = row_hash({"name": "x", "id": 1})
    c = row_hash({"id": 1, "name": "x", "_id360_extract_ts": "2026-07-20"})
    assert a == b == c


def test_envelope_carries_full_provenance(source, task):
    from id360_connect.core.models import BatchMeta
    meta = BatchMeta.create(task, schema_version=3,
                            classification=Classification.CONFIDENTIAL)
    wrapped = wrap({"id": 1}, meta=meta, op=Op.UPDATE,
                   position=Position("lsn", "0/1A"))
    assert wrapped["_id360_op"] == "U"
    assert wrapped["_id360_pii_class"] == "confidential"
    assert wrapped["_id360_schema_ver"] == 3
    assert wrapped["_id360_batch_id"] == meta.batch_id
    assert wrapped["_id360_row_hash"].startswith("sha256:")


# --------------------------------------------------------------------------- #
# Schema drift
# --------------------------------------------------------------------------- #
def test_evolve_accepts_additive_and_widening():
    old = Schema(fields=(Field("id", "int32"), Field("name", "string")))
    new = Schema(fields=(Field("id", "int64"), Field("name", "string"),
                         Field("region", "string")))
    report = diff(old, new)
    assert not report.breaking
    apply_posture(report, DriftPosture.EVOLVE)          # must not raise


def test_evolve_quarantines_a_dropped_column():
    old = Schema(fields=(Field("id", "int64"), Field("email", "string")))
    new = Schema(fields=(Field("id", "int64"),))
    report = diff(old, new)
    assert report.breaking
    with pytest.raises(Exception):
        apply_posture(report, DriftPosture.EVOLVE)


def test_strict_rejects_even_an_added_column():
    old = Schema(fields=(Field("id", "int64"),))
    new = Schema(fields=(Field("id", "int64"), Field("extra", "string")))
    with pytest.raises(Exception):
        apply_posture(diff(old, new), DriftPosture.STRICT)


def test_fingerprint_is_order_independent():
    a = Schema(fields=(Field("a", "int64"), Field("b", "string")))
    b = Schema(fields=(Field("b", "string"), Field("a", "int64")))
    assert a.fingerprint() == b.fingerprint()


# --------------------------------------------------------------------------- #
# Sink determinism
# --------------------------------------------------------------------------- #
def test_sink_path_is_deterministic(source, task):
    """Deterministic path + idempotent commit = safe replay after any crash."""
    from id360_connect.core.models import BatchMeta
    from id360_connect.sinks.base import Sink

    meta = BatchMeta.create(task, schema_version=1,
                            classification=Classification.INTERNAL)
    first = Sink.object_path(meta, root="s3://bucket/id360")
    second = Sink.object_path(meta, root="s3://bucket/id360")
    assert first == second
    assert meta.run_id in first and meta.batch_id in first
