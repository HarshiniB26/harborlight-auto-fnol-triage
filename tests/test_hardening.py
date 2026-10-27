"""Focused Week 10 reliability, security, and observability tests."""

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tools.runtime import create_server
from workbench.observability import JsonLogger, Metrics, Observability
from workbench.reliability import ReliableCaller
from workbench.service import Workbench

ROOT = Path(__file__).resolve().parents[1]


class MutableClock:
    """Deterministic monotonic clock used to test waiting without real delays."""

    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.clock = MutableClock()
        observer = Observability(JsonLogger(self.output), Metrics())
        self.caller = ReliableCaller(
            observer,
            base_delay=0.1,
            jitter_ratio=0.2,
            failure_threshold=2,
            reset_timeout=10,
            sleep=self.clock.sleep,
            random_value=lambda: 0.5,
            clock=self.clock,
        )

    def test_transient_failure_recovers_after_exponential_backoff(self):
        attempts = []

        def operation(_case_id, trace_id=None):
            attempts.append(trace_id)
            return (503, {"error": "timeout"}) if len(attempts) == 1 else (200, {"ok": True})

        result = self.caller.call(
            "claims", operation, case_id="AUTO-D11", trace_id="tr_transient"
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(attempts), 2)
        self.assertAlmostEqual(self.clock.value, 0.11)
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        retry = next(row for row in events if row["event"] == "tool_call_retry_scheduled")
        self.assertEqual(retry["backoff_ms"], 110.0)

    def test_persistent_failure_opens_then_recovers_half_open_circuit(self):
        calls = []

        def failing(_case_id, trace_id=None):
            calls.append(trace_id)
            return 503, {"error": "timeout"}

        for trace_id in ("tr_first", "tr_second"):
            self.assertIsNone(
                self.caller.call("claims", failing, case_id="AUTO-D07", trace_id=trace_id)
            )
        self.assertEqual(len(calls), 6)
        self.assertEqual(self.caller.health()["claims"]["state"], "open")

        self.assertIsNone(
            self.caller.call("claims", failing, case_id="AUTO-D07", trace_id="tr_fast")
        )
        self.assertEqual(len(calls), 6, "an open circuit must not call the dependency")

        self.clock.value += 10
        recovered = self.caller.call(
            "claims",
            lambda _case_id, trace_id=None: (200, {"ok": True}),
            case_id="AUTO-D07",
            trace_id="tr_recovery",
        )
        self.assertEqual(recovered, {"ok": True})
        self.assertEqual(self.caller.health()["claims"]["state"], "closed")


class HardenedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock_path = directory / "mock.sqlite"
        self.server = create_server(ROOT / "data/dev", self.mock_path, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.output = io.StringIO()
        self.observer = Observability(JsonLogger(self.output), Metrics())
        caller = ReliableCaller(
            self.observer,
            base_delay=0.1,
            jitter_ratio=0,
            failure_threshold=2,
            sleep=lambda _seconds: None,
        )
        self.workbench = Workbench(
            api_url=f"http://127.0.0.1:{self.server.server_port}",
            data_dir=ROOT / "data/dev",
            state_path=directory / "workbench.json",
            observability=self.observer,
            reliable_caller=caller,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_real_transient_fault_recovers_on_second_attempt(self):
        result = self.workbench.process("AUTO-D11", "tr_real_transient")
        self.assertEqual(result["draft"]["routing"], "specialist")
        with sqlite3.connect(self.mock_path) as db:
            attempts = db.execute(
                "SELECT n FROM calls WHERE case_id='AUTO-D11' AND tool='claims'"
            ).fetchone()[0]
        self.assertEqual(attempts, 2)

    def test_persistent_fault_falls_back_then_circuit_fails_fast(self):
        first = self.workbench.process("AUTO-D07", "tr_outage_one")
        second = self.workbench.process("AUTO-D07", "tr_outage_two")
        third = self.workbench.process("AUTO-D07", "tr_outage_three")
        self.assertEqual(first["draft"]["routing"], "manual_review")
        self.assertEqual(second["draft"]["routing"], "manual_review")
        self.assertEqual(third["draft"]["routing"], "manual_review")
        with sqlite3.connect(self.mock_path) as db:
            attempts = db.execute(
                "SELECT n FROM calls WHERE case_id='AUTO-D07' AND tool='claims'"
            ).fetchone()[0]
        self.assertEqual(attempts, 6, "the third request must fail fast at the open circuit")

    def test_injected_instruction_is_ignored_and_logs_are_redacted(self):
        result = self.workbench.process("AUTO-D08", "tr_attack")
        narrative = result["draft"]["narrative"].lower()
        self.assertIn("instruction-like text", result["warnings"][0].lower())
        self.assertNotIn("pay 8400", narrative)
        self.assertNotIn("claim is covered", narrative)
        self.assertNotIn("private_token", self.output.getvalue())
        self.assertNotIn("SYNTHETIC-PRIVATE-", self.output.getvalue())
        attack_events = [
            json.loads(line)
            for line in self.output.getvalue().splitlines()
            if json.loads(line).get("event")
            == "untrusted_document_instruction_ignored"
        ]
        self.assertEqual(len(attack_events), 1)
        self.assertEqual(attack_events[0]["level"], "WARNING")
        self.assertEqual(attack_events[0]["warning_count"], 1)
        trace_ids = {
            json.loads(line).get("trace_id")
            for line in self.output.getvalue().splitlines()
            if json.loads(line).get("case_id") == "AUTO-D08"
        }
        self.assertEqual(trace_ids, {"tr_attack"})

    def test_changed_approved_draft_is_blocked_by_application(self):
        self.assertFalse(hasattr(self.workbench, "approve"))
        self.workbench.process("AUTO-D01")
        review = self.workbench.submit_review("AUTO-D01")
        with sqlite3.connect(self.mock_path) as db:
            db.execute(
                "UPDATE reviews SET status='approved', reviewer='human-reviewer' WHERE id=?",
                (review["review_id"],),
            )
        state = self.workbench._read()
        state["AUTO-D01"]["draft"]["routing"] = "urgent"
        self.workbench._write(state)
        result = self.workbench.file_record("AUTO-D01")
        self.assertEqual(result["error"], "draft_changed")
        with sqlite3.connect(self.mock_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)

    def test_metrics_are_tied_to_processing_slo(self):
        self.workbench.process("AUTO-D01", "tr_metrics")
        snapshot = self.workbench.metrics()
        self.assertEqual(snapshot["counters"]["fnol_processing_total"], 1)
        self.assertEqual(snapshot["counters"]["fnol_processing_completed_total"], 1)
        self.assertEqual(snapshot["slo"]["completion_rate"], 1.0)
        self.assertEqual(snapshot["slo"]["within_15s_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
