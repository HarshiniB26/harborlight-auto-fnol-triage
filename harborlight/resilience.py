"""Retries with exponential backoff and jitter, plus a per-operation circuit breaker.

A circuit breaker here is scoped to one case and tool. The training mock injects
faults per case on a shared host, so a process-wide breaker would turn one
case outage into a false outage for every later case.
"""

import random
import time
from dataclasses import dataclass


class TransientUpstreamError(Exception):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


class UpstreamUnavailable(Exception):
    """Dependency failed its retry budget or the circuit is open."""


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    reset_timeout: float = 30.0
    failure_count: int = 0
    state: str = "closed"
    opened_at: float | None = None

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.state == "open":
            if self.opened_at is not None and now - self.opened_at >= self.reset_timeout:
                self.state = "half-open"
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.opened_at = now


def backoff_seconds(attempt: int, base: float) -> float:
    """attempt is zero-based. Wait grows  base, 2base, 4base, plus up to 20% jitter."""
    delay = base * (2**attempt)
    return delay + random.uniform(0, delay * 0.2)


def call_with_retries(func, *, max_attempts: int, base_delay: float, breaker: CircuitBreaker, sleep=time.sleep):
    """Run func up to max_attempts. Retry only TransientUpstreamError."""
    last: Exception | None = None
    for attempt in range(max_attempts):
        if not breaker.allow():
            raise UpstreamUnavailable("circuit_open")
        try:
            result = func(attempt)
        except TransientUpstreamError as exc:
            last = exc
            breaker.record_failure()
            if attempt + 1 >= max_attempts or not breaker.allow():
                break
            sleep(backoff_seconds(attempt, base_delay))
            continue
        else:
            breaker.record_success()
            return result
    raise UpstreamUnavailable(str(last) if last else "upstream_unavailable")
