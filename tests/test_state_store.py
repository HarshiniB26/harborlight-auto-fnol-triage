"""Workflow storage: SQLite CRUD, approval races, idempotency, and rollback."""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tools.runtime import digest
from tools.state_store import SqliteStore, canonical


def exercise_store(test, store):
    """Run the mutable-workflow contract against either database."""

    draft = {"case_id": "AUTO-D01", "classification": "collision"}
    draft_hash = digest(draft)
    store.insert_review("rev1", "AUTO-D01", canonical(draft), draft_hash)
    pending = store.get_review("rev1")
    test.assertEqual(pending["status"], "pending")
    test.assertIsNone(pending["reviewer"])

    blocked = store.commit_record(
        case_id="AUTO-D01",
        draft_canonical=canonical(draft),
        draft_hash=draft_hash,
        review_id="rev1",
        key="key-1",
        fingerprint=digest({"case_id": "AUTO-D01", "draft": draft, "review_id": "rev1"}),
        logical_key="loss-1",
        dataset_version="dataset-v1",
    )
    test.assertEqual(blocked[0], 403)
    test.assertIsNone(store.get_record("loss-1"))

    approved = store.transition_review("rev1", "approved", "ada")
    test.assertEqual(approved["reviewer"], "ada")
    test.assertIsNone(store.transition_review("rev1", "approved", "someone-else"))
    test.assertEqual(store.get_review("rev1")["reviewer"], "ada")

    fingerprint = digest({"case_id": "AUTO-D01", "draft": draft, "review_id": "rev1"})
    created = store.commit_record(
        case_id="AUTO-D01",
        draft_canonical=canonical(draft),
        draft_hash=draft_hash,
        review_id="rev1",
        key="key-1",
        fingerprint=fingerprint,
        logical_key="loss-1",
        dataset_version="dataset-v1",
    )
    test.assertEqual(created, (201, {
        "record_id": "loss-1",
        "case_id": "AUTO-D01",
        "status": "created",
        "review_id": "rev1",
    }))
    record = store.get_record("loss-1")
    test.assertEqual(set(record), {"id", "case_id", "draft", "review_id"})
    replay = store.commit_record(
        case_id="AUTO-D01",
        draft_canonical=canonical(draft),
        draft_hash=draft_hash,
        review_id="rev1",
        key="key-1",
        fingerprint=fingerprint,
        logical_key="loss-1",
        dataset_version="dataset-v1",
    )
    test.assertEqual(replay[0], 200)
    test.assertEqual(replay[1]["record_id"], "loss-1")
    conflict = store.commit_record(
        case_id="AUTO-D01",
        draft_canonical=canonical({"case_id": "AUTO-D01", "classification": "theft"}),
        draft_hash="different",
        review_id="rev1",
        key="key-1",
        fingerprint="different-fingerprint",
        logical_key="loss-1",
        dataset_version="dataset-v1",
    )
    test.assertEqual(conflict[0], 409)
    test.assertEqual(conflict[1]["error"], "idempotency_conflict")
    duplicate = store.commit_record(
        case_id="AUTO-D01",
        draft_canonical=canonical(draft),
        draft_hash=draft_hash,
        review_id="rev1",
        key="key-2",
        fingerprint=digest({"case_id": "AUTO-D01", "draft": draft, "review_id": "rev1", "other": True}),
        logical_key="loss-1",
        dataset_version="dataset-v1",
    )
    test.assertEqual(duplicate[1]["error"], "duplicate_case")
    created_events = [
        event for event in store.list_audit()
        if event["event"] == "record_created"
    ]
    test.assertEqual(len(created_events), 1)
    test.assertIn("dataset-v1", created_events[0]["detail"])
    count, failed = store.record_tool_attempt("AUTO-D01", "claims", {"failures": 1})
    test.assertEqual(count, 1)
    test.assertTrue(failed)
    count, failed = store.record_tool_attempt("AUTO-D01", "claims", {"failures": 1})
    test.assertEqual(count, 2)
    test.assertFalse(failed)


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SqliteStore(Path(self.temporary.name) / "mock.sqlite")

    def tearDown(self):
        self.temporary.cleanup()

    def test_review_record_idempotency_and_audit(self):
        exercise_store(self, self.store)

    def test_transaction_rolls_back_when_a_later_insert_fails(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as tx:
                tx.execute(
                    "INSERT INTO keys(key, hash, response) VALUES(?,?,?)",
                    ("key-rollback", "hash", "{}"),
                )
                tx.execute(
                    "INSERT INTO records(id, case_id, draft, review_id, dataset_version) VALUES(?,?,?,?,?)",
                    ("loss-rollback", "AUTO-D01", "{}", "rev", "v1"),
                )
                tx.execute(
                    "INSERT INTO records(id, case_id, draft, review_id, dataset_version) VALUES(?,?,?,?,?)",
                    ("loss-rollback", "AUTO-D02", "{}", "rev", "v1"),
                )
        self.assertIsNone(self.store.get_record("loss-rollback"))
        with self.store.transaction() as tx:
            self.assertIsNone(tx.fetchone("SELECT key FROM keys WHERE key=?", ("key-rollback",)))

    def test_concurrent_submissions_create_one_record(self):
        draft = {"case_id": "AUTO-D09", "classification": "glass"}
        draft_hash = digest(draft)
        self.store.insert_review("rev9", "AUTO-D09", canonical(draft), draft_hash)
        self.store.transition_review("rev9", "approved", "ada")
        fingerprint = digest({"case_id": "AUTO-D09", "draft": draft, "review_id": "rev9"})
        barrier = threading.Barrier(2)
        results = []

        def submit():
            barrier.wait()
            results.append(self.store.commit_record(
                case_id="AUTO-D09",
                draft_canonical=canonical(draft),
                draft_hash=draft_hash,
                review_id="rev9",
                key="same-key",
                fingerprint=fingerprint,
                logical_key="loss-9",
                dataset_version="dataset-v1",
            ))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(code for code, _body in results), [200, 201])
        self.assertIsNotNone(self.store.get_record("loss-9"))

    def test_reset_removes_workflow_rows(self):
        exercise_store(self, self.store)
        self.store.reset()
        self.assertEqual(self.store.list_audit(), [])
        self.assertEqual(self.store.list_pending(), [])
        self.assertIsNone(self.store.get_record("loss-1"))


if __name__ == "__main__":
    unittest.main()
