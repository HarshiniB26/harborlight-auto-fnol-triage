"""Workbench against the real local mock. The app code under test never opens dataset.json."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tools.evaluate import evaluate
from tools.runtime import create_server
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

    def test_approved_draft_files_once_and_duplicate_intake_reuses_it(self):
        for case_id in ("AUTO-D09", "AUTO-D10"):
            self.workbench.process(case_id)
            review = self.workbench.submit_review(case_id)
            self._approve(review["review_id"])
        created = self.workbench.file_record("AUTO-D09")
        self.assertEqual(created["status"], "created")
        duplicate = self.workbench.file_record("AUTO-D10")
        self.assertEqual(duplicate["status"], "existing")
        self.assertEqual(duplicate["record"]["record_id"], created["record"]["record_id"])
        with sqlite3.connect(self.mock_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)

    def _approve(self, review_id):
        """The test acts as the human operator. The workbench has no approve method."""
        with sqlite3.connect(self.mock_path) as db:
            db.execute(
                "UPDATE reviews SET status='approved', reviewer='reviewer-name' WHERE id=? AND status='pending'",
                (review_id,),
            )


if __name__ == "__main__":
    unittest.main()
