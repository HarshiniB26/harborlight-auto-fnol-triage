import io
import json
import runpy
import sys
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools import aws_runtime


class Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class S3:
    def __init__(self, dataset):
        self.dataset = dataset
        self.fail = False
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("s3 unavailable")
        return {"Body": Body(json.dumps(self.dataset).encode())}


class Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class Database:
    def __init__(self):
        self.reviews = {}
        self.records = {}
        self.keys = {}
        self.calls = {}
        self.audit_rows = [{"event": "existing"}]
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params=()):
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params))
        if "RETURNING attempts" in sql:
            key = tuple(params)
            self.calls[key] = self.calls.get(key, 0) + 1
            return Result({"attempts": self.calls[key]})
        if "SELECT id, case_id, draft, draft_hash AS hash" in sql:
            return Result(self.reviews.get(params[0]))
        if "SELECT id, case_id, review_id, 'created' AS status" in sql:
            return Result(self.records.get(params[0]))
        if "INSERT INTO fnol.reviews" in sql:
            review_id, case_id, draft, draft_hash = params
            self.reviews[review_id] = {
                "id": review_id, "case_id": case_id, "draft": draft.obj,
                "draft_hash": draft_hash, "hash": draft_hash,
                "status": "pending", "reviewer": None,
            }
            return Result()
        if "SELECT * FROM fnol.reviews WHERE id=%s FOR UPDATE" in sql:
            return Result(self.reviews.get(params[0]))
        if "UPDATE fnol.reviews" in sql:
            status, reviewer, review_id = params
            self.reviews[review_id].update(status=status, reviewer=reviewer)
            return Result()
        if "SELECT request_hash, response" in sql:
            return Result(self.keys.get(params[0]))
        if "SELECT * FROM fnol.reviews WHERE id=%s" in sql:
            return Result(self.reviews.get(params[0]))
        if "SELECT id FROM fnol.records" in sql:
            row = self.records.get(params[0])
            return Result({"id": params[0]} if row else None)
        if "INSERT INTO fnol.records" in sql:
            record_id, case_id, draft, review_id = params
            self.records[record_id] = {
                "id": record_id, "record_id": record_id, "case_id": case_id,
                "draft": draft.obj, "review_id": review_id, "status": "created",
            }
            return Result()
        if "INSERT INTO fnol.idempotency_keys" in sql:
            key, request_hash, response = params
            self.keys[key] = {"request_hash": request_hash, "response": response.obj}
            return Result()
        if "WHERE status='pending'" in sql:
            return Result(many=[row for row in self.reviews.values() if row["status"] == "pending"])
        if "SELECT * FROM fnol.audit ORDER BY" in sql:
            return Result(many=self.audit_rows)
        return Result()


