"""Consume durable S3 dataset-change notifications from Amazon SQS."""

from __future__ import annotations

import json
import threading
import uuid
from urllib.parse import unquote_plus


def dataset_keys_from_sqs_body(body):
    """Extract and URL-decode object keys from an S3 event notification."""

    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return []
    if not isinstance(body, dict):
        return []
    if isinstance(body.get("Message"), str):
        try:
            body = json.loads(body["Message"])
        except json.JSONDecodeError:
            return []
    records = body.get("Records")
    if not isinstance(records, list):
        return []
    keys = []
    for record in records:
        obj = ((record or {}).get("s3") or {}).get("object") or {}
        key = obj.get("key")
        if isinstance(key, str):
            keys.append(unquote_plus(key).lstrip("/"))
    return keys


def is_dataset_json_key(key, *, prefix=""):
    """Match only the configured dataset object, never supporting documents."""

    if not isinstance(key, str):
        return False
    expected = "/".join(part for part in (prefix.strip("/"), "dataset.json") if part)
    return key == expected


class DatasetDiffPoller:
    """Long-poll SQS and invoke the sequential dataset-diff workflow."""

    def __init__(
        self,
        workbench,
        queue_url,
        *,
        sqs_client=None,
        wait_seconds=20,
        visibility_timeout=900,
        evidence_prefix="",
        stop_event=None,
    ):
        if not queue_url:
            raise ValueError("DATASET_DIFF_QUEUE_URL is required")
        self.workbench = workbench
        self.queue_url = queue_url
        self.wait_seconds = wait_seconds
        self.visibility_timeout = visibility_timeout
        self.evidence_prefix = evidence_prefix or ""
        self.stop_event = stop_event or threading.Event()
        if sqs_client is None:
            import boto3

            sqs_client = boto3.client("sqs")
        self.sqs = sqs_client
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self.run_forever,
            name="dataset-diff-poller",
            daemon=True,
        )
        self._thread.start()
        self.workbench.observability.event("dataset_diff_poller_started")
        return self._thread

    def stop(self):
        self.stop_event.set()

    def run_forever(self):
        """Poll continuously, with bounded backoff after AWS/client errors."""

        failures = 0
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                failures = 0
            except Exception as error:
                failures += 1
                delay = min(30, 2 ** min(failures, 5))
                self.workbench.observability.event(
                    "dataset_diff_poller_error",
                    level="ERROR",
                    error_type=type(error).__name__,
                    retry_delay_seconds=delay,
                )
                self.stop_event.wait(delay)

    def poll_once(self):
        response = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=self.wait_seconds,
            VisibilityTimeout=self.visibility_timeout,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages") or []
        return self.handle_message(messages[0]) if messages else None

    def handle_message(self, message):
        trace_id = "tr_" + uuid.uuid4().hex[:16]
        receipt = message.get("ReceiptHandle")
        keys = dataset_keys_from_sqs_body(message.get("Body"))
        relevant = [
            key
            for key in keys
            if is_dataset_json_key(key, prefix=self.evidence_prefix)
        ]
        if not relevant:
            if receipt:
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
            self.workbench.observability.event(
                "dataset_diff_message_ignored", trace_id=trace_id, keys=keys
            )
            return {"status": "ignored", "trace_id": trace_id, "ack": True}

        try:
            current_ids = [
                case["case_id"]
                for case in self.workbench.client.list_cases(trace_id, refresh=True)
            ]
        except Exception as error:
            self.workbench.observability.event(
                "dataset_diff_list_failed",
                level="ERROR",
                trace_id=trace_id,
                error_type=type(error).__name__,
            )
            return {"status": "list_failed", "trace_id": trace_id, "ack": False}

        result = self.workbench.apply_dataset_diff(current_ids, trace_id=trace_id)
        if result.get("ack") and receipt:
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
        return result
