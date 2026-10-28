"""Batch prepare_queue, dataset diff cursor, HTTP, and SQS poller behavior."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.runtime import create_server
from workbench.dataset_diff_poller import (
    DatasetDiffPoller,
    dataset_keys_from_sqs_body,
    is_dataset_json_key,
)
from workbench.reliability import ReliableCaller
from workbench.server import create_server as create_workbench
from workbench.service import Workbench
from workbench.storage import QUEUE_META_KEY

ROOT = Path(__file__).resolve().parents[1]


class MemoryQueueStateStore:
    """Postgres-shaped state store for cursor tests without a live database."""

    def __init__(self):
        self.rows = {}

    def read(self):
        return {key: json.loads(json.dumps(value)) for key, value in self.rows.items()}

    def write(self, state):
        for case_id, entry in state.items():
            self.rows[case_id] = json.loads(json.dumps(entry))

    def delete_cases(self, case_ids):
        for case_id in case_ids:
            self.rows.pop(case_id, None)


class FakeSqs:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.deleted = []

    def receive_message(self, **_kwargs):
        if not self.messages:
            return {"Messages": []}
        return {"Messages": [self.messages.pop(0)]}

    def delete_message(self, QueueUrl=None, ReceiptHandle=None, **_kwargs):
        self.deleted.append(ReceiptHandle)


class BatchPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock = create_server(ROOT / "data/dev", directory / "mock.sqlite", port=0)
        self.mock_thread = threading.Thread(
            target=self.mock.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.mock_thread.start()
        self.api = f"http://127.0.0.1:{self.mock.server_port}"
        self.state_path = directory / "workbench.json"
        self.workbench = Workbench(self.api, ROOT / "data/dev", self.state_path)

    def tearDown(self):
        self.mock.shutdown()
        self.mock.server_close()
        self.mock_thread.join(timeout=2)
        self.temporary.cleanup()

    def test_prepare_queue_processes_and_submits_selected_cases(self):
        result = self.workbench.prepare_queue(
            trace_id="tr_batch_one", case_ids=["AUTO-D01", "AUTO-D02"]
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trace_id"], "tr_batch_one")
        self.assertEqual(set(result["handled_case_ids"]), {"AUTO-D01", "AUTO-D02"})
        self.assertEqual(len(result["cases"]), 2)
        for row in result["cases"]:
            self.assertEqual(row["process"]["status"], "draft_ready")
            self.assertEqual(row["review"]["status"], "pending")
        second = self.workbench.prepare_queue(
            trace_id="tr_batch_locked", case_ids=["AUTO-D01"]
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["cases"][0]["process"]["error"], "draft_locked")
        self.assertIn("AUTO-D01", second["handled_case_ids"])

    def test_prepare_queue_defers_when_dependencies_unavailable(self):
        broken = Workbench(
            self.api,
            ROOT / "data/dev",
            self.state_path.with_name("broken.json"),
            reliable_caller=ReliableCaller(
                self.workbench.observability,
                max_attempts=1,
                failure_threshold=1,
                reset_timeout=60,
                base_delay=0,
                jitter_ratio=0,
            ),
        )
        broken.client.get_policy = lambda case_id, trace_id=None: (
            503,
            {"error": "upstream_timeout"},
        )
        broken.client.get_claims = lambda case_id, trace_id=None: (
            503,
            {"error": "upstream_timeout"},
        )
        result = broken.prepare_queue(trace_id="tr_dep", case_ids=["AUTO-D01"])
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["cases"][0]["error"], "dependency_unavailable")
        self.assertEqual(result["handled_case_ids"], [])
        self.assertIsNone((broken._read().get("AUTO-D01") or {}).get("draft"))


class DatasetDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock = create_server(ROOT / "data/dev", directory / "mock.sqlite", port=0)
        self.mock_thread = threading.Thread(
            target=self.mock.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.mock_thread.start()
        self.workbench = Workbench(
            f"http://127.0.0.1:{self.mock.server_port}",
            ROOT / "data/dev",
            directory / "workbench.json",
            state_store=MemoryQueueStateStore(),
        )
        self.workbench.supports_queue_cursor = True

    def tearDown(self):
        self.mock.shutdown()
        self.mock.server_close()
        self.mock_thread.join(timeout=2)
        self.temporary.cleanup()

    def test_apply_dataset_diff_prepares_only_added_ids(self):
        first = self.workbench.apply_dataset_diff(
            ["AUTO-D01", "AUTO-D02"], trace_id="tr_diff_1"
        )
        self.assertTrue(first["ack"])
        self.assertEqual(set(first["added_case_ids"]), {"AUTO-D01", "AUTO-D02"})
        self.assertEqual(set(first["handled_case_ids"]), {"AUTO-D01", "AUTO-D02"})
        meta = self.workbench.state_store.read()[QUEUE_META_KEY]["last_seen_case_ids"]
        self.assertEqual(set(meta), {"AUTO-D01", "AUTO-D02"})

        second = self.workbench.apply_dataset_diff(
            ["AUTO-D01", "AUTO-D02", "AUTO-D03"], trace_id="tr_diff_2"
        )
        self.assertEqual(second["added_case_ids"], ["AUTO-D03"])
        self.assertEqual(second["handled_case_ids"], ["AUTO-D03"])
        self.assertEqual(second["removed_case_ids"], [])

        third = self.workbench.apply_dataset_diff(
            ["AUTO-D01", "AUTO-D03"], trace_id="tr_diff_3"
        )
        self.assertEqual(third["added_case_ids"], [])
        self.assertEqual(third["removed_case_ids"], ["AUTO-D02"])
        meta = self.workbench.state_store.read()[QUEUE_META_KEY]["last_seen_case_ids"]
        self.assertEqual(set(meta), {"AUTO-D01", "AUTO-D03"})
        self.assertNotIn("AUTO-D02", self.workbench.state_store.read())


class PollerHelperTests(unittest.TestCase):
    def test_dataset_key_helpers(self):
        body = {
            "Records": [
                {"s3": {"object": {"key": "dataset.json"}}},
                {"s3": {"object": {"key": "documents/AUTO-D01.md"}}},
            ]
        }
        self.assertEqual(dataset_keys_from_sqs_body(body), ["dataset.json", "documents/AUTO-D01.md"])
        self.assertTrue(is_dataset_json_key("dataset.json"))
        self.assertFalse(is_dataset_json_key("documents/AUTO-D01.md"))

    def test_poller_acks_dataset_diff_message(self):
        temporary = tempfile.TemporaryDirectory()
        directory = Path(temporary.name)
        mock = create_server(ROOT / "data/dev", directory / "mock.sqlite", port=0)
        thread = threading.Thread(
            target=mock.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        try:
            workbench = Workbench(
                f"http://127.0.0.1:{mock.server_port}",
                ROOT / "data/dev",
                directory / "workbench.json",
                state_store=MemoryQueueStateStore(),
            )
            workbench.supports_queue_cursor = True
            message = {
                "ReceiptHandle": "abc",
                "Body": json.dumps({
                    "Records": [{"s3": {"object": {"key": "dataset.json"}}}]
                }),
            }
            sqs = FakeSqs([message])
            poller = DatasetDiffPoller(
                workbench, "https://example.invalid/queue", sqs_client=sqs
            )
            # Limit current IDs to one case for speed
            workbench.client.list_cases = lambda trace_id=None: [{"case_id": "AUTO-D01", "documents": []}]
            result = poller.poll_once()
            self.assertTrue(result.get("ack"))
            self.assertEqual(sqs.deleted, ["abc"])
        finally:
            mock.shutdown()
            mock.server_close()
            thread.join(timeout=2)
            temporary.cleanup()


class BatchHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock = create_server(ROOT / "data/dev", directory / "mock.sqlite", port=0)
        self.mock_thread = threading.Thread(
            target=self.mock.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.mock_thread.start()
        workbench = Workbench(
            api_url=f"http://127.0.0.1:{self.mock.server_port}",
            data_dir=ROOT / "data/dev",
            state_path=directory / "workbench.json",
        )
        self.server = create_workbench(workbench, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.mock.shutdown()
        self.mock.server_close()
        self.thread.join(timeout=2)
        self.mock_thread.join(timeout=2)
        self.temporary.cleanup()

    def test_queue_prepare_endpoint(self):
        request = Request(
            self.base + "/api/queue/prepare",
            data=json.dumps({"case_ids": ["AUTO-D01"]}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Trace-ID": "tr_http_batch",
            },
        )
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode())
            self.assertEqual(response.headers["X-Trace-ID"], "tr_http_batch")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["trace_id"], "tr_http_batch")
        self.assertEqual(payload["handled_case_ids"], ["AUTO-D01"])


if __name__ == "__main__":
    unittest.main()
