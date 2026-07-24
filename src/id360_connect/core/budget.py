"""Budget governor — the component that keeps you from becoming a cost incident.

Three mechanisms, all enforced by the framework rather than left to connector
authors:

1. Token buckets on four axes (rows, bytes, queries, API calls), rate-limited.
2. Cumulative caps per window - rate limits stop spikes, cumulative caps stop
   slow leaks. You need both.
3. AIMD adaptive concurrency - additive increase on success, multiplicative
   decrease on throttle. Same control law as TCP congestion control, and for
   the same reason: it discovers the source's real capacity without being told,
   and it backs off before the provider notices.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .errors import BudgetExceeded
from .models import Budget


class TokenBucket:
    """Thread-safe token bucket. `try_consume` never blocks; `consume` waits."""

    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_consume(self, n: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def consume(self, n: float = 1.0, *, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                # Tokens can exceed capacity only if the caller asks for more
                # than the bucket can ever hold - fail fast rather than hang.
                if n > self.capacity:
                    raise BudgetExceeded(
                        "request larger than bucket capacity",
                        requested=n, capacity=self.capacity)
                wait = (n - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                raise BudgetExceeded("timed out waiting for budget", wait_seconds=wait)
            time.sleep(min(wait, 0.25))


@dataclass
class CumulativeCounter:
    """Rolling window counter for per-day style caps."""
    window_seconds: float = 86400.0
    _events: list[tuple[float, float]] = field(default_factory=list)

    def add(self, amount: float) -> None:
        now = time.monotonic()
        self._events.append((now, amount))
        self._prune(now)

    def total(self) -> float:
        self._prune(time.monotonic())
        return sum(a for _, a in self._events)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._events = [(t, a) for t, a in self._events if t >= cutoff]


class AdaptiveConcurrency:
    """AIMD limiter. Grows by 1 on success, halves on throttle/timeout."""

    def __init__(self, initial: int = 2, ceiling: int = 16, floor: int = 1):
        self.ceiling, self.floor = ceiling, floor
        self._limit = max(floor, min(initial, ceiling))
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def on_success(self) -> None:
        with self._lock:
            self._limit = min(self.ceiling, self._limit + 1)

    def on_throttle(self) -> None:
        with self._lock:
            self._limit = max(self.floor, self._limit // 2)

    on_timeout = on_throttle


class BudgetGovernor:
    """Per-(connection, object) enforcement point.

    Every read path calls `before_query`, then `record_*`. A breach raises
    BudgetExceeded, which the executor turns into: cancel at source, park the
    job, notify both sides. The framework never silently exceeds a budget.
    """

    def __init__(self, budget: Budget, *, label: str = ""):
        self.budget, self.label = budget, label
        b = budget
        self.rows = TokenBucket(b.max_rows_per_second) if b.max_rows_per_second else None
        self.bytes = TokenBucket(b.max_bytes_per_second) if b.max_bytes_per_second else None
        self.queries = (TokenBucket(b.max_queries_per_minute / 60.0,
                                    capacity=b.max_queries_per_minute)
                        if b.max_queries_per_minute else None)
        self.api = (TokenBucket(b.max_api_calls_per_minute / 60.0,
                                capacity=b.max_api_calls_per_minute)
                    if b.max_api_calls_per_minute else None)
        self.daily_bytes = CumulativeCounter()
        self.daily_query_seconds = CumulativeCounter()
        self.concurrency = AdaptiveConcurrency(ceiling=b.max_concurrency)

    # -- pre-flight -------------------------------------------------------- #
    def check_estimate(self, estimated_bytes: int) -> None:
        """Called with the result of EXPLAIN / dry_run BEFORE reading anything.

        This is the single highest-value guardrail in the framework: it turns
        an accidental bill into a refused task.
        """
        cap = self.budget.max_bytes_per_day
        if cap is not None and self.daily_bytes.total() + estimated_bytes > cap:
            raise BudgetExceeded(
                "estimated read would exceed the daily byte budget",
                label=self.label, estimated_bytes=estimated_bytes,
                consumed=self.daily_bytes.total(), cap=cap)

    def before_query(self) -> None:
        if self.queries:
            self.queries.consume(1)

    def before_api_call(self, n: int = 1) -> None:
        if self.api:
            self.api.consume(n)

    # -- accounting -------------------------------------------------------- #
    def record_rows(self, n: int) -> None:
        if self.rows:
            self.rows.consume(n)

    def record_bytes(self, n: int) -> None:
        if self.bytes:
            self.bytes.consume(n)
        self.daily_bytes.add(n)
        cap = self.budget.max_bytes_per_day
        if cap is not None and self.daily_bytes.total() > cap:
            raise BudgetExceeded("daily byte budget exhausted",
                                 label=self.label, cap=cap)

    def record_query_seconds(self, seconds: float) -> None:
        self.daily_query_seconds.add(seconds)
        cap = self.budget.max_query_seconds_per_day
        if cap is not None and self.daily_query_seconds.total() > cap:
            raise BudgetExceeded("daily query-second budget exhausted",
                                 label=self.label, cap=cap)

    # -- reporting --------------------------------------------------------- #
    def utilization(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.budget.max_bytes_per_day:
            out["bytes"] = self.daily_bytes.total() / self.budget.max_bytes_per_day
        if self.budget.max_query_seconds_per_day:
            out["query_seconds"] = (self.daily_query_seconds.total()
                                    / self.budget.max_query_seconds_per_day)
        return out


class CircuitBreaker:
    """Scoped per (source, object) - NOT per source.

    One pathological table opens its own breaker while the other forty objects
    on the same connection keep flowing.
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 60.0):
        self.threshold, self.cooldown = threshold, cooldown_seconds
        self.state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.state == self.OPEN:
                if time.monotonic() - self._opened_at >= self.cooldown:
                    self.state = self.HALF_OPEN   # single probe request
                    return True
                return False
            return True

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self.state = self.CLOSED

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self.state == self.HALF_OPEN or self._failures >= self.threshold:
                self.state = self.OPEN
                self._opened_at = time.monotonic()
