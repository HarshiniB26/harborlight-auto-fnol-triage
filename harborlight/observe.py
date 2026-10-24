"""Structured logs, in-process metrics, and redacted traces."""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from harborlight.redact import redact

_lock = threading.Lock()
_metrics = {
    "drafts_total": 0,
    "drafts_failed": 0,
    "tool_calls_total": 0,
    "tool_retries_total": 0,
    "tool_failures_total": 0,
    "circuit_open_total": 0,
    "records_created_total": 0,
    "records_duplicate_total": 0,
    "injection_blocked_total": 0,
    "latency_ms_sum": 0.0,
    "latency_ms_count": 0,
}


class Tracer:
    def __init__(self, path: Path | None):
        self.path = path
        self.events: list[dict] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, name: str, **fields) -> None:
        record = {
            "at": datetime.now(UTC).isoformat(),
            "event": name,
            **redact(fields),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        print(line, flush=True)
        with _lock:
            self.events.append(record)
            if self.path:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    def span(self, name: str, **fields):
        return _Span(self, name, fields)


class _Span:
    def __init__(self, tracer: Tracer, name: str, fields: dict):
        self.tracer = tracer
        self.name = name
        self.fields = fields
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, _tb):
        elapsed = (time.perf_counter() - self.started) * 1000
        self.tracer.event(
            self.name,
            duration_ms=round(elapsed, 3),
            error=None if exc is None else type(exc).__name__,
            **self.fields,
        )
        return False


def incr(name: str, amount: float = 1) -> None:
    with _lock:
        _metrics[name] = _metrics.get(name, 0) + amount


def snapshot() -> dict:
    with _lock:
        data = dict(_metrics)
    count = data.get("latency_ms_count") or 0
    data["latency_ms_avg"] = round(data["latency_ms_sum"] / count, 3) if count else 0
    return data


def render_prometheus() -> str:
    lines = []
    for key, value in snapshot().items():
        lines.append(f"harborlight_{key} {value}")
    return "\n".join(lines) + "\n"
