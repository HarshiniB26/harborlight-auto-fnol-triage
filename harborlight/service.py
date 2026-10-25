"""Local operator HTTP service. Binds to loopback unless HOST is set for a container."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harborlight.enterprise import EnterpriseClient
from harborlight.idempotency import creation_block, idempotency_key
from harborlight.observe import Tracer, render_prometheus, snapshot
from harborlight.resilience import UpstreamUnavailable
from harborlight.workflow import Workflow


class App:
    def __init__(self, data_dir: Path, enterprise_url: str, trace_path: Path, *, retry_base: float = 0.05):
        self.data_dir = data_dir
        self.tracer = Tracer(trace_path)
        self.client = EnterpriseClient(enterprise_url, self.tracer, base_delay=retry_base)
        self.workflow = Workflow(data_dir, self.client, self.tracer)
        self.drafts: dict[str, dict] = {}

    def draft(self, case_id: str | None = None) -> list[dict]:
        cases = self.client.list_cases()
        if case_id:
            cases = [item for item in cases if item["case_id"] == case_id]
            if not cases:
                raise KeyError(case_id)
        results = []
        for case in cases:
            item = self.workflow.draft_case(case)
            self.drafts[item["case_id"]] = item
            results.append(item)
        return results


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def _json(self, code: int, body) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read(self) -> dict:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 262144:
                raise ValueError("body_size")
            if size == 0:
                return {}
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise TypeError("object_required")
            return body

        def do_GET(self):
            if self.path == "/health":
                try:
                    upstream = app.client.health()
                except (UpstreamUnavailable, OSError, TimeoutError) as exc:
                    return self._json(503, {"status": "degraded", "upstream": type(exc).__name__})
                return self._json(200, {"status": "ok", "upstream": upstream, "service": "harborlight-fnol"})
            if self.path == "/metrics":
                raw = render_prometheus().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/v1/metrics":
                return self._json(200, snapshot())
            return self._json(404, {"error": "not_found"})

        def do_POST(self):
            try:
                body = self._read()
            except (ValueError, TypeError, json.JSONDecodeError):
                return self._json(400, {"error": "invalid_json"})
            if self.path == "/v1/drafts":
                try:
                    drafts = app.draft(body.get("case_id"))
                except KeyError:
                    return self._json(404, {"error": "unknown_case"})
                except (UpstreamUnavailable, OSError, TimeoutError, RuntimeError) as exc:
                    app.tracer.event("draft_failed", error=type(exc).__name__)
                    return self._json(503, {"error": "draft_unavailable"})
                return self._json(200, drafts)
            if self.path == "/v1/reviews":
                case_id = body.get("case_id")
                draft = app.drafts.get(case_id) if isinstance(case_id, str) else None
                if not draft:
                    return self._json(409, {"error": "draft_required"})
                try:
                    review = app.client.request_review(case_id, draft)
                except (UpstreamUnavailable, OSError, TimeoutError) as exc:
                    return self._json(502, {"error": type(exc).__name__})
                return self._json(201, review)
            if self.path == "/v1/records":
                case_id = body.get("case_id")
                review_id = body.get("review_id")
                draft = app.drafts.get(case_id) if isinstance(case_id, str) else None
                if not isinstance(review_id, str) or not draft:
                    return self._json(400, {"error": "draft_and_review_required"})
                blocked = creation_block(draft)
                if blocked:
                    return self._json(409, {"error": blocked})
                key = body.get("idempotency_key")
                if not isinstance(key, str) or not key:
                    key = idempotency_key(case_id, draft)
                status, result = app.client.create_record(case_id, review_id, draft, key)
                return self._json(status, result)
            return self._json(404, {"error": "not_found"})

    return Handler


def serve(app: App, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(json.dumps({"event": "listening", "host": host, "port": port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
