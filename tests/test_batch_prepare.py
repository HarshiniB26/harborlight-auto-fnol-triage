
"""Sequential queue preparation and durable S3/SQS dataset-diff behavior."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tools.runtime import create_server
from workbench.dataset_diff_poller import DatasetDiffPoller, dataset_keys_from_sqs_body
from workbench.reliability import ReliableCaller
from workbench.service import Workbench
from workbench.storage import QUEUE_META_KEY

ROOT = Path(__file__).resolve().parents[1]


class MemoryQueueStateStore:
    def __init__(self):
        self.rows = {}

    def read(self):
        return json.loads(json.dumps(self.rows))

    def write(self, state):
        self.rows.update(json.loads(json.dumps(state)))


class FakeSqs:
    def __init__(self):
        self.deleted = []

    def delete_message(self, QueueUrl=None, ReceiptHandle=None):
        self.deleted.append(ReceiptHandle)


class BatchPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)

        self.runtime = create_server(
            ROOT / "data/dev",
            directory / "mock.sqlite",
            port=0,
        )

        self.thread = threading.Thread(
            target=self.runtime.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

        self.store = MemoryQueueStateStore()

        self.workbench = Workbench(
            f"http://127.0.0.1:{self.runtime.server_port}",
            ROOT / "data/dev",
            directory / "workbench.json",
            state_store=self.store,
            queue_cursor=True,
        )

    def tearDown(self):
        self.runtime.shutdown()
        self.runtime.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_initial_batch_is_sequential_and_auto_submits_reviews(self):
        calls = []
        original = self.workbench.process

        def tracked(case_id, trace_id=None):
            calls.append(case_id)
            return original(case_id, trace_id)

        self.workbench.process = tracked

        result = self.workbench.prepare_queue(
            trace_id="tr_initial",
            case_ids=["AUTO-D01", "AUTO-D02"],
        )

        self.assertEqual(calls, ["AUTO-D01", "AUTO-D02"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["handled_case_ids"],
            ["AUTO-D01", "AUTO-D02"],
        )
        self.assertTrue(
            all(
                row["review"]["status"] == "pending"
                for row in result["cases"]
            )
        )

    def test_dependency_outage_routes_to_manual_review(self):
        broken = Workbench(
            f"http://127.0.0.1:{self.runtime.server_port}",
            ROOT / "data/dev",
            Path(self.temporary.name) / "broken.json",
            state_store=MemoryQueueStateStore(),
            queue_cursor=True,
            reliable_caller=ReliableCaller(
                self.workbench.observability,
                max_attempts=1,
                failure_threshold=1,
                base_delay=0,
                jitter_ratio=0,
            ),
        )

        # Simulate an unavailable Claims API.
        broken.client.get_claims = lambda case_id, trace_id=None: (
            503,
            {"error": "upstream_timeout"},
        )

        result = broken.prepare_queue(
            trace_id="tr_fallback",
            case_ids=["AUTO-D01"],
        )

        # An unavailable Claims API is a manual-review condition,
        # not a reason to discard the draft.
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["failed_case_ids"], [])
        self.assertEqual(result["handled_case_ids"], ["AUTO-D01"])

        row = result["cases"][0]
        draft = row["process"]["draft"]

        self.assertEqual(draft["routing"], "manual_review")

        # The draft must enter the normal handler review workflow.
        self.assertEqual(row["review"]["status"], "pending")

        # Verify the draft and review are available to the handler.
        view = broken.case_view("AUTO-D01")

        self.assertEqual(view["status"], "pending")
        self.assertIsNotNone(view["draft"])
        self.assertIsNotNone(view["review"])
        self.assertEqual(view["review"]["status"], "pending")
        self.assertEqual(
            view["review"]["review_id"],
            row["review"]["review_id"],
        )

        # A human decision is still required.
        self.assertIsNone(view["record"])
        self.assertIn("AUTO-D01", broken.state_store.read())

    def test_dataset_diff_only_processes_additions_and_preserves_old_state(self):
        first = self.workbench.apply_dataset_diff(
            ["AUTO-D01", "AUTO-D02"],
            trace_id="tr_diff_1",
        )

        self.assertTrue(first["ack"])
        self.assertEqual(
            first["handled_case_ids"],
            ["AUTO-D01", "AUTO-D02"],
        )

        second = self.workbench.apply_dataset_diff(
            ["AUTO-D02", "AUTO-D03"],
            trace_id="tr_diff_2",
        )

        self.assertEqual(second["added_case_ids"], ["AUTO-D03"])
        self.assertEqual(second["removed_case_ids"], ["AUTO-D01"])
        self.assertIn("AUTO-D01", self.store.read())

        self.assertEqual(
            self.store.read()[QUEUE_META_KEY]["last_seen_case_ids"],
            ["AUTO-D01", "AUTO-D02", "AUTO-D03"],
        )

    def test_hard_failure_is_not_acknowledged_and_success_is_checkpointed(self):
        original = self.workbench.process

        def fail_second(case_id, trace_id=None):
            if case_id == "AUTO-D02":
                raise RuntimeError("technical failure")
            return original(case_id, trace_id)

        self.workbench.process = fail_second

        result = self.workbench.apply_dataset_diff(
            ["AUTO-D01", "AUTO-D02"],
            trace_id="tr_partial",
        )

        self.assertFalse(result["ack"])
        self.assertEqual(result["handled_case_ids"], ["AUTO-D01"])
        self.assertEqual(result["failed_case_ids"], ["AUTO-D02"])

        self.assertEqual(
            self.store.read()[QUEUE_META_KEY]["last_seen_case_ids"],
            ["AUTO-D01"],
        )

    def test_poller_forces_fresh_case_list_and_acks_only_success(self):
        calls = []

        self.workbench.client.list_cases = (
            lambda trace_id=None, refresh=False: (
                calls.append(refresh)
                or [{"case_id": "AUTO-D01", "documents": []}]
            )
        )

        sqs = FakeSqs()

        poller = DatasetDiffPoller(
            self.workbench,
            "https://example.invalid/queue",
            sqs_client=sqs,
        )

        message = {
            "ReceiptHandle": "receipt-1",
            "Body": json.dumps({
                "Records": [
                    {"s3": {"object": {"key": "dataset.json"}}}
                ]
            }),
        }

        result = poller.handle_message(message)

        self.assertTrue(calls[0])
        self.assertTrue(result["ack"])
        self.assertEqual(sqs.deleted, ["receipt-1"])

    def test_s3_keys_are_url_decoded(self):
        body = {
            "Records": [
                {"s3": {"object": {"key": "folder%2Fdataset.json"}}}
            ]
        }

        self.assertEqual(
            dataset_keys_from_sqs_body(body),
            ["folder/dataset.json"],
        )

    def test_bootstrap_checkpoints_an_already_created_record(self):
        self.workbench.process("AUTO-D01", "tr_existing")
        self.workbench.submit_review("AUTO-D01", "tr_existing")

        self.workbench.decide_review(
            "AUTO-D01",
            "Taylor Handler",
            "approved",
            "tr_existing",
        )

        # Step 4.1 automatically files the claim on approval.
        # A repeated filing call must reuse the existing record.
        self.workbench.file_record("AUTO-D01", "tr_existing")

        result = self.workbench.apply_dataset_diff(
            ["AUTO-D01"],
            trace_id="tr_bootstrap_existing",
        )

        self.assertTrue(result["ack"])
        self.assertEqual(
            result["handled_case_ids"],
            ["AUTO-D01"],
        )
        self.assertEqual(
            self.workbench.case_view("AUTO-D01")["status"],
            "record_created",
        )


if __name__ == "__main__":
    unittest.main()
