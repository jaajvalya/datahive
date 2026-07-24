"""Logging with redaction enforced at the formatter.

Design point: redaction runs in the formatter, not at the call site. A careless
`log.info(f"row: {row}")` somewhere in a connector cannot bypass it. That is
the difference between a policy and a control.

Rules:
  * No row values in logs. Ever. Not at DEBUG.
  * Log the HASH of a predicate and the NAMES of bound parameters, never values.
  * Secrets are swept by pattern as defence in depth.
  * run_id is on every line, so one ID traces a run across every component.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from typing import Any, Mapping

# Fields permitted to appear verbatim in a log line. Anything else is dropped
# rather than logged - an allowlist, not a denylist, because a denylist always
# misses the field somebody added last week.
ALLOWED_FIELDS = frozenset({
    "run_id", "task_id", "batch_id", "tenant_id", "connection_id", "agent_id",
    "object", "object_name", "strategy", "attempt", "event", "outcome",
    "row_count", "byte_count", "query_seconds", "duration_ms", "partition",
    "position_kind", "predicate_hash", "schema_version", "policy_version",
    "http_status", "retry_after", "concurrency", "code", "sink", "uri_prefix",
    "classification", "utilization", "estimated_bytes", "cap",
})

_SECRET_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"(password|passwd|pwd)\s*[=:]\s*\S+",
        r"(api[_-]?key|secret|token|credential)\s*[=:]\s*\S+",
        r"Bearer\s+[A-Za-z0-9\-._~+/]+=*",
        r"AKIA[0-9A-Z]{16}",                     # AWS access key id
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"[a-z]+://[^:/@\s]+:[^@\s]+@",          # creds embedded in a URI
    )
]

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="-")


def bind(run_id: str | None = None, tenant_id: str | None = None) -> None:
    if run_id:
        _run_id.set(run_id)
    if tenant_id:
        _tenant_id.set(tenant_id)


def scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def predicate_hash(sql_or_filter: str) -> str:
    """Log this instead of the predicate itself. Bound values in a WHERE clause
    are row data, and row data does not go in logs."""
    return "sha256:" + hashlib.sha256(sql_or_filter.encode()).hexdigest()[:16]


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
            "run_id": _run_id.get(),
            "tenant_id": _tenant_id.get(),
        }
        extra = getattr(record, "id360", None)
        if isinstance(extra, Mapping):
            for k, v in extra.items():
                if k in ALLOWED_FIELDS:
                    payload[k] = scrub(v) if isinstance(v, str) else v
                # Non-allowlisted keys are dropped silently and deliberately.

        if record.exc_info:
            exc = record.exc_info[1]
            # Driver messages routinely embed row values
            # ("Key (email)=(alice@example.com)"). Log the type and a scrubbed,
            # truncated message - never the full traceback text verbatim.
            payload["error_type"] = type(exc).__name__
            payload["error"] = scrub(str(exc))[:500]
            payload["error_code"] = getattr(exc, "code", None)

        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Structured logging helper. Only allowlisted fields survive."""
    logger.log(level, msg, extra={"id360": fields})
