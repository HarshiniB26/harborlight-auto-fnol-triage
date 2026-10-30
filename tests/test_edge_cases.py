"""Focused edge-path tests that keep reliability and HTTP behavior honest."""

import http.client
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from tools.runtime import create_server as create_runtime
from workbench import client as client_module
from workbench import server as server_module
from workbench.client import MockClient
from workbench.draft import (
    _add_citation,
    _cite_document,
    _load_policy,
    _section,
    build_draft,
)
from workbench.observability import JsonLogger, Metrics, Observability, _redact
from workbench.reliability import CircuitBreaker, CircuitOpenError, ReliableCaller
from workbench.rules import determine_route, parse_timestamp, reconcile, select_policy
from workbench.service import Workbench, _public_review

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    """Small urlopen response used to exercise client decoding failures."""

    def __init__(self, status, body):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class ClientEdgeTests(unittest.TestCase):
    def setUp(self):
        self.client = MockClient("http://example.invalid")

    def test_http_error_with_invalid_and_empty_json_is_normalized(self):
        invalid = HTTPError(
            "http://example.invalid", 500, "error", {}, io.BytesIO(b"not-json")
        )
        with patch.object(client_module, "urlopen", side_effect=invalid):
            self.assertEqual(self.client.health(), (500, {}))

        empty = HTTPError(
            "http://example.invalid", 404, "missing", {}, io.BytesIO(b"")
        )
        with patch.object(client_module, "urlopen", side_effect=empty):
            self.assertEqual(self.client.health(), (404, {}))

    def test_transport_and_success_decode_failures_return_503(self):
        with patch.object(client_module, "urlopen", side_effect=URLError("down")):
            self.assertEqual(
                self.client.health(), (503, {"error": "transport_unavailable"})
            )
        with patch.object(
            client_module, "urlopen", return_value=FakeResponse(200, b"not-json")
        ):
            self.assertEqual(
                self.client.health(), (503, {"error": "transport_unavailable"})
            )

    def test_invalid_case_list_and_record_responses_raise(self):
        with patch.object(self.client, "_request", return_value=(503, {})):
            with self.assertRaises(RuntimeError):
                self.client.list_cases()
        with patch.object(self.client, "_request", return_value=(200, {"cases": {}})):
            with self.assertRaises(RuntimeError):
                self.client.list_cases()
        with patch.object(self.client, "_request", return_value=(500, {})):
            with self.assertRaises(RuntimeError):
                self.client.get_record("AUTO-X")


