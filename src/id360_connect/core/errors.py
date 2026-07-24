"""Framework exception hierarchy.

Two properties matter here:

1. Errors carry a stable machine-readable `code`. API consumers see the code,
   never the underlying driver message - source errors routinely contain row
   values (`Key (email)=(alice@example.com)`) and must not escape.
2. `retryable` and `retry_after` are explicit, so the retry layer never has to
   pattern-match on error strings.
"""
from __future__ import annotations


class ID360Error(Exception):
    code = "id360.error"
    retryable = False
    http_status = 500

    def __init__(self, message: str = "", *, detail: str | None = None, **ctx):
        super().__init__(message or self.code)
        self.message = message or self.code
        #: Internal-only. Logged under redaction; NEVER returned to a caller.
        self.detail = detail
        self.ctx = ctx

    def public(self) -> dict:
        """Safe representation for an API response."""
        return {"code": self.code, "message": self.message}


# ---------------------------------------------------------------- config ----
class ConfigError(ID360Error):
    code = "id360.config.invalid"
    http_status = 400


class UnknownObjectError(ConfigError):
    code = "id360.config.unknown_object"
    http_status = 404


# ---------------------------------------------------------------- policy ----
class PolicyDenied(ID360Error):
    """A denial is a first-class audit event, not just an error."""
    code = "id360.policy.denied"
    http_status = 403


class BudgetExceeded(ID360Error):
    code = "id360.budget.exceeded"
    http_status = 429


class BlackoutWindow(ID360Error):
    code = "id360.policy.blackout"
    retryable = True
    http_status = 409


# ---------------------------------------------------------------- source ----
class SourceUnavailable(ID360Error):
    code = "id360.source.unavailable"
    retryable = True
    http_status = 503


class SourceThrottled(ID360Error):
    """Raised on 429/503 with Retry-After. The value is honoured exactly - we
    never substitute our own backoff curve for the source's instruction."""
    code = "id360.source.throttled"
    retryable = True
    http_status = 429

    def __init__(self, message: str = "", *, retry_after: float = 1.0, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class SourceTimeout(ID360Error):
    code = "id360.source.timeout"
    retryable = True
    http_status = 504


class PartitionTooLarge(ID360Error):
    """Signals the split-on-fail path: halve the partition and retry."""
    code = "id360.source.partition_too_large"
    retryable = True


class InsecureTransport(ID360Error):
    """The source offered plaintext. This is a hard failure requiring an
    explicit, documented operator override - never a silent downgrade."""
    code = "id360.security.insecure_transport"


# -------------------------------------------------------------- position ----
class PositionInvalid(ID360Error):
    """The resume position is no longer in the source's retention window.

    CRITICAL: must trigger a re-snapshot and an alert. Silently resuming from
    the earliest available position causes undetectable data loss.
    """
    code = "id360.position.invalid"


class PositionRegression(ID360Error):
    code = "id360.position.regression"


class ResyncRequired(PositionInvalid):
    """Graph 410 Gone / Drive token expiry / oplog rollover."""
    code = "id360.position.resync_required"


# ---------------------------------------------------------------- schema ----
class SchemaDrift(ID360Error):
    code = "id360.schema.drift"

    def __init__(self, message: str = "", *, added=(), removed=(), changed=(), **kw):
        super().__init__(message, **kw)
        self.added, self.removed, self.changed = list(added), list(removed), list(changed)


# ------------------------------------------------------------------ sink ----
class SinkUnavailable(ID360Error):
    code = "id360.sink.unavailable"
    retryable = True
    http_status = 503


class CommitFailed(ID360Error):
    code = "id360.sink.commit_failed"
    retryable = True


# ------------------------------------------------------------- integrity ----
class ReconciliationFailed(ID360Error):
    code = "id360.reconcile.failed"


class AuditChainBroken(ID360Error):
    """The hash chain does not verify. Treat as a security incident."""
    code = "id360.audit.chain_broken"
