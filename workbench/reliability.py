"""Bounded retries, exponential backoff with jitter, and circuit breaking."""

from __future__ import annotations

import random
import threading
import time


class CircuitOpenError(RuntimeError):
    """Raised when a dependency is in its fail-fast cooldown period."""


class CircuitBreaker:
    """Open after repeated failed logical operations, then probe half-open."""

    def __init__(self, failure_threshold=2, reset_timeout=30.0, clock=None):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock or time.monotonic
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = None
        self._lock = threading.Lock()

    def before_call(self):
        """Allow a call, fail fast, or transition an expired circuit to half-open."""

        with self._lock:
            if self.state == "half_open":
                raise CircuitOpenError("dependency recovery probe is already running")
            if self.state != "open":
                return
            if self.clock() - self.opened_at >= self.reset_timeout:
                self.state = "half_open"
                return
            raise CircuitOpenError("dependency circuit is open")

    def success(self):
        """Close the circuit after a successful operation or recovery probe."""

        with self._lock:
            self.failure_count = 0
            self.state = "closed"
            self.opened_at = None

    def failure(self):
        """Count one exhausted logical operation and open at the threshold."""

        with self._lock:
            self.failure_count += 1
            if self.state == "half_open" or self.failure_count >= self.failure_threshold:
                self.state = "open"
                self.opened_at = self.clock()

    def snapshot(self):
        """Return state suitable for health output and tests."""

        with self._lock:
            return {"state": self.state, "failure_count": self.failure_count}


class ReliableCaller:
    """Execute retryable read operations without creating a retry storm."""

    def __init__(
        self,
        observability,
        *,
        max_attempts=3,
        base_delay=0.1,
        max_delay=1.0,
        jitter_ratio=0.2,
        failure_threshold=2,
        reset_timeout=30.0,
        sleep=None,
        random_value=None,
        clock=None,
    ):
        self.observability = observability
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter_ratio = jitter_ratio
        self.sleep = sleep or time.sleep
        self.random_value = random_value or random.random
        self.clock = clock or time.monotonic
        self.breakers = {
            "policy": CircuitBreaker(failure_threshold, reset_timeout, self.clock),
            "claims": CircuitBreaker(failure_threshold, reset_timeout, self.clock),
        }

    def call(self, dependency, operation, *, case_id, trace_id):
        """Return a successful dictionary or ``None`` after controlled failure."""

        breaker = self.breakers[dependency]
        try:
            breaker.before_call()
        except CircuitOpenError:
            self.observability.metrics.increment("fnol_circuit_rejected_total")
            circuit_state = breaker.snapshot()["state"]
            self.observability.event(
                "circuit_rejected",
                level="WARNING",
                trace_id=trace_id,
                case_id=case_id,
                dependency=dependency,
                circuit_state=circuit_state,
            )
            return None

        probe = breaker.snapshot()["state"] == "half_open"
        attempt_limit = 1 if probe else self.max_attempts

        for attempt in range(1, attempt_limit + 1):
            started = self.clock()
            self.observability.metrics.increment("fnol_tool_call_total")
            self.observability.event(
                "tool_call_started",
                trace_id=trace_id,
                case_id=case_id,
                dependency=dependency,
                attempt=attempt,
            )
            try:
                status, payload = operation(case_id, trace_id=trace_id)
            except (ConnectionError, TimeoutError, OSError):
                # Treat transport failures as dependency unavailability.
                # Never expose exception details in logs or UI.
                status, payload = 503, {
                    "error": "transport_unavailable"
                }

            duration_ms = (self.clock() - started) * 1000
            if status == 200 and isinstance(payload, dict):
                breaker.success()
                self.observability.event(
                    "tool_call_completed",
                    trace_id=trace_id,
                    case_id=case_id,
                    dependency=dependency,
                    attempt=attempt,
                    status=status,
                    duration_ms=round(duration_ms, 3),
                )
                return payload

            retryable = status in (502, 503, 504)

            if status == 429:
                # The upstream service has rate-limited this client.
                # Do not send immediate retries.
                self.observability.metrics.increment(
                    "fnol_tool_rate_limited_total"
                )
                self.observability.event(
                    "tool_call_rate_limited",
                    level="WARNING",
                    trace_id=trace_id,
                    case_id=case_id,
                    dependency=dependency,
                    attempt=attempt,
                )

                # Rate limiting does not mean the server is unhealthy.
                # However, a half-open recovery probe must be released.
                if probe:
                    breaker.failure()

                return None

            self.observability.metrics.increment("fnol_tool_failure_total")
            self.observability.event(
                "tool_call_failed",
                level="WARNING",
                trace_id=trace_id,
                case_id=case_id,
                dependency=dependency,
                attempt=attempt,
                status=status,
                retryable=retryable,
                duration_ms=round(duration_ms, 3),
            )
            if not retryable or attempt == attempt_limit:
                breaker.failure()
                if breaker.snapshot()["state"] == "open":
                    self.observability.metrics.increment("fnol_circuit_open_total")
                    self.observability.event(
                        "circuit_opened",
                        level="ERROR",
                        trace_id=trace_id,
                        case_id=case_id,
                        dependency=dependency,
                    )
                return None

            exponential = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
            delay = exponential + exponential * self.jitter_ratio * self.random_value()
            self.observability.metrics.increment("fnol_tool_retry_total")
            self.observability.event(
                "tool_call_retry_scheduled",
                level="WARNING",
                trace_id=trace_id,
                case_id=case_id,
                dependency=dependency,
                next_attempt=attempt + 1,
                backoff_ms=round(delay * 1000, 3),
            )
            self.sleep(delay)
        return None

    def health(self):
        """Expose circuit states without internal timing or sensitive values."""

        return {name: breaker.snapshot() for name, breaker in self.breakers.items()}
