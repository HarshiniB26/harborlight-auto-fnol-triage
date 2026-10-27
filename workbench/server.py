"""Loopback workbench server and the handler page."""
import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .auth import (
    assert_configured,
    clear_cookie_header,
    cookie_header,
    login,
    public_session,
    session_from_cookie,
)
from .service import Workbench

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent / "web"
PAGES = {"app.js": "text/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8"}


def create_server(workbench, host="127.0.0.1", port=8080):
    """Create the browser-facing server; the supplied runtime stays internal."""

    assert_configured()

    class Handler(BaseHTTPRequestHandler):
        """Serve the static UI and translate ``/api`` actions into service calls."""

        def log_message(self, *_):
            pass

        def do_GET(self):
            self.trace_id = self.headers.get("X-Trace-ID") or "tr_" + uuid.uuid4().hex[:16]
            path = urlsplit(self.path).path
            if path == "/":
                return self._file(WEB / "index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                if name in PAGES:
                    return self._file(WEB / name, PAGES[name])
            if path == "/api/session":
                return self._json(200, public_session(self.headers.get("Cookie")))
            if path == "/health":
                return self._json(200, workbench.health())
            if path == "/ready":
                result = workbench.readiness(self.trace_id)
                return self._json(200 if result["status"] == "ready" else 503, result)
            if path == "/metrics":
                return self._json(200, workbench.metrics())
            if path == "/api/results":
                if not self._authorized():
                    return None
                try:
                    result = workbench.export_results()
                except RuntimeError as error:
                    return self._unavailable(error)
                code = 409 if isinstance(result, dict) and result.get("error") else 200
                return self._json(code, result)
            if path == "/api/cases":
                if not self._authorized():
                    return None
                try:
                    return self._json(200, {"cases": workbench.list_cases()})
                except RuntimeError as error:
                    return self._unavailable(error)
            if path.startswith("/api/cases/"):
                if not self._authorized():
                    return None
                case_id = path.removeprefix("/api/cases/")
                if "/" not in case_id:
                    try:
                        return self._json(200, workbench.case_view(case_id, self.trace_id))
                    except KeyError:
                        return self._json(404, {"error": "case_not_found", "message": "This case is not in the current list."})
                    except RuntimeError as error:
                        return self._unavailable(error)
            return self._json(404, {"error": "not_found", "message": "That page does not exist."})

        def do_POST(self):
            self.trace_id = self.headers.get("X-Trace-ID") or "tr_" + uuid.uuid4().hex[:16]
            path = urlsplit(self.path).path
            try:
                size = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return self._json(400, {"error": "invalid_content_length", "message": "Content-Length must be an integer."})
            if size > 262144:
                return self._json(413, {"error": "body_too_large", "message": "Request exceeds 256 KiB."})
            raw = self.rfile.read(size) if size else b""
            if path == "/api/login":
                return self._login(raw)
            if path == "/api/logout":
                return self._json(200, {"authenticated": False}, {"Set-Cookie": clear_cookie_header()})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] in {"process", "review", "record"}:
                if not self._authorized():
                    return None
                case_id, action = parts[2], parts[3]
                try:
                    if action == "process":
                        result = workbench.process(case_id, self.trace_id)
                    elif action == "review":
                        result = workbench.submit_review(case_id, self.trace_id)
                    else:
                        result = workbench.file_record(case_id, self.trace_id)
                except KeyError:
                    return self._json(404, {"error": "case_not_found", "message": "This case is not in the current list."})
                except RuntimeError as error:
                    return self._unavailable(error)
                code = 409 if isinstance(result, dict) and result.get("error") else 200
                return self._json(code, result)
            return self._json(404, {"error": "not_found", "message": "That action does not exist."})

        def _file(self, path, content_type):
            if not path.is_file():
                return self._json(404, {"error": "not_found", "message": "That page does not exist."})
            raw = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            if getattr(self, "trace_id", None):
                self.send_header("X-Trace-ID", self.trace_id)
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self):
            """Allow the request when auth is off, or when a handler session is present.

            Reviewer sessions may use the workbench. Neither role can approve;
            the workbench still has no approval method.
            """

            session = public_session(self.headers.get("Cookie"))
            if not session.get("auth_required"):
                return True
            if session.get("error") == "auth_unavailable":
                self._json(503, {
                    "error": "auth_unavailable",
                    "message": "Authentication is required but is not configured.",
                })
                return False
            current = session_from_cookie(self.headers.get("Cookie"))
            if not current or current.get("role") not in {"handler", "reviewer"}:
                self._json(401, {"error": "unauthorized", "message": "Sign in required."})
                return False
            return True

        def _login(self, raw):
            try:
                body = json.loads(raw or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._json(400, {"error": "invalid_json", "message": "Sign-in body must be JSON."})
            if not isinstance(body, dict):
                return self._json(400, {"error": "invalid_json", "message": "Sign-in body must be an object."})
            token = login(body.get("username"), body.get("password"))
            if not token:
                return self._json(401, {"error": "unauthorized", "message": "Sign-in failed."})
            return self._json(
                200,
                {"authenticated": True},
                {"Set-Cookie": cookie_header(token)},
            )

        def _json(self, code, body, headers=None):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if getattr(self, "trace_id", None):
                self.send_header("X-Trace-ID", self.trace_id)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def _unavailable(self, error):
            """Return a predictable response when the supplied runtime is down."""

            return self._json(503, {
                "error": "runtime_unavailable",
                "message": str(error),
            })

    return ThreadingHTTPServer((host, port), Handler)


def main():
    """Run the local handler workbench on loopback by default."""

    parser = argparse.ArgumentParser(description="Harborlight local FNOL workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--api", default="http://127.0.0.1:8767")
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "dev")
    parser.add_argument("--state", type=Path, default=ROOT / ".state" / "workbench.json")
    args = parser.parse_args()
    workbench = Workbench(args.api, args.data, args.state)
    server = create_server(workbench, args.host, args.port)
    workbench.observability.event(
        "server_started", host=args.host, port=args.port, service="fnol-workbench"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
