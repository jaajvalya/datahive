"""Connector base class and the strategy resolver.

A connector is source-specific I/O ONLY. It does not schedule, does not own
state, does not decide policy, does not touch crypto. Everything else lives in
the substrate. Keeping this boundary strict is what stops eight source families
from becoming eight codebases.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from ..core.budget import BudgetGovernor, CircuitBreaker
from ..core.errors import ConfigError, PolicyDenied, UnknownObjectError
from ..core.models import (PREFERENCE, ExtractionTask, ObjectPolicy, Op,
                           Position, SourceRef, Strategy)
from ..core.schema import Schema


@dataclass
class ReadResult:
    """What a connector hands back for one batch."""
    records: Sequence[Mapping[str, Any]]
    op: Op = Op.REFRESH
    position: Position | None = None
    source_bytes: int = 0
    query_seconds: float = 0.0
    is_last: bool = False


class Connector(abc.ABC):
    """Base for all connectors."""

    kind: str = "base"

    def __init__(self, source: SourceRef, credential, *,
                 governor: BudgetGovernor | None = None,
                 breaker: CircuitBreaker | None = None):
        self.source = source
        self.credential = credential
        self.governor = governor or BudgetGovernor(source.budget,
                                                   label=source.connection_id)
        self.breaker = breaker or CircuitBreaker()

    # -- lifecycle --------------------------------------------------------- #
    def connect(self) -> None: ...
    def close(self) -> None: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- required ---------------------------------------------------------- #
    @abc.abstractmethod
    def discover(self) -> Sequence[str]:
        """List readable object names. Must respect the registry allowlist."""

    @abc.abstractmethod
    def fetch_schema(self, object_name: str) -> Schema:
        """Metadata-only. Called before every run so drift is caught before a
        single row is read."""

    @abc.abstractmethod
    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        """Yield batches. Must honour the governor and the statement timeout."""

    # -- optional ---------------------------------------------------------- #
    def estimate_bytes(self, task: ExtractionTask) -> int | None:
        """Pre-flight cost estimate (EXPLAIN / dry_run / manifest sum).

        Returning None means "cannot estimate" - the governor then falls back
        to enforcing caps during the read rather than before it.
        """
        return None

    def current_position(self, object_name: str) -> Position | None:
        """The source's current head, for lag calculation."""
        return None

    def cancel(self) -> None:
        """Cancel the in-flight query SERVER-SIDE.

        Dropping the connection is not enough: many engines keep executing an
        orphaned query, and now the provider is paying for compute that nobody
        will ever read.
        """

    # -- shared helpers ---------------------------------------------------- #
    def policy_for(self, object_name: str) -> ObjectPolicy:
        policy = self.source.objects.get(object_name)
        if policy is None:
            # Default deny. An object not in the registry is not readable, full
            # stop - this is what makes the allowlist meaningful.
            raise PolicyDenied("object is not in the registered allowlist",
                               object_name=object_name)
        return policy

    def projected_columns(self, object_name: str, schema: Schema) -> Sequence[str]:
        """Column projection from the policy contract.

        `SELECT *` is banned by the framework. On columnar engines this is the
        single largest cost lever available, and on a masked column it is the
        difference between a control and a leak.
        """
        policy = self.policy_for(object_name)
        available = [f.name for f in schema.fields]
        if policy.columns:
            missing = set(policy.columns) - set(available)
            if missing:
                raise UnknownObjectError("policy names columns absent from source",
                                         object_name=object_name,
                                         missing=sorted(missing))
            return list(policy.columns)
        return [c for c in available if c not in policy.masked_columns]

    def masked_expression(self, column: str, rule: str) -> str:
        """Masking is applied IN THE SOURCE QUERY, never client-side.

        Fetch-then-drop is not a control - it is a leak with extra steps. If we
        are not allowed to hold the value, we never fetch it.
        """
        rules = {
            "sha256": f"SHA2({column}, 256)",
            "null": "NULL",
            "redact": "'[REDACTED]'",
            "last4": f"RIGHT({column}, 4)",
            "domain_only": f"SUBSTRING({column} FROM POSITION('@' IN {column}) + 1)",
            "year_only": f"DATE_TRUNC('year', {column})",
        }
        if rule not in rules:
            raise ConfigError(f"unknown masking rule {rule!r}")
        return f"{rules[rule]} AS {column}"


# --------------------------------------------------------------------------- #
# Strategy resolution
# --------------------------------------------------------------------------- #
def resolve_strategy(source: SourceRef, object_name: str,
                     *, force: Strategy | None = None,
                     bootstrap: bool = False) -> Strategy:
    """Pick the cheapest strategy the source can actually support.

    Reads ONLY the capabilities recorded at onboarding - it never guesses from
    the source kind. If a capability was not probed and confirmed by a human,
    it does not exist as far as the resolver is concerned.
    """
    if force is not None:
        return force

    caps = source.capabilities

    if bootstrap:
        # Bootstrap prefers a bulk path; full snapshot only as a last resort.
        if caps.supports_storage_native or caps.supports_bulk_unload:
            return Strategy.STORAGE_NATIVE
        return Strategy.FULL_SNAPSHOT

    supported = {
        Strategy.STORAGE_NATIVE: caps.supports_storage_native or caps.supports_bulk_unload,
        Strategy.LOG_CDC: caps.supports_log_cdc,
        Strategy.CHANGE_FEED: caps.supports_change_feed,
        # A watermark column that is not indexed is a full table scan wearing a
        # costume. Refuse to call it incremental.
        Strategy.WATERMARK: bool(caps.watermark_column) and caps.watermark_indexed,
        Strategy.FULL_SNAPSHOT: True,
    }

    for strategy in PREFERENCE:
        if supported.get(strategy):
            return strategy
    return Strategy.FULL_SNAPSHOT


def explain_resolution(source: SourceRef, object_name: str) -> dict[str, Any]:
    """Human-readable justification. Surface this at onboarding so an operator
    can see WHY a source ended up on an expensive strategy - that is usually
    the moment to go back and negotiate for a cheaper one."""
    caps = source.capabilities
    chosen = resolve_strategy(source, object_name)
    notes = []
    if chosen is Strategy.FULL_SNAPSHOT:
        notes.append("No incremental path available. Escalate to the provider: "
                     "an index on a change column, CDC access, or a change feed "
                     "is cheap for them and expensive for you to work around.")
    if caps.watermark_column and not caps.watermark_indexed:
        notes.append(f"Watermark column {caps.watermark_column!r} is NOT indexed - "
                     "an incremental predicate on it is a full scan.")
    if not caps.read_replica_available and source.kind.value == "database":
        notes.append("No read replica registered. Ask for one before going live.")
    return {"object": object_name, "strategy": chosen.value,
            "capabilities": vars(caps), "notes": notes}
