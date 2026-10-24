"""HTTP client for the fictional enterprise mock. Reads tools; never approves."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from harborlight.observe import Tracer, incr
from harborlight.redact import redact
from harborlight.resilience import (
    CircuitBreaker,
    TransientUpstreamError,
    UpstreamUnavailable,
    call_with_retries,
)


@dataclass
class ToolResult:
    ok: bool
    status: int
    body: dict | None
    attempts: int
    error: str | None


class EnterpriseClient:
    def __init__(self, base_url: str, tracer: Tracer, *, max_attempts: int = 3, base_delay: float = 0.05):
        self.base_url = base_url.rstrip("/")
        self.tracer = tracer
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    def _breaker(self, case_id: str, tool: str) -> CircuitBreaker:
        key = (case_id, tool)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker()
        return self._breakers[key]

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read().decode()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"error": "invalid_upstream_body"}
            if exc.code == 503:
                raise TransientUpstreamError(parsed.get("error", "upstream_timeout"), 503) from exc
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TransientUpstreamError("connection_failed") from exc

    def health(self) -> dict:
        status, body = self._request("GET", "/health")
        if status != 200:
            raise UpstreamUnavailable(f"health_{status}")
        return body

    def list_cases(self) -> list[dict]:
        status, body = self._request("GET", "/v1/cases")
        if status != 200 or not isinstance(body.get("cases"), list):
            raise UpstreamUnavailable("cases_unavailable")
        return body["cases"]

    def read_tool(self, case_id: str, tool: str) -> ToolResult:
        attempts = {"n": 0}

        def once(_attempt: int):
            attempts["n"] += 1
            incr("tool_calls_total")
            if attempts["n"] > 1:
                incr("tool_retries_total")
            status, body = self._request("GET", f"/v1/cases/{case_id}/{tool}")
            return status, body

        breaker = self._breaker(case_id, tool)
        try:
            status, body = call_with_retries(
                once,
                max_attempts=self.max_attempts,
                base_delay=self.base_delay,
                breaker=breaker,
            )
        except UpstreamUnavailable as exc:
            incr("tool_failures_total")
            if "circuit_open" in str(exc):
                incr("circuit_open_total")
            self.tracer.event(
                "tool_read_failed",
                case_id=case_id,
                tool=tool,
                attempts=attempts["n"],
                error=str(exc),
            )
            return ToolResult(False, 503, None, attempts["n"], str(exc))
        self.tracer.event(
            "tool_read",
            case_id=case_id,
            tool=tool,
            attempts=attempts["n"],
            status=status,
            source_id=(body or {}).get("source_id") if isinstance(body, dict) else None,
        )
        if status != 200:
            return ToolResult(False, status, body if isinstance(body, dict) else None, attempts["n"], f"http_{status}")
        return ToolResult(True, status, redact(body), attempts["n"], None)

    def get_record(self, case_id: str) -> dict | None:
        status, body = self._request("GET", f"/v1/records/{case_id}")
        if status == 404:
            return None
        if status != 200:
            raise UpstreamUnavailable(f"records_{status}")
        return body

    def request_review(self, case_id: str, draft: dict) -> dict:
        status, body = self._request("POST", "/v1/reviews", {"case_id": case_id, "draft": draft})
        if status != 201:
            raise UpstreamUnavailable(body.get("error", f"review_{status}"))
        self.tracer.event("review_requested", case_id=case_id, review_id=body.get("review_id"))
        return body

    def get_review(self, review_id: str) -> dict:
        status, body = self._request("GET", f"/v1/reviews/{review_id}")
        if status != 200:
            raise UpstreamUnavailable("review_missing")
        return body

    def create_record(self, case_id: str, review_id: str, draft: dict, idempotency_key: str) -> tuple[int, dict]:
        status, body = self._request(
            "POST",
            "/v1/records",
            {"case_id": case_id, "review_id": review_id, "draft": draft},
            {"Idempotency-Key": idempotency_key},
        )
        self.tracer.event(
            "record_write",
            case_id=case_id,
            review_id=review_id,
            status=status,
            error=None if status in (200, 201) else body.get("error"),
        )
        if status == 201:
            incr("records_created_total")
        if status == 409 and body.get("error") == "duplicate_case":
            incr("records_duplicate_total")
        return status, body