def request(base, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(base + path, data=data, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


class AwsRuntimeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.dataset = {
            "name": "test", "allow_write": True,
            "cases": [{
                "case_id": "A", "intake_source": "A-I", "documents": ["A-I"],
                "logical_key": "loss-a", "tools": {"policy": {"source_id": "A-P"}},
                "faults": {"policy": {"failures": 1}},
            }],
        }
        self.s3 = S3(self.dataset)
        self.database = Database()
        self.db_patch = patch.object(aws_runtime, "connect", return_value=self.database)
        self.db_patch.start()
        self.server = aws_runtime.create_server("db", "bucket", port=0, simulate_faults=True, s3_client=self.s3, dataset_cache_seconds=60)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.db_patch.stop()

    def test_dataset_store_validation_cache_and_helpers(self):
        self.assertEqual(aws_runtime.canonical({"b": 1, "a": 2}), '{"a":2,"b":1}')
        self.assertEqual(aws_runtime.digest({"a": 1}), aws_runtime.digest({"a": 1}))
        with self.assertRaises(ValueError):
            aws_runtime.S3DatasetStore("")
        store = aws_runtime.S3DatasetStore("b", "/prefix/", client=self.s3, cache_seconds=60)
        self.assertIs(store.get(), store.get())
        store.get(force=True)
        self.assertEqual(self.s3.calls[-1]["Key"], "prefix/dataset.json")
        bad = aws_runtime.S3DatasetStore("b", client=S3([]))
        with self.assertRaises(RuntimeError):
            bad.get()
        with patch.object(aws_runtime.boto3, "client", return_value=self.s3):
            self.assertIsNotNone(aws_runtime.S3DatasetStore("b").client)

        connection = MagicMock()
        aws_runtime.audit(connection, "event", "", detail=True)
        self.assertTrue(connection.execute.called)
        self.db_patch.stop()
        with patch.object(aws_runtime.psycopg, "connect", return_value="connected") as connect:
            self.assertEqual(aws_runtime.connect("url"), "connected")
            self.assertEqual(connect.call_args.kwargs["row_factory"], aws_runtime.dict_row)
        self.db_patch.start()

    def test_get_contract_ready_failures_and_faults(self):
        self.assertEqual(request(self.base, "/health")[0], 200)
        self.assertEqual(request(self.base, "/v1/cases?refresh=1")[1]["cases"][0]["case_id"], "A")
        self.assertEqual(request(self.base, "/v1/reviews/missing")[0], 404)
        self.database.reviews["R"] = {"id": "R", "case_id": "A", "draft": {}, "draft_hash": "h", "hash": "h", "status": "pending", "reviewer": None}
        self.assertEqual(request(self.base, "/v1/reviews/R")[0], 200)
        self.assertEqual(request(self.base, "/v1/records/missing")[0], 404)
        self.assertEqual(request(self.base, "/v1/records/A")[0], 404)
        self.database.records["loss-a"] = {"id": "loss-a", "case_id": "A", "review_id": "R", "status": "created"}
        self.assertEqual(request(self.base, "/v1/records/A")[0], 200)
        self.assertEqual(request(self.base, "/not-found")[0], 404)
        self.assertEqual(request(self.base, "/v1/cases/A/missing")[0], 404)
        self.assertEqual(request(self.base, "/v1/cases/A/policy")[0], 503)
        self.assertEqual(request(self.base, "/v1/cases/A/policy")[0], 200)
        self.s3.fail = True
        self.server.RequestHandlerClass.server_version = "test"
        # Force refresh bypasses the populated cache and exposes readiness failure.
        self.assertEqual(request(self.base, "/health?refresh=1")[0], 503)

        healthy_s3 = S3(self.dataset)
        no_fault_server = aws_runtime.create_server(
            "db", "bucket", port=0, simulate_faults=False, s3_client=healthy_s3
        )
        no_fault_thread = threading.Thread(target=no_fault_server.serve_forever, daemon=True)
        no_fault_thread.start()
        try:
            no_fault_base = f"http://127.0.0.1:{no_fault_server.server_port}"
            self.assertEqual(request(no_fault_base, "/v1/cases/A/policy")[0], 200)
        finally:
            no_fault_server.shutdown()
            no_fault_server.server_close()
            no_fault_thread.join(timeout=2)

    def test_post_review_decisions_and_validation(self):
        empty = Request(self.base + "/v1/reviews", data=b"", method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(empty)
        self.assertEqual(caught.exception.code, 413)
        self.assertEqual(request(self.base, "/v1/reviews", {"case_id": "bad"})[0], 400)
        self.assertEqual(request(self.base, "/v1/reviews", {"case_id": "A", "draft": []})[0], 400)
        draft = {"case_id": "A", "value": 1}
        status, created = request(self.base, "/v1/reviews", {"case_id": "A", "draft": draft})
        self.assertEqual(status, 201)
        review_id = created["review_id"]
        path = f"/v1/reviews/{review_id}/decision"
        self.assertEqual(request(self.base, path, {"case_id": "A", "decision": "x", "reviewer": ""})[0], 400)
        self.assertEqual(request(self.base, "/v1/reviews/missing/decision", {"case_id": "A", "decision": "approved", "reviewer": "N"})[0], 404)
        self.dataset["cases"].append({"case_id": "B", "intake_source": "B-I", "documents": ["B-I"], "logical_key": "loss-b", "tools": {}})
        request(self.base, "/health?refresh=1")
        self.assertEqual(request(self.base, path, {"case_id": "B", "decision": "approved", "reviewer": "N"})[0], 409)
        self.assertEqual(request(self.base, path, {"case_id": "A", "decision": "approved", "reviewer": " Name "})[0], 200)
        self.assertEqual(request(self.base, path, {"case_id": "A", "decision": "rejected", "reviewer": "N"})[0], 409)
        self.assertEqual(request(self.base, "/v1/unsupported", {"case_id": "A"})[0], 404)

    def test_record_creation_replay_conflict_and_guards(self):
        draft = {"case_id": "A", "value": 1}
        review_id = "R"
        self.database.reviews[review_id] = {"id": review_id, "case_id": "A", "draft": draft, "draft_hash": aws_runtime.digest(draft), "status": "pending", "reviewer": "N"}
        self.assertEqual(request(self.base, "/v1/records", {"case_id": "A"})[0], 400)
        bad = {"case_id": "A", "draft": {"case_id": "bad"}, "review_id": review_id}
        self.assertEqual(request(self.base, "/v1/records", bad, {"Idempotency-Key": "k"})[0], 400)
        body = {"case_id": "A", "draft": draft, "review_id": review_id}
        self.assertEqual(request(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 403)
        self.database.reviews[review_id]["status"] = "approved"
        self.assertEqual(request(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 201)
        self.assertEqual(request(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 200)
        changed = {"case_id": "A", "draft": {"case_id": "A", "value": 2}, "review_id": review_id}
        self.assertEqual(request(self.base, "/v1/records", changed, {"Idempotency-Key": "k"})[0], 409)
        self.database.keys.clear()
        self.assertEqual(request(self.base, "/v1/records", body, {"Idempotency-Key": "new"})[0], 409)
        self.dataset["allow_write"] = False
        self.s3.dataset = self.dataset
        self.assertEqual(request(self.base, "/v1/records?x=1", body, {"Idempotency-Key": "z"})[0], 404)
        # A separate forced cache refresh activates read-only mode.
        request(self.base, "/health?refresh=1")
        self.assertEqual(request(self.base, "/v1/records", body, {"Idempotency-Key": "z"})[0], 403)

    def test_malformed_post_and_dataset_failure(self):
        req = Request(self.base + "/v1/reviews", data=b"not-json", method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(req)
        self.assertEqual(caught.exception.code, 400)
        req = Request(self.base + "/v1/reviews", data=json.dumps([]).encode(), method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(req)
        self.assertEqual(caught.exception.code, 400)
        self.s3.fail = True
        self.server.RequestHandlerClass.server_version = "test"
        # New server has no cache, so POST reports the dataset dependency failure.
        other = aws_runtime.create_server("db", "bucket", port=0, s3_client=self.s3)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{other.server_port}"
            self.assertEqual(request(base, "/v1/reviews", {"case_id": "A"})[0], 503)
        finally:
            other.shutdown(); other.server_close(); thread.join(timeout=2)

    def test_cli_commands_and_main_guard(self):
        self.database.reviews["R"] = {"id": "R", "case_id": "A", "draft_hash": "h", "status": "pending", "reviewer": None}
        for argv in (["aws_runtime", "pending"], ["aws_runtime", "audit"], ["aws_runtime", "approve", "R", "--reviewer", "N"]):
            if argv[1] == "approve":
                self.database.reviews["R"]["status"] = "pending"
            with patch.object(sys, "argv", argv), patch.object(aws_runtime, "database_url_from_environment", return_value="db"), redirect_stdout(io.StringIO()):
                aws_runtime.main()
        self.database.reviews["R"]["status"] = "decided"
        with patch.object(sys, "argv", ["aws_runtime", "reject", "R", "--reviewer", "N"]), patch.object(aws_runtime, "database_url_from_environment", return_value="db"), self.assertRaises(SystemExit):
            aws_runtime.main()

        fake_server = MagicMock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(sys, "argv", ["aws_runtime", "serve", "--bucket", "b"]), patch.object(aws_runtime, "database_url_from_environment", return_value="db"), patch.object(aws_runtime, "create_server", return_value=fake_server), redirect_stdout(io.StringIO()):
            aws_runtime.main()
        fake_server.server_close.assert_called_once()

        fake_connection = self.database
        with patch.object(sys, "argv", ["aws_runtime", "pending"]), \
             patch("workbench.storage.database_url_from_environment", return_value="db"), \
             patch("psycopg.connect", return_value=fake_connection), redirect_stdout(io.StringIO()):
            runpy.run_module("tools.aws_runtime", run_name="__main__")


if __name__ == "__main__":
    unittest.main()
