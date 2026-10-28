"""Edge contracts for the browser server and SQS intake adapter."""

import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workbench import server as server_module
from workbench import draft as draft_module
from workbench.dataset_diff_poller import (
    DatasetDiffPoller,
    dataset_keys_from_sqs_body,
    is_dataset_json_key,
)
from workbench.observability import Observability
from workbench.service import Workbench, digest
from workbench.storage import S3EvidenceStore


class Observer:
    def __init__(self):
        self.rows = []

    def event(self, name, **detail):
        self.rows.append((name, detail))


class Sqs:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.deleted = []

    def receive_message(self, **_):
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


class MinimalWorkbench:
    def __init__(self):
        self.observability = Observer()
        self.client = MagicMock()
        self.client.list_cases.return_value = [{"case_id": "A"}]
        self.diff = {"ack": True}

    def apply_dataset_diff(self, *_args, **_kwargs):
        return self.diff


class PollerCoverageTests(unittest.TestCase):
    def test_event_parsing_rejects_malformed_shapes_and_decodes_sns(self):
        self.assertEqual(dataset_keys_from_sqs_body("bad"), [])
        self.assertEqual(dataset_keys_from_sqs_body([]), [])
        self.assertEqual(dataset_keys_from_sqs_body({"Message": "bad"}), [])
        self.assertEqual(dataset_keys_from_sqs_body({"Records": {}}), [])
        wrapped = {
            "Message": json.dumps({
                "Records": [None, {"s3": {}}, {"s3": {"object": {"key": 3}}},
                            {"s3": {"object": {"key": "/folder%2Fdataset.json"}}}]
            })
        }
        self.assertEqual(dataset_keys_from_sqs_body(json.dumps(wrapped)), ["folder/dataset.json"])
        self.assertFalse(is_dataset_json_key(None))
        self.assertTrue(is_dataset_json_key("folder/dataset.json", prefix="/folder/"))
        self.assertFalse(is_dataset_json_key("folder/document.md", prefix="folder"))

    def test_constructor_lifecycle_polling_and_default_client(self):
        workbench = MinimalWorkbench()
        with self.assertRaises(ValueError):
            DatasetDiffPoller(workbench, "")
        with patch("boto3.client", return_value=Sqs()) as client:
            poller = DatasetDiffPoller(workbench, "queue")
        client.assert_called_once_with("sqs")

        poller.poll_once = lambda: poller.stop_event.set()
        poller.run_forever()
        self.assertTrue(poller.stop_event.is_set())

        poller = DatasetDiffPoller(workbench, "queue", sqs_client=Sqs())
        thread = poller.start()
        second = poller.start()
        self.assertIs(thread, second)
        poller.stop()
        thread.join(timeout=2)
        self.assertIn("dataset_diff_poller_started", [row[0] for row in workbench.observability.rows])

    def test_run_forever_backs_off_and_poll_once_handles_empty_and_message(self):
        workbench = MinimalWorkbench()

        class Stop:
            def __init__(self):
                self.done = False
                self.delay = None

            def is_set(self):
                return self.done

            def set(self):
                self.done = True

            def wait(self, delay):
                self.delay = delay
                self.done = True

        stop = Stop()
        poller = DatasetDiffPoller(workbench, "queue", sqs_client=Sqs(), stop_event=stop)
        poller.poll_once = MagicMock(side_effect=RuntimeError("aws"))
        poller.run_forever()
        self.assertEqual(stop.delay, 2)
        self.assertEqual(workbench.observability.rows[-1][0], "dataset_diff_poller_error")

        empty = DatasetDiffPoller(workbench, "queue", sqs_client=Sqs())
        self.assertIsNone(empty.poll_once())
        message = {"Body": json.dumps({"Records": []})}
        populated = DatasetDiffPoller(workbench, "queue", sqs_client=Sqs([message]))
        self.assertEqual(populated.poll_once()["status"], "ignored")

    def test_message_acknowledgement_ignore_failure_and_no_receipt_paths(self):
        workbench = MinimalWorkbench()
        sqs = Sqs()
        poller = DatasetDiffPoller(workbench, "queue", sqs_client=sqs, evidence_prefix="folder")
        ignored = poller.handle_message({
            "ReceiptHandle": "ignored",
            "Body": json.dumps({"Records": [{"s3": {"object": {"key": "other"}}}]})
        })
        self.assertTrue(ignored["ack"])
        self.assertEqual(sqs.deleted, ["ignored"])
        poller.handle_message({"Body": "bad"})

        workbench.client.list_cases.side_effect = RuntimeError("down")
        failed = poller.handle_message({
            "ReceiptHandle": "keep",
            "Body": json.dumps({"Records": [{"s3": {"object": {"key": "folder/dataset.json"}}}]})
        })
        self.assertFalse(failed["ack"])
        self.assertNotIn("keep", sqs.deleted)

        workbench.client.list_cases.side_effect = None
        workbench.client.list_cases.return_value = [{"case_id": "A"}]
        workbench.diff = {"ack": False}
        result = poller.handle_message({
            "Body": json.dumps({"Records": [{"s3": {"object": {"key": "folder/dataset.json"}}}]})
        })
        self.assertFalse(result["ack"])