class RuleAndDraftEdgeTests(unittest.TestCase):
    def test_invalid_time_conflicts_and_manual_reasons(self):
        with self.assertRaises(ValueError):
            parse_timestamp("not-a-timestamp")
        selected, issue = select_policy([], "not-a-timestamp")
        self.assertIsNone(selected)
        self.assertIn("invalid", issue)

        assertions = [
            {"field": "loss_type", "value": "glass"},
            {"field": "loss_type", "value": "theft"},
        ]
        resolved, conflicts = reconcile(assertions)
        self.assertIsNone(resolved["loss_type"])
        decision = determine_route(
            "unsupported",
            ["driver_name"],
            conflicts,
            [],
            policy_available=False,
            claims_available=True,
        )
        self.assertEqual(decision["routing"], "manual_review")
        self.assertGreaterEqual(len(decision["manual_reasons"]), 4)

    def test_citation_helpers_reject_unsafe_values_and_use_fallback(self):
        self.assertEqual(_section("Header\n\nNext", "Missing"), None)
        self.assertEqual(
            _section("Collision section:\ncontinued text.\n\nNext", "Collision section:"),
            "Collision section:\ncontinued text.",
        )
        citations = []
        sources = {"SRC": "Estimate reference: EST-1\nprivate_token: blocked"}
        _add_citation(citations, "empty", "SRC", None, sources)
        _add_citation(citations, "secret", "SRC", "private_token: blocked", sources)
        self.assertEqual(citations, [])
        _cite_document(
            citations,
            "SRC",
            [],
            sources["SRC"],
            sources,
        )
        self.assertEqual(citations[0]["quote"], "Estimate reference: EST-1")
        self.assertIsNone(_load_policy(ROOT / "data/dev", "../unsafe"))

    def test_draft_handles_missing_policy_files_conflicts_and_non_drivable(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            policies = data / "policies"
            policies.mkdir()
            (policies / "AUTO-TRIAGE-01.md").write_text(
                "Supported losses: collision, theft, glass.\n"
            )
            documents = {
                "DOC-A": (
                    "Driver name: A\nIncident location: One\n"
                    "Incident timestamp: 2026-10-01T12:00:00Z\n"
                    "Loss type: collision\nVehicle drivable: no\n"
                ),
                "DOC-B": "Loss type: theft\n",
            }
            case = {"case_id": "AUTO-X", "documents": ["DOC-A", "DOC-B", "MISSING"]}
            policy = {
                "source_id": "POLICY-X",
                "policy_number": "PX",
                "vehicle_id": "VX",
                "policies": [{
                    "version": "1",
                    "effective_from": "2026-01-01T00:00:00Z",
                    "effective_to": "2027-01-01T00:00:00Z",
                    "wording_source": "MISSING-WORDING",
                    "endorsements": [],
                }],
            }
            claims = {"source_id": "CLAIMS-X", "existing_claims": []}
            draft, _ = build_draft(case, documents, policy, claims, data)
            self.assertEqual(draft["routing"], "manual_review")
            self.assertIn("non-drivable", draft["narrative"])
            self.assertIn("wording or an endorsement", draft["narrative"])
            conflict_citations = [
                row for row in draft["citations"] if row["claim"].startswith("Conflicting")
            ]
            self.assertEqual(len(conflict_citations), 2)

            empty = Path(temporary) / "empty"
            empty.mkdir()
            no_policy, _ = build_draft(
                {"case_id": "AUTO-Y", "documents": []}, {}, None, None, empty
            )
            self.assertIn("triage procedure is unavailable", no_policy["narrative"].lower())


class ObservabilityAndReliabilityEdgeTests(unittest.TestCase):
    def test_nested_lists_and_restricted_strings_are_redacted(self):
        value = [{"password": "x"}, "SYNTHETIC-PRIVATE-ABC", "safe"]
        self.assertEqual(_redact(value), [{"password": "[REDACTED]"}, "[REDACTED]", "safe"])

    def test_half_open_guard_nonretryable_failure_and_empty_attempt_bound(self):
        breaker = CircuitBreaker()
        breaker.state = "half_open"
        with self.assertRaises(CircuitOpenError):
            breaker.before_call()

        observer = Observability(JsonLogger(io.StringIO()), Metrics())
        caller = ReliableCaller(observer, sleep=lambda _: None)
        result = caller.call(
            "claims", lambda *_args, **_kwargs: (400, {}),
            case_id="AUTO-X", trace_id="tr_nonretryable",
        )
        self.assertIsNone(result)

        no_attempts = ReliableCaller(observer, max_attempts=0)
        self.assertIsNone(
            no_attempts.call(
                "claims", lambda *_args, **_kwargs: (200, {}),
                case_id="AUTO-X", trace_id="tr_zero",
            )
        )


class WorkbenchEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock_path = directory / "mock.sqlite"
        self.runtime = create_runtime(ROOT / "data/dev", self.mock_path, port=0)
        self.thread = threading.Thread(
            target=self.runtime.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.log = io.StringIO()
        observer = Observability(JsonLogger(self.log), Metrics())
        self.workbench = Workbench(
            f"http://127.0.0.1:{self.runtime.server_port}",
            ROOT / "data/dev",
            directory / "workbench.json",
            observability=observer,
            reliable_caller=ReliableCaller(observer, sleep=lambda _: None),
        )

    def tearDown(self):
        self.runtime.shutdown()
        self.runtime.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def approve(self, review_id, *, draft=None):
        with sqlite3.connect(self.mock_path) as database:
            if draft is None:
                database.execute(
                    "UPDATE reviews SET status='approved', reviewer='human' WHERE id=?",
                    (review_id,),
                )
            else:
                database.execute(
                    "UPDATE reviews SET status='approved', reviewer='human', draft=? WHERE id=?",
                    (draft, review_id),
                )

    def test_review_lock_existing_record_and_activity_paths(self):
        self.assertEqual(self.workbench.submit_review("AUTO-D01")["error"], "draft_required")
        self.workbench.process("AUTO-D01")
        first = self.workbench.submit_review("AUTO-D01")
        self.assertEqual(self.workbench.submit_review("AUTO-D01")["status"], "pending")
        self.assertEqual(self.workbench.process("AUTO-D01")["error"], "draft_locked")
        self.assertEqual(self.workbench.case_view("AUTO-D01")["status"], "pending")

        self.approve(first["review_id"])
        created = self.workbench.file_record("AUTO-D01")
        self.assertEqual(created["status"], "created")
        view = self.workbench.case_view("AUTO-D01")
        labels = [row["label"] for row in view["activity"]]
        self.assertIn("Human approved exact draft", labels)
        self.assertIn("Record created", labels)
        self.assertEqual(view["status"], "record_created")
        self.assertEqual(self.workbench.submit_review("AUTO-D01")["error"], "existing_record")

    def test_review_and_record_failure_responses(self):
        self.workbench.process("AUTO-D02")
        with patch.object(self.workbench.client, "submit_review", return_value=(500, {})):
            self.assertEqual(self.workbench.submit_review("AUTO-D02")["error"], "review_failed")

        review = self.workbench.submit_review("AUTO-D02")
        self.approve(review["review_id"], draft="{")
        self.assertEqual(self.workbench.file_record("AUTO-D02")["error"], "invalid_review")

        self.workbench.process("AUTO-D03")
        review = self.workbench.submit_review("AUTO-D03")
        self.approve(review["review_id"])
        with patch.object(self.workbench.client, "create_record", return_value=(500, {"error": "down"})):
            self.assertEqual(self.workbench.file_record("AUTO-D03")["error"], "record_failed")

        self.workbench.process("AUTO-D04")
        review = self.workbench.submit_review("AUTO-D04")
        self.approve(review["review_id"])
        with patch.object(
            self.workbench.client,
            "create_record",
            return_value=(409, {"error": "duplicate_case"}),
        ):
            self.assertEqual(self.workbench.file_record("AUTO-D04")["status"], "existing")

    def test_processing_and_state_validation_errors(self):
        with patch.object(self.workbench.client, "list_cases", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                self.workbench.process("AUTO-D01", "tr_failure")
        self.assertIn('"event": "processing_failed"', self.log.getvalue())

        with self.assertRaises(KeyError):
            self.workbench.case_view("AUTO-NOT-FOUND")
        self.assertIsNone(self.workbench._live_review({"review_id": "missing"}))
        with patch.object(self.workbench.client, "get_review", return_value=(503, {})):
            with self.assertRaises(RuntimeError):
                self.workbench._live_review({"review_id": "rev_x"})
        self.workbench.state_path.write_text("[]")
        self.assertEqual(self.workbench._read(), {})
        with self.assertRaises(KeyError):
            self.workbench._check_id("../unsafe")
        self.assertIsNone(_public_review(None))


class FakeWorkbench:
    """Behavior switchboard for browser-server status and error paths."""

    def health(self):
        return {"status": "ok"}

    def readiness(self, _trace_id):
        return {"status": "not_ready"}

    def metrics(self):
        return {"slo": {}}

    def export_results(self):
        return []

    def list_cases(self):
        return []

    def case_view(self, case_id, _trace_id):
        if case_id == "missing":
            raise KeyError(case_id)
        if case_id == "unavailable":
            raise RuntimeError("dependency down")
        return {"case_id": case_id}

    def process(self, case_id, _trace_id):
        if case_id == "missing":
            raise KeyError(case_id)
        if case_id == "unavailable":
            raise RuntimeError("dependency down")
        return {"status": "draft_ready"}

    def submit_review(self, _case_id, _trace_id):
        return {"status": "pending"}

    def file_record(self, _case_id, _trace_id):
        return {"error": "approval_required"}


class ServerEdgeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWorkbench()
        self.server = server_module.create_server(self.fake, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=b""):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        status = response.status
        connection.close()
        return status, payload

    def test_get_static_not_ready_and_error_routes(self):
        self.assertIn("const", urlopen(self.base + "/static/app.js", timeout=3).read().decode())
        self.assertEqual(self.request("GET", "/ready")[0], 503)
        self.assertEqual(self.request("GET", "/api/results"), (200, []))
        self.assertEqual(self.request("GET", "/api/cases"), (200, {"cases": []}))
        self.assertEqual(self.request("GET", "/api/cases/ok")[0], 200)
        self.assertEqual(self.request("GET", "/api/cases/missing")[0], 404)
        self.assertEqual(self.request("GET", "/api/cases/unavailable")[0], 503)
        self.assertEqual(self.request("GET", "/not-found")[0], 404)

        with patch.object(self.fake, "export_results", side_effect=RuntimeError("down")):
            self.assertEqual(self.request("GET", "/api/results")[0], 503)
        with patch.object(self.fake, "list_cases", side_effect=RuntimeError("down")):
            self.assertEqual(self.request("GET", "/api/cases")[0], 503)
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            server_module, "WEB", Path(temporary)
        ):
            self.assertEqual(self.request("GET", "/")[0], 404)

    def test_post_actions_input_limits_and_error_routes(self):
        self.assertEqual(self.request("POST", "/api/cases/ok/process", b"{}")[0], 200)
        self.assertEqual(self.request("POST", "/api/cases/ok/review", b"{}")[0], 200)
        self.assertEqual(self.request("POST", "/api/cases/ok/record", b"{}")[0], 409)
        self.assertEqual(self.request("POST", "/api/cases/missing/process", b"{}")[0], 404)
        self.assertEqual(self.request("POST", "/api/cases/unavailable/process", b"{}")[0], 503)
        self.assertEqual(self.request("POST", "/api/cases/ok/unknown", b"{}")[0], 404)

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/api/cases/ok/process")
        connection.putheader("Content-Length", "invalid")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/api/cases/ok/process")
        connection.putheader("Content-Length", "262145")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()

    def test_server_main_closes_after_keyboard_interrupt(self):
        fake_server = Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        fake_workbench = Mock()
        with patch.object(server_module, "Workbench", return_value=fake_workbench), patch.object(
            server_module, "create_server", return_value=fake_server
        ), patch.object(sys, "argv", ["server", "--port", "9999"]):
            server_module.main()
        fake_server.serve_forever.assert_called_once()
        fake_server.server_close.assert_called_once()
        fake_workbench.observability.event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
