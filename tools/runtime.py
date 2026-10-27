"""Local fictional enterprise simulator. Python 3.12+.

SQLite remains the default state store. PostgreSQL and S3 are used only when
their environment settings are present. The workbench still cannot approve a
review; approval stays on this command-line interface.
"""

import argparse
import hashlib
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.dataset_loader import DatasetCache, DatasetError, DatasetInvalid, DatasetNotFound
from tools.state_store import canonical, open_store

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def create_server(data_dir, state, host="127.0.0.1", port=8765, *, dataset_cache=None, store=None):
    cache = dataset_cache or DatasetCache.from_env(data_dir)
    cache.get()
    store = store or open_store(state)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _dataset(self):
            return cache.get().dataset

        def do_GET(self):
            from urllib.parse import urlsplit

            path = urlsplit(self.path).path
            loaded = cache.get()
            dataset = loaded.dataset
            case_map = {item["case_id"]: item for item in dataset["cases"]}
            if path == "/health":
                return self.reply(200, {
                    "status": "ok",
                    "dataset": dataset["name"],
                    "dataset_version": loaded.version,
                    "fictional_training_only": True,
                })
            if path == "/v1/cases":
                return self.reply(200, {"cases": [
                    {
                        "case_id": item["case_id"],
                        "intake_source": item["intake_source"],
                        "documents": item["documents"],
                    }
                    for item in dataset["cases"]
                ]})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["v1", "reviews"]:
                row = store.get_review(parts[2])
                return self.reply(200, row) if row else self.reply(404, {"error": "not_found"})
            if len(parts) == 3 and parts[:2] == ["v1", "records"]:
                case = case_map.get(parts[2])
                if not case:
                    return self.reply(404, {"error": "not_found"})
                row = store.get_record(case["logical_key"])
                return self.reply(200, row) if row else self.reply(404, {"error": "not_found"})
            if len(parts) != 4 or parts[:2] != ["v1", "cases"]:
                return self.reply(404, {"error": "not_found"})
            case_id, tool = parts[2:]
            case = case_map.get(case_id)
            if not case or tool not in case["tools"]:
                return self.reply(404, {"error": "not_found"})
            _count, failed = store.record_tool_attempt(case_id, tool, case.get("faults", {}).get(tool))
            if failed:
                return self.reply(503, {
                    "error": "upstream_timeout",
                    "retryable": True,
                    "message": "Simulated upstream timeout; never treat as no results.",
                })
            return self.reply(200, case["tools"][tool])

        def do_POST(self):
            from urllib.parse import urlsplit

            path = urlsplit(self.path).path
            if path == "/internal/dataset/refresh":
                self._discard_body()
                return self._refresh_dataset()
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 262144:
                    return self.reply(413, {"error": "body_size"})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                return self.reply(400, {"error": "invalid_json_object"})
            dataset = self._dataset()
            case_map = {item["case_id"]: item for item in dataset["cases"]}
            case_id = body.get("case_id")
            if not isinstance(case_id, str) or case_id not in case_map:
                return self.reply(400, {"error": "unknown_case"})
            if path == "/v1/reviews":
                draft = body.get("draft")
                if not isinstance(draft, dict) or draft.get("case_id") != case_id:
                    return self.reply(400, {"error": "draft_case_id_required"})
                review_id = "rev_" + uuid.uuid4().hex[:12]
                store.insert_review(review_id, case_id, canonical(draft), digest(draft))
                return self.reply(201, {
                    "review_id": review_id,
                    "status": "pending",
                    "draft_hash": digest(draft),
                })
            if path != "/v1/records":
                store.audit("unsupported_write_attempt", case_id, path=path)
                return self.reply(404, {"error": "unsupported_operation"})
            if not dataset["allow_write"]:
                store.audit("forbidden_write_attempt", case_id)
                return self.reply(403, {"error": "read_only_mission"})
            draft, review_id = body.get("draft"), body.get("review_id")
            key = self.headers.get("Idempotency-Key", "")
            if not isinstance(draft, dict) or not isinstance(review_id, str) or not key or len(key) > 128:
                return self.reply(400, {"error": "draft_review_and_idempotency_key_required"})
            if draft.get("case_id") != case_id:
                return self.reply(400, {"error": "draft_case_mismatch"})
            code, payload = store.commit_record(
                case_id=case_id,
                draft_canonical=canonical(draft),
                draft_hash=digest(draft),
                review_id=review_id,
                key=key,
                fingerprint=digest({"case_id": case_id, "draft": draft, "review_id": review_id}),
                logical_key=case_map[case_id]["logical_key"],
                dataset_version=cache.get().version,
            )
            return self.reply(code, payload)

        def _discard_body(self):
            try:
                size = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return
            if 0 < size <= 262144:
                self.rfile.read(size)

        def _refresh_dataset(self):
            client_host = self.client_address[0]
            if client_host not in {"127.0.0.1", "::1"}:
                return self.reply(403, {"error": "refresh_is_local_only"})
            try:
                loaded = cache.refresh()
            except DatasetNotFound as error:
                return self.reply(404, {"error": "dataset_not_found", "message": str(error)})
            except DatasetInvalid as error:
                return self.reply(400, {"error": "dataset_invalid", "message": str(error)})
            except DatasetError as error:
                return self.reply(502, {"error": "dataset_unavailable", "message": str(error)})
            return self.reply(200, {
                "status": "refreshed",
                "dataset": loaded.name,
                "dataset_version": loaded.version,
            })

    return ThreadingHTTPServer((host, port), Handler)


def _reviewer_is_allowed(reviewer):
    from tools.aws_config import auth_required, load_auth_document

    if not auth_required():
        return True
    try:
        document = load_auth_document()
    except (OSError, RuntimeError, json.JSONDecodeError):
        sys.exit("Reviewer authorization is required but the auth configuration could not be read.")
    allowed = {
        user.get("username")
        for user in document.get("users", [])
        if isinstance(user, dict) and user.get("role") == "reviewer" and user.get("username")
    }
    if reviewer not in allowed:
        sys.exit("Reviewer is not authorized.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--data", type=Path, default=ROOT / "data" / "dev")
    serve.add_argument("--state", type=Path, default=ROOT / ".state" / "mock.sqlite")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    for name in ["pending", "approve", "reject", "audit", "reset"]:
        command = sub.add_parser(name)
        command.add_argument("--state", type=Path, default=ROOT / ".state" / "mock.sqlite")
        if name in ["approve", "reject"]:
            command.add_argument("review_id")
            command.add_argument("--reviewer", required=True)
        if name == "reset":
            command.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        server = create_server(args.data, args.state, host=args.host, port=args.port)
        print(f"Fictional training mock at http://{args.host}:{args.port}; Ctrl-C stops it.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    store = open_store(args.state)
    if args.command in ["approve", "reject"]:
        status = "approved" if args.command == "approve" else "rejected"
        _reviewer_is_allowed(args.reviewer)
        changed = store.transition_review(args.review_id, status, args.reviewer)
        if not changed:
            sys.exit("Review must exist and be pending.")
        print(status)
    elif args.command == "pending":
        print(json.dumps(store.list_pending(), indent=2))
    elif args.command == "audit":
        print(json.dumps(store.list_audit(), indent=2))
    elif args.command == "reset":
        store.reset()
        print("Reset local mock state. Stop all clients before resetting.")


if __name__ == "__main__":
    main()