class StubWorkbench:
    def __init__(self):
        self.observability = Observer()

    def health(self): return {"status": "ok"}
    def readiness(self, _): return {"status": "ready"}
    def metrics(self): return {"ok": True}
    def export_results(self): return []
    def list_cases(self): return []
    def case_view(self, case_id, _): return {"case_id": case_id}
    def prepare_queue(self, **_): return {"status": "completed"}
    def process(self, case_id, _): return {"case_id": case_id}
    def submit_review(self, case_id, _): return {"case_id": case_id}
    def decide_review(self, case_id, *_): return {"case_id": case_id}
    def file_record(self, case_id, _): return {"case_id": case_id}


class ServerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.workbench = StubWorkbench()
        self.server = server_module.create_server(self.workbench, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, raw=None, headers=None):
        request = Request(self.base + path, data=raw, method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def test_static_health_ready_metrics_and_not_found_routes(self):
        self.assertEqual(self.request("GET", "/static/styles.css")[0], 200)
        self.assertEqual(self.request("GET", "/static/nope.js")[0], 404)
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/metrics")[0], 200)
        self.workbench.readiness = lambda _: {"status": "not_ready"}
        self.assertEqual(self.request("GET", "/ready")[0], 503)
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        self.assertEqual(self.request("GET", "/api/cases/A/B")[0], 404)

    def test_get_api_errors_and_conflict_results(self):
        self.workbench.export_results = lambda: {"error": "incomplete"}
        self.assertEqual(self.request("GET", "/api/results")[0], 409)
        self.workbench.export_results = MagicMock(side_effect=RuntimeError("runtime"))
        self.assertEqual(self.request("GET", "/api/results")[0], 503)
        self.workbench.list_cases = MagicMock(side_effect=RuntimeError("runtime"))
        self.assertEqual(self.request("GET", "/api/cases")[0], 503)
        self.workbench.case_view = MagicMock(side_effect=KeyError("x"))
        self.assertEqual(self.request("GET", "/api/cases/X")[0], 404)
        self.workbench.case_view = MagicMock(side_effect=RuntimeError("runtime"))
        self.assertEqual(self.request("GET", "/api/cases/X")[0], 503)

    def test_post_validation_dispatch_and_error_paths(self):
        self.assertEqual(self.request("POST", "/api/queue/prepare", b"{}", {"Content-Length": "bad"})[0], 400)
        self.assertEqual(self.request("POST", "/api/queue/prepare", b"{}", {"Content-Length": "262145"})[0], 413)
        self.assertEqual(self.request("POST", "/api/queue/prepare", b"[1]")[0], 400)
        self.assertEqual(self.request("POST", "/api/unknown", b"")[0], 404)

        self.workbench.prepare_queue = lambda **_: {"error": "bad"}
        self.assertEqual(self.request("POST", "/api/queue/prepare", b"{}")[0], 409)
        self.workbench.prepare_queue = MagicMock(side_effect=RuntimeError("runtime"))
        self.assertEqual(self.request("POST", "/api/queue/prepare", b"{}")[0], 503)

        for action in ("process", "review", "decision", "record"):
            method = {
                "process": "process", "review": "submit_review",
                "decision": "decide_review", "record": "file_record",
            }[action]
            setattr(self.workbench, method, MagicMock(return_value={"error": "blocked"}))
            self.assertEqual(self.request("POST", f"/api/cases/A/{action}", b"{}")[0], 409)

        self.workbench.process = MagicMock(side_effect=KeyError("A"))
        self.assertEqual(self.request("POST", "/api/cases/A/process", b"{}")[0], 404)
        self.workbench.process = MagicMock(side_effect=RuntimeError("runtime"))
        self.assertEqual(self.request("POST", "/api/cases/A/process", b"{}")[0], 503)

    def test_missing_static_file_helper(self):
        code, body, _ = self.request("GET", "/static/../index.html")
        self.assertEqual(code, 404)
        self.assertIn(b"not_found", body)

    def test_main_local_aws_and_poller_shutdown_paths(self):
        fake_server = MagicMock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        fake_poller = MagicMock()
        with (
            patch.object(sys, "argv", ["server", "--port", "0"]),
            patch.object(server_module, "Workbench"),
            patch.object(server_module, "create_server", return_value=fake_server),
            patch.dict("os.environ", {}, clear=True),
        ):
            server_module.main()
        fake_server.server_close.assert_called_once()

        fake_server.reset_mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with (
            patch.object(sys, "argv", ["server", "--storage", "aws", "--database-url", "db", "--s3-bucket", "bucket"]),
            patch.object(server_module, "PostgresStateStore", return_value="state"),
            patch.object(server_module, "S3EvidenceStore", return_value="evidence"),
            patch.object(server_module, "Workbench"),
            patch.object(server_module, "create_server", return_value=fake_server),
            patch.object(server_module, "DatasetDiffPoller", return_value=fake_poller),
            patch.dict("os.environ", {"DATASET_DIFF_QUEUE_URL": "queue"}, clear=True),
        ):
            server_module.main()
        fake_poller.start.assert_called_once()
        fake_poller.stop.assert_called_once()


class StateStore:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def read(self):
        return json.loads(json.dumps(self.rows))

    def write(self, values):
        self.rows.update(json.loads(json.dumps(values)))


class Evidence:
    def document(self, source_id):
        return None if source_id == "MISSING" else f"document {source_id}"

    def policy(self, _source_id):
        return None


class ServiceCoverageTests(unittest.TestCase):
    def workbench(self, rows=None, queue_cursor=False):
        workbench = Workbench(
            "http://unit",
            ".",
            "state.json",
            state_store=StateStore(rows),
            evidence_store=Evidence(),
            queue_cursor=queue_cursor,
            observability=Observability(),
        )
        workbench.client = MagicMock()
        workbench.client.list_cases.return_value = [{"case_id": "A", "documents": []}]
        workbench.client.get_record.return_value = None
        return workbench

    def test_export_prepare_and_diff_validation_paths(self):
        draft = {"case_id": "A"}
        workbench = self.workbench({"A": {"draft": draft}})
        self.assertEqual(workbench.export_results(), [draft])

        workbench.process = MagicMock(return_value={"draft": draft})
        workbench.submit_review = MagicMock(return_value={"status": "pending"})
        result = workbench.prepare_queue()
        self.assertEqual(result["handled_case_ids"], ["A"])
        self.assertEqual(workbench.prepare_queue(case_ids="A")["error"], "invalid_case_ids")
        self.assertEqual(workbench.prepare_queue(case_ids=[1])["error"], "invalid_case_ids")

        workbench.submit_review.return_value = {"error": "review_failed"}
        self.assertEqual(workbench.prepare_queue(case_ids=["A"])["failed_case_ids"], ["A"])

        self.assertEqual(workbench.apply_dataset_diff(["A"])["error"], "cursor_unavailable")
        cursor = self.workbench(queue_cursor=True)
        self.assertEqual(cursor.apply_dataset_diff([1])["error"], "invalid_current_ids")
        cursor.state_store.rows["fnol-queue-meta"] = {"last_seen_case_ids": ["A", 3]}
        no_change = cursor.apply_dataset_diff(("A",))
        self.assertTrue(no_change["ack"])
        self.assertEqual(no_change["added_case_ids"], [])

    def test_process_slow_completion_and_exception_metric(self):
        ticks = iter((0.0, 16.0))
        workbench = self.workbench()
        workbench.monotonic = lambda: next(ticks)
        workbench.client.get_policy.return_value = (200, {
            "source_id": "POL", "policy_number": "P", "vehicle_id": "V", "policies": []
        })
        workbench.client.get_claims.return_value = (200, {
            "source_id": "CLAIMS", "existing_claims": []
        })
        result = workbench.process("A")
        self.assertEqual(result["status"], "draft_ready")
        self.assertNotIn("fnol_processing_within_15s_total", workbench.metrics()["counters"])

        broken = self.workbench()
        broken.client.list_cases.side_effect = RuntimeError("down")
        with self.assertRaises(RuntimeError):
            broken.process("A")
        self.assertEqual(broken.metrics()["counters"]["fnol_processing_error_total"], 1)

    def test_review_decision_validation_and_failure_paths(self):
        draft = {"case_id": "A"}
        review = {"id": "R", "status": "pending", "hash": digest(draft)}
        workbench = self.workbench({"A": {"draft": draft, "review_id": "R"}})
        workbench.client.get_review.return_value = (200, review)

        self.assertEqual(workbench.decide_review("A", "Name", "maybe")["error"], "invalid_decision")
        for reviewer in (None, "", "x" * 81, "bad\nname"):
            self.assertEqual(workbench.decide_review("A", reviewer, "approved")["error"], "reviewer_required")

        no_review = self.workbench()
        self.assertEqual(no_review.decide_review("A", "Name", "approved")["error"], "review_required")
        workbench.client.get_review.return_value = (200, {**review, "status": "rejected"})
        self.assertEqual(workbench.decide_review("A", "Name", "approved")["error"], "review_already_decided")
        workbench.client.get_review.return_value = (200, {**review, "hash": "wrong"})
        self.assertEqual(workbench.decide_review("A", "Name", "approved")["error"], "draft_changed")

        workbench.client.get_review.return_value = (200, review)
        workbench.client.decide_review.return_value = (503, {"error": "down"})
        self.assertEqual(workbench.decide_review("A", "Name", "approved")["error"], "down")
        workbench.client.decide_review.return_value = (503, "bad")
        self.assertEqual(workbench.decide_review("A", "Name", "approved")["error"], "review_decision_failed")

        workbench.client.decide_review.return_value = (200, {"review_id": "R", "status": "rejected"})
        self.assertEqual(workbench.decide_review("A", "Name", "rejected")["status"], "rejected")
        workbench.client.decide_review.return_value = (200, {"review_id": "R", "status": "approved"})
        workbench.file_record = MagicMock(side_effect=RuntimeError("later"))
        approved = workbench.decide_review("A", "Name", "approved")
        self.assertEqual(approved["record_creation_status"], "pending_retry")

    def test_service_helpers_cover_empty_and_present_lifecycle_states(self):
        workbench = self.workbench({"A": {"draft": {"case_id": "A"}}})
        self.assertEqual(workbench._status("A", workbench._read()), "draft_ready")
        self.assertEqual(workbench._documents(["MISSING", "FOUND"]), {"FOUND": "document FOUND"})
        self.assertEqual(workbench._activity({}, None, None), [])
        self.assertEqual(workbench._read_last_seen(), set())
        workbench._merge_last_seen(["A"])
        workbench._merge_last_seen([])

    def test_final_draft_storage_server_and_record_branches(self):
        self.assertEqual(draft_module._section("Collision section:\nNext section:", "Collision section:"), "Collision section:")
        self.assertEqual(draft_module._section("Collision section:", "Collision section:"), "Collision section:")
        citations = []
        draft_module._add_citation(citations, "claim", "S", "quote", {"S": "quote"})
        draft_module._add_citation(citations, "claim", "S", "quote", {"S": "quote"})
        draft_module._add_citation(citations, "claim", "S", "missing", {"S": "quote"})
        self.assertEqual(len(citations), 1)

        policy = {
            "source_id": "POL", "policies": [{
                "version": "1", "effective_from": "2020-01-01T00:00:00Z",
                "effective_to": "2030-01-01T00:00:00Z",
                "wording_source": "AUTO-TRIAGE-01", "endorsements": [],
            }],
        }
        documents = {"I": "Incident timestamp: 2026-01-01T00:00:00Z\nLoss type: collision"}
        result, _ = draft_module.build_draft(
            {"case_id": "A", "documents": ["I"]}, documents, policy,
            {"source_id": "C", "existing_claims": []},
            policy_loader=lambda _source_id: "Supported losses: collision",
        )
        self.assertIn("AUTO-TRIAGE-01", result["policy_ids"])

        with patch("boto3.client", return_value=MagicMock()) as client:
            self.assertIsNotNone(S3EvidenceStore("bucket").client)
            client.assert_called_once_with("s3")

        minimal = MinimalWorkbench()
        server = server_module.create_server(minimal, port=0)
        try:
            handler = object.__new__(server.RequestHandlerClass)
            handler.send_response = MagicMock()
            handler.send_header = MagicMock()
            handler.end_headers = MagicMock()
            handler.wfile = io.BytesIO()
            with tempfile.TemporaryDirectory() as temporary:
                page = Path(temporary) / "page.txt"
                page.write_text("page")
                handler._file(page, "text/plain")
            handler._json(200, {"ok": True})
            self.assertFalse(any(call.args[0] == "X-Trace-ID" for call in handler.send_header.call_args_list))
        finally:
            server.server_close()

        state = StateStore({"A": {"draft": {"case_id": "A"}, "review_id": "R"}})
        workbench = Workbench("http://unit", ".", "state", state_store=state, evidence_store=Evidence(), observability=Observability())
        workbench.client = MagicMock()
        workbench.client.list_cases.return_value = [{"case_id": "A"}]
        workbench.client.get_record.return_value = None
        review = {"id": "R", "status": "approved", "draft": {"case_id": "A"}, "hash": digest({"case_id": "A"})}
        workbench.client.get_review.return_value = (200, review)
        def remove_state(*_args):
            state.rows.clear()
            return 201, {"record_id": "record"}
        workbench.client.create_record.side_effect = remove_state
        self.assertEqual(workbench.file_record("A")["status"], "created")

        # Execute the real server module main guard without starting a listener.
        path = Path(server_module.__file__)
        lines = path.read_text().splitlines()
        guard = "\n" * (len(lines) - 2) + "\n".join(lines[-2:]) + "\n"
        main = MagicMock()
        exec(compile(guard, str(path), "exec"), {"__name__": "__main__", "main": main})
        main.assert_called_once()


if __name__ == "__main__":
    unittest.main()
