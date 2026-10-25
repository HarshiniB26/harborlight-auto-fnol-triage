"""Small HTTP adapter for the supplied runtime API.

Only this module knows the runtime's ``/v1`` URLs. The rest of the application
calls named Python methods such as :meth:`get_policy` and never reads the
runtime's private ``dataset.json`` fixture.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class MockClient:
    """Call the supplied local insurance-system simulator."""

    def __init__(self, api_url, timeout=3):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None, idempotency_key=None, trace_id=None):
        """Send one JSON request and return ``(status_code, payload)``."""

        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if trace_id:
            headers["X-Trace-ID"] = trace_id

        request = Request(self.api_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as error:
            raw = error.read().decode()
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            return error.code, payload
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            return 503, {"error": "transport_unavailable"}

    def health(self, trace_id=None):
        """Return whether the supplied runtime is currently reachable."""

        return self._request("GET", "/health", trace_id=trace_id)

    def list_cases(self, trace_id=None):
        """Return the case summaries exposed by ``GET /v1/cases``."""

        status, payload = self._request("GET", "/v1/cases", trace_id=trace_id)
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("The case list is unavailable.")
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise RuntimeError("The case list is unavailable.")
        return cases

    def get_policy(self, case_id, trace_id=None):
        """Retrieve a case's policy evidence."""

        return self._get_case_tool(case_id, "policy", trace_id)

    def get_claims(self, case_id, trace_id=None):
        """Retrieve a case's claims-history evidence."""

        return self._get_case_tool(case_id, "claims", trace_id)

    def _get_case_tool(self, case_id, name, trace_id=None):
        encoded_case = quote(case_id, safe="")
        encoded_name = quote(name, safe="")
        return self._request(
            "GET", f"/v1/cases/{encoded_case}/{encoded_name}", trace_id=trace_id
        )

    def get_review(self, review_id, trace_id=None):
        """Retrieve the runtime's current status for a submitted review."""

        return self._request(
            "GET", f"/v1/reviews/{quote(review_id, safe='')}", trace_id=trace_id
        )

    def get_record(self, case_id, trace_id=None):
        """Return a live record or ``None`` when the logical loss is unfiled."""

        status, payload = self._request(
            "GET", f"/v1/records/{quote(case_id, safe='')}", trace_id=trace_id
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("The record ledger is unavailable.")
        return payload

    def submit_review(self, case_id, draft, trace_id=None):
        """Submit the exact generated draft for external human approval."""

        return self._request(
            "POST",
            "/v1/reviews",
            {"case_id": case_id, "draft": draft},
            trace_id=trace_id,
        )

    def create_record(self, case_id, review_id, draft, idempotency_key, trace_id=None):
        """Request one idempotent record from an exact approved draft."""

        return self._request(
            "POST",
            "/v1/records",
            {"case_id": case_id, "review_id": review_id, "draft": draft},
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
