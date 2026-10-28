"""Workbench against the real local mock. The app code under test never opens dataset.json."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tools.evaluate import evaluate
from tools.runtime import create_server, _file_approved_review
from workbench.service import Workbench

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock_path = directory / "mock.sqlite"
        self.server = create_server(ROOT / "data/dev", self.mock_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.workbench = Workbench(
            api_url=f"http://127.0.0.1:{self.server.server_port}",
            data_dir=ROOT / "data/dev",
            state_path=directory / "workbench.json",
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_development_cases_match_the_output_contract(self):
        cases = self.workbench.list_cases()
        drafts = [self.workbench.process(case["case_id"])["draft"] for case in cases]
        dataset = json.loads((ROOT / "data/dev/dataset.json").read_text())
        gold = json.loads((ROOT / "feedback/dev_gold.json").read_text())
        report = evaluate(dataset, gold, drafts)
        self.assertTrue(report["passed"], json.dumps(report, indent=2))
        exported = self.workbench.export_results()
        self.assertIsInstance(exported, list)
        self.assertEqual(exported, drafts)
        blob = json.dumps(drafts)
        self.assertNotIn("SYNTHETIC-PRIVATE-", blob)
        self.assertNotIn("private_token", blob)
        d08 = next(item for item in drafts if item["case_id"] == "AUTO-D08")
        self.assertNotIn("pay 8400", d08["narrative"].lower())
        self.assertNotIn("claim is covered", d08["narrative"].lower())
        d05 = next(item for item in drafts if item["case_id"] == "AUTO-D05")
        self.assertIn("policy selection is unresolved", d05["narrative"].lower())
        self.assertNotIn("cited coverage wording", d05["narrative"].lower())
        policy_citation = next(
            citation
            for citation in d05["citations"]
            if citation["source_id"] == "AUTO-D05-POLICY"
        )
        self.assertIn("multiple policy intervals", policy_citation["claim"].lower())

    def test_unapproved_draft_is_not_sent_to_records(self):
        self.workbench.process("AUTO-D01")
        review = self.workbench.submit_review("AUTO-D01")
        self.assertEqual(review["status"], "pending")
        blocked = self.workbench.file_record("AUTO-D01")
        self.assertEqual(blocked["error"], "approval_required")
        with sqlite3.connect(self.mock_path) as db:
            attempted = db.execute(
                "SELECT COUNT(*) FROM audit WHERE event='unapproved_write_attempt'"
            ).fetchone()[0]
            records = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.assertEqual(attempted, 0)
        self.assertEqual(records, 0)

    def test_approval_creates_record_and_reads_are_safe(self):
        self.workbench.process("AUTO-D01")
        review = self.workbench.submit_review("AUTO-D01")
        self._approve(review["review_id"])

        for _ in range(3):
            self.workbench.list_cases()
            detail = self.workbench.case_view("AUTO-D01")
            self.assertEqual(detail["status"], "record_created")
            self.assertIsNotNone(detail["record"])

        with sqlite3.connect(self.mock_path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_duplicate_loss_approval_creates_one_record(self):
        self.workbench.process("AUTO-D09")
        review = self.workbench.submit_review("AUTO-D09")
        self._approve(review["review_id"])

        self.workbench.process("AUTO-D10")

        first = self.workbench.case_view("AUTO-D09")["record"]
        second = self.workbench.case_view("AUTO-D10")["record"]

        self.assertEqual(first["record_id"], second["record_id"])

        with sqlite3.connect(self.mock_path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_record_request_after_approval_does_not_duplicate(self):
        self.workbench.process("AUTO-D02")
        review = self.workbench.submit_review("AUTO-D02")
        self._approve(review["review_id"])

        result = self.workbench.file_record("AUTO-D02")
        self.assertEqual(result["status"], "existing")

        with sqlite3.connect(self.mock_path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def _approve(self, review_id):
        """Simulate the runtime operator's approval and filing."""
        with sqlite3.connect(self.mock_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM reviews WHERE id=?",
                (review_id,),
            ).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")

            db.execute(
                "UPDATE reviews SET status='approved', reviewer=? WHERE id=?",
                ("reviewer-name", review_id),
            )

            record_id = _file_approved_review(
                db, row, "reviewer-name"
            )
            self.assertIsNotNone(record_id)


if __name__ == "__main__":
    unittest.main()
