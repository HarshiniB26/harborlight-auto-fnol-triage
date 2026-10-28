"""AWS implementation of the supplied fictional ``/v1`` API.

Immutable supplied input stays as ``dataset.json`` in private S3. PostgreSQL
contains only mutable runtime state such as reviews, records, idempotency keys,
tool counters, and audit events.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import boto3
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from workbench.storage import database_url_from_environment


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def connect(database_url):
    return psycopg.connect(database_url, row_factory=dict_row)


def audit(connection, event, case_id="", **detail):
    connection.execute(
        "INSERT INTO fnol.audit(event, case_id, detail) VALUES (%s, %s, %s)",
        (event, case_id or None, Jsonb(detail)),
    )


class S3DatasetStore:
    """Load the unchanged dataset fixture from S3 with a short-lived cache."""

    def __init__(self, bucket, prefix="", *, client=None, cache_seconds=30):
        if not bucket:
            raise ValueError("EVIDENCE_BUCKET is required")
        self.bucket = bucket
        self.key = "/".join(
            part for part in (prefix.strip("/"), "dataset.json") if part
        )
        self.client = client or boto3.client("s3", region_name=os.getenv("AWS_REGION"))
        self.cache_seconds = cache_seconds
        self._loaded_at = 0.0
        self._dataset = None
        self._lock = threading.Lock()

    def get(self, *, force=False):
        """Return a validated dataset, refreshing it when the cache expires."""

        with self._lock:
            now = time.monotonic()
            if not force and self._dataset is not None and now - self._loaded_at < self.cache_seconds:
                return self._dataset
            response = self.client.get_object(Bucket=self.bucket, Key=self.key)
            dataset = json.loads(response["Body"].read())
            if not isinstance(dataset, dict) or not isinstance(dataset.get("cases"), list):
                raise RuntimeError("S3 dataset.json has an invalid shape")
            self._dataset = dataset
            self._loaded_at = now
            return dataset


def create_server(
    database_url,
    bucket,
    prefix="",
    host="127.0.0.1",
    port=8767,
    simulate_faults=False,
    *,
    s3_client=None,
    dataset_cache_seconds=30,
):
    """Create the cloud runtime while retaining the supplied HTTP contract."""

    datasets = S3DatasetStore(
        bucket,
        prefix,
        client=s3_client,
        cache_seconds=dataset_cache_seconds,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _decide_review(self, database, review_id, body):
            """Apply one explicit human review decision to a pending draft."""

            reviewer = body.get("reviewer")
            decision = body.get("decision")
            if (
                decision not in {"approved", "rejected"}
                or not isinstance(reviewer, str)
                or not reviewer.strip()
                or len(reviewer.strip()) > 80
            ):
                return self.reply(400, {"error": "reviewer_and_decision_required"})
            row = database.execute(
                "SELECT * FROM fnol.reviews WHERE id=%s FOR UPDATE", (review_id,)
            ).fetchone()
            if not row:
                return self.reply(404, {"error": "review_not_found"})
            if row["case_id"] != body.get("case_id"):
                return self.reply(409, {"error": "review_case_mismatch"})
            if row["status"] != "pending":
                return self.reply(409, {"error": "review_already_decided"})
            reviewer = reviewer.strip()
            database.execute(
                """
                UPDATE fnol.reviews
                SET status=%s, reviewer=%s, reviewed_at=CURRENT_TIMESTAMP
                WHERE id=%s
                """,
                (decision, reviewer, review_id),
            )
            audit(
                database,
                "human_" + decision,
                row["case_id"],
                review_id=review_id,
                reviewer=reviewer,
                draft_hash=row["draft_hash"],
                channel="workbench_ui",
            )
            return self.reply(
                200,
                {
                    "review_id": review_id,
                    "case_id": row["case_id"],
                    "status": decision,
                    "reviewer": reviewer,
                    "draft_hash": row["draft_hash"],
                },
            )

        def do_GET(self):
            request_url = urlsplit(self.path)
            path = request_url.path
            force_refresh = request_url.query == "refresh=1"
            try:
                dataset = datasets.get(force=force_refresh)
            except Exception:
                return self.reply(503, {"status": "not_ready", "error": "dataset_unavailable"})
            case_map = {case["case_id"]: case for case in dataset["cases"]}
            with connect(database_url) as database:
                if path == "/health":
                    return self.reply(
                        200,
                        {
                            "status": "ok",
                            "dataset": dataset.get("name"),
                            "fictional_training_only": True,
                        },
                    )
                if path == "/v1/cases":
                    cases = [
                        {
                            "case_id": case["case_id"],
                            "intake_source": case["intake_source"],
                            "documents": case["documents"],
                        }
                        for case in sorted(dataset["cases"], key=lambda item: item["case_id"])
                    ]
                    return self.reply(200, {"cases": cases})

                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["v1", "reviews"]:
                    row = database.execute(
                        """
                        SELECT id, case_id, draft, draft_hash AS hash, status, reviewer
                        FROM fnol.reviews WHERE id=%s
                        """,
                        (parts[2],),
                    ).fetchone()
                    return self.reply(200, row) if row else self.reply(404, {"error": "not_found"})
                if len(parts) == 3 and parts[:2] == ["v1", "records"]:
                    case = case_map.get(parts[2])
                    if not case:
                        return self.reply(404, {"error": "not_found"})
                    row = database.execute(
                        """
                        SELECT id, case_id, review_id, 'created' AS status
                        FROM fnol.records WHERE id=%s
                        """,
                        (case["logical_key"],),
                    ).fetchone()
                    return self.reply(200, row) if row else self.reply(404, {"error": "not_found"})
                if len(parts) != 4 or parts[:2] != ["v1", "cases"]:
                    return self.reply(404, {"error": "not_found"})

                case_id, tool_name = parts[2:]
                case = case_map.get(case_id)
                if not case or tool_name not in case.get("tools", {}):
                    return self.reply(404, {"error": "not_found"})
                payload = case["tools"][tool_name]
                attempt = database.execute(
                    """
                    INSERT INTO fnol.tool_calls(case_id, tool_name, attempts)
                    VALUES (%s, %s, 1)
                    ON CONFLICT (case_id, tool_name) DO UPDATE
                    SET attempts = fnol.tool_calls.attempts + 1
                    RETURNING attempts
                    """,
                    (case_id, tool_name),
                ).fetchone()["attempts"]
                failure = case.get("faults", {}).get(tool_name)
                should_fail = False
                if simulate_faults and failure:
                    failures = failure.get("failures")
                    should_fail = failures == "always" or attempt <= int(failures)
                audit(
                    database,
                    "tool_read",
                    case_id,
                    tool=tool_name,
                    attempt=attempt,
                    status=503 if should_fail else 200,
                )
                if should_fail:
                    return self.reply(
                        503,
                        {
                            "error": "upstream_timeout",
                            "retryable": True,
                            "message": "Simulated upstream timeout; never treat as no results.",
                        },
                    )
                return self.reply(200, payload)

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 262144:
                    return self.reply(413, {"error": "body_size"})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return self.reply(400, {"error": "invalid_json_object"})

            case_id = body.get("case_id")
            try:
                dataset = datasets.get()
            except Exception:
                return self.reply(503, {"error": "dataset_unavailable"})
            case_map = {case["case_id"]: case for case in dataset["cases"]}
            case = case_map.get(case_id)
            with connect(database_url) as database:
                if not case:
                    return self.reply(400, {"error": "unknown_case"})
                if self.path == "/v1/reviews":
                    draft = body.get("draft")
                    if not isinstance(draft, dict) or draft.get("case_id") != case_id:
                        return self.reply(400, {"error": "draft_case_id_required"})
                    review_id = "rev_" + uuid.uuid4().hex[:12]
                    draft_hash = digest(draft)
                    database.execute(
                        """
                        INSERT INTO fnol.reviews(id, case_id, draft, draft_hash, status)
                        VALUES (%s, %s, %s, %s, 'pending')
                        """,
                        (review_id, case_id, Jsonb(draft), draft_hash),
                    )
                    audit(
                        database,
                        "review_requested",
                        case_id,
                        review_id=review_id,
                        draft_hash=draft_hash,
                    )
                    return self.reply(
                        201,
                        {"review_id": review_id, "status": "pending", "draft_hash": draft_hash},
                    )
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["v1", "reviews"] and parts[3] == "decision":
                    return self._decide_review(database, parts[2], body)
                if self.path != "/v1/records":
                    audit(database, "unsupported_write_attempt", case_id, path=self.path)
                    return self.reply(404, {"error": "unsupported_operation"})

                if not dataset.get("allow_write"):
                    audit(database, "forbidden_write_attempt", case_id)
                    return self.reply(403, {"error": "read_only_mission"})
                draft, review_id = body.get("draft"), body.get("review_id")
                key = self.headers.get("Idempotency-Key", "")
                if not isinstance(draft, dict) or not isinstance(review_id, str) or not key or len(key) > 128:
                    return self.reply(400, {"error": "draft_review_and_idempotency_key_required"})
                if draft.get("case_id") != case_id:
                    return self.reply(400, {"error": "draft_case_mismatch"})
                fingerprint = digest(
                    {"case_id": case_id, "draft": draft, "review_id": review_id}
                )
                database.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                previous = database.execute(
                    "SELECT request_hash, response FROM fnol.idempotency_keys WHERE idempotency_key=%s",
                    (key,),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != fingerprint:
                        audit(database, "idempotency_conflict", case_id)
                        return self.reply(409, {"error": "idempotency_conflict"})
                    audit(database, "write_replayed", case_id)
                    return self.reply(200, previous["response"])
                review = database.execute(
                    "SELECT * FROM fnol.reviews WHERE id=%s",
                    (review_id,),
                ).fetchone()
                if (
                    not review
                    or review["case_id"] != case_id
                    or review["status"] != "approved"
                    or review["draft_hash"] != digest(draft)
                ):
                    audit(database, "unapproved_write_attempt", case_id, review_id=review_id)
                    return self.reply(403, {"error": "approval_missing_or_draft_changed"})

                logical_key = case["logical_key"]
                database.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (logical_key,))
                existing = database.execute(
                    "SELECT id FROM fnol.records WHERE id=%s", (logical_key,)
                ).fetchone()
                if existing:
                    audit(
                        database,
                        "duplicate_prevented",
                        case_id,
                        existing_record_id=logical_key,
                    )
                    return self.reply(
                        409,
                        {"error": "duplicate_case", "existing_record_id": logical_key},
                    )
                response = {
                    "record_id": logical_key,
                    "case_id": case_id,
                    "status": "created",
                    "review_id": review_id,
                }
                database.execute(
                    "INSERT INTO fnol.records(id, case_id, draft, review_id) VALUES (%s, %s, %s, %s)",
                    (logical_key, case_id, Jsonb(draft), review_id),
                )
                database.execute(
                    """
                    INSERT INTO fnol.idempotency_keys(idempotency_key, request_hash, response)
                    VALUES (%s, %s, %s)
                    """,
                    (key, fingerprint, Jsonb(response)),
                )
                audit(
                    database,
                    "record_created",
                    case_id,
                    record_id=logical_key,
                    review_id=review_id,
                    draft_hash=digest(draft),
                    reviewer=review["reviewer"],
                )
                return self.reply(201, response)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default=os.getenv("RUNTIME_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("RUNTIME_PORT", "8767")))
    serve.add_argument("--bucket", default=os.getenv("EVIDENCE_BUCKET"))
    serve.add_argument("--prefix", default=os.getenv("EVIDENCE_PREFIX", ""))
    for command in ("pending", "approve", "reject", "audit"):
        subparser = subparsers.add_parser(command)
        if command in {"approve", "reject"}:
            subparser.add_argument("review_id")
            subparser.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    database_url = database_url_from_environment()
    if args.command == "serve":
        server = create_server(
            database_url,
            args.bucket,
            args.prefix,
            args.host,
            args.port,
            os.getenv("ENABLE_SIMULATED_FAULTS", "false").lower() == "true",
        )
        print(f"PostgreSQL FNOL runtime at {args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    with connect(database_url) as database:
        if args.command in {"approve", "reject"}:
            status = "approved" if args.command == "approve" else "rejected"
            row = database.execute(
                "SELECT * FROM fnol.reviews WHERE id=%s FOR UPDATE", (args.review_id,)
            ).fetchone()
            if not row or row["status"] != "pending":
                sys.exit("Review must exist and be pending.")
            database.execute(
                """
                UPDATE fnol.reviews SET status=%s, reviewer=%s, reviewed_at=CURRENT_TIMESTAMP
                WHERE id=%s
                """,
                (status, args.reviewer, args.review_id),
            )
            audit(
                database,
                "human_" + status,
                row["case_id"],
                review_id=args.review_id,
                reviewer=args.reviewer,
                draft_hash=row["draft_hash"],
            )
            print(status)
        elif args.command == "pending":
            rows = database.execute(
                "SELECT id, case_id, draft_hash, created_at FROM fnol.reviews WHERE status='pending'"
            ).fetchall()
            print(json.dumps(rows, indent=2, default=str))
        elif args.command == "audit":
            rows = database.execute(
                "SELECT * FROM fnol.audit ORDER BY sequence"
            ).fetchall()
            print(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
