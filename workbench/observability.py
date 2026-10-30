"""Safe structured logging and small in-process metrics for the workbench.

The capstone has no model calls, so traces describe deterministic workflow and
tool steps rather than hidden model reasoning.  Logs deliberately contain
identifiers and outcomes, never evidence bodies or tool response payloads.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone

RESTRICTED_KEYS = {
    "authorization",
    "password",
    "private_token",
    "secret",
    "token",
}


def _redact(value):
    """Recursively redact values whose field names are security-sensitive."""

    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in RESTRICTED_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and "SYNTHETIC-PRIVATE-" in value:
        return "[REDACTED]"
    return value


class JsonLogger:
    """Write one searchable JSON object per event."""

    def __init__(self, stream=None, clock=None):
        self.stream = stream if stream is not None else sys.stdout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def emit(self, event, *, level="INFO", **fields):
        """Emit a redacted structured event without logging evidence payloads."""

        record = {
            "timestamp": self.clock().isoformat().replace("+00:00", "Z"),
            "level": level,
            "event": event,
            **_redact(fields),
        }
        with self._lock:
            self.stream.write(json.dumps(record, sort_keys=True) + "\n")
            self.stream.flush()


class Metrics:
    """Collect the small set of counters and timings required by the SLO."""

    def __init__(self):
        self._counters = defaultdict(int)
        self._durations_ms = []
        self._lock = threading.Lock()

    def increment(self, name, amount=1):
        """Increment a named counter."""

        with self._lock:
            self._counters[name] += amount

    def observe_processing(self, duration_ms):
        """Record one end-to-end case-processing duration."""

        with self._lock:
            self._durations_ms.append(round(duration_ms, 3))

    def snapshot(self):
        """Return JSON-safe metrics, including the two SLO indicators."""

        with self._lock:
            counters = dict(sorted(self._counters.items()))
            durations = sorted(self._durations_ms)
        requested = counters.get("fnol_processing_total", 0)
        completed = counters.get("fnol_processing_completed_total", 0)
        within_target = counters.get("fnol_processing_within_15s_total", 0)
        p95 = None
        if durations:
            index = max(0, min(len(durations) - 1, (95 * len(durations) + 99) // 100 - 1))
            p95 = durations[index]
        return {
            "counters": counters,
            "slo": {
                "processing_requests": requested,
                "completion_rate": round(completed / requested, 4) if requested else None,
                "within_15s_rate": round(within_target / requested, 4) if requested else None,
                "p95_duration_ms": p95,
            },
        }


class Observability:
    """Bundle logging and metrics so tests can inject an isolated instance."""

    def __init__(self, logger=None, metrics=None):
        self.logger = logger or JsonLogger()
        self.metrics = metrics or Metrics()

    def event(self, name, *, level="INFO", **fields):
        """Record one event using the configured safe logger."""

        self.logger.emit(name, level=level, **fields)
