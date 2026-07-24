"""Retry, backoff, and split-on-fail.

The one rule that matters: when a source sends `Retry-After`, honour it
EXACTLY. Do not substitute your own curve, do not shorten it, do not retry
early. Sustained violation gets your app throttled tenant-wide, which degrades
the provider's other integrations - and that is how a connector turns into an
incident on someone else's system.
"""
from __future__ import annotations

import functools
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence, TypeVar

from .budget import AdaptiveConcurrency
from .errors import (ID360Error, PartitionTooLarge, SourceThrottled,
                     SourceTimeout, SourceUnavailable)

T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: bool = True          # full jitter; avoids synchronized retry storms

    def delay_for(self, attempt: int) -> float:
        raw = min(self.max_delay, self.base_delay * (self.multiplier ** (attempt - 1)))
        return random.uniform(0, raw) if self.jitter else raw


def with_retry(policy: RetryPolicy | None = None,
               concurrency: AdaptiveConcurrency | None = None):
    """Decorator. Feeds throttle signals into the AIMD limiter so the system
    self-tunes to the source's real capacity."""
    policy = policy or RetryPolicy()

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last: Exception | None = None
            for attempt in range(1, policy.max_attempts + 1):
                try:
                    result = fn(*args, **kwargs)
                    if concurrency:
                        concurrency.on_success()
                    return result
                except SourceThrottled as exc:
                    last = exc
                    if concurrency:
                        concurrency.on_throttle()
                    # Honour the source's instruction verbatim.
                    time.sleep(exc.retry_after)
                except (SourceUnavailable, SourceTimeout) as exc:
                    last = exc
                    if concurrency:
                        concurrency.on_timeout()
                    if attempt == policy.max_attempts:
                        break
                    time.sleep(policy.delay_for(attempt))
                except ID360Error as exc:
                    if not exc.retryable or attempt == policy.max_attempts:
                        raise
                    last = exc
                    time.sleep(policy.delay_for(attempt))
            assert last is not None
            raise last
        return wrapper
    return decorate


# --------------------------------------------------------------------------- #
# Split-on-fail
# --------------------------------------------------------------------------- #
@dataclass
class Partition:
    """A half-open range [low, high). Independently retryable, which makes it
    the natural unit of both concurrency and recovery."""
    low: object
    high: object
    depth: int = 0

    def split(self) -> tuple["Partition", "Partition"]:
        try:
            mid = type(self.low)((int(self.low) + int(self.high)) // 2)
        except (TypeError, ValueError):
            raise PartitionTooLarge("partition is not numerically splittable")
        if mid in (self.low, self.high):
            raise PartitionTooLarge("partition cannot be split further")
        return (Partition(self.low, mid, self.depth + 1),
                Partition(mid, self.high, self.depth + 1))


def run_with_splitting(partitions: Sequence[Partition],
                       fn: Callable[[Partition], T],
                       *, max_depth: int = 8) -> Iterator[T]:
    """Execute partitions, halving any that time out or blow their byte budget.

    Large tables self-tune to a working chunk size without an operator having
    to guess magic numbers up front.
    """
    queue = list(partitions)
    while queue:
        part = queue.pop(0)
        try:
            yield fn(part)
        except (PartitionTooLarge, SourceTimeout):
            if part.depth >= max_depth:
                raise
            left, right = part.split()
            queue[:0] = [left, right]


def hash_partitions(n: int) -> list[Partition]:
    """Modulo-of-hash partitions for tables without a usable numeric key.

        WHERE MOD(ABS(HASHTEXT(pk::text)), :n) = :p
    """
    return [Partition(low=i, high=i + 1) for i in range(n)]


def range_partitions(low: int, high: int, n: int) -> list[Partition]:
    if n <= 1 or high <= low:
        return [Partition(low, high)]
    step = max(1, (high - low) // n)
    out, cur = [], low
    while cur < high:
        out.append(Partition(cur, min(cur + step, high)))
        cur += step
    return out
