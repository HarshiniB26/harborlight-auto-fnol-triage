"""Long-poll SQS for dataset.json ObjectCreated events and run ID diffs."""

from __future__ import annotations

import json
import threading
import uuid


def dataset_keys_from_sqs_body(body):
    """Extract object keys from an S3 event notification payload."""

    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return []
    if not isinstance(body, dict):
        return []
    # S3 → SQS may wrap the event in an SNS envelope.
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
        if not isinstance(record, dict):
            continue
        s3 = record.get("s3")
        if not isinstance(s3, dict):
            continue
        obj = s3.get("object")
        if not isinstance(obj, dict):
            continue
        key = obj.get("key")
        if isinstance(key, str):
            keys.append(key.lstrip("/"))
    return keys


def is_dataset_json_key(key, *, prefix=""):
    """Return whether the object key is the evidence dataset fixture."""

    if not isinstance(key, str):
        return False
    expected = "/".join(part for part in (prefix.strip("/"), "dataset.json") if part)
    return key == expected or key.endswith("/dataset.json") or key == "dataset.json"


class DatasetDiffPoller:
    """Background consumer that turns dataset uploads into prepare diffs."""

    def __init__(
        self,
        workbench,
        queue_url,
        *,
        sqs_client=None,
        wait_seconds=20,
        visibility_timeout=120,
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
        """Start the daemon poller thread once."""

        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self.run_forever,
            name="dataset-diff-poller",
            daemon=True,
        )
        self._thread.start()
        self.workbench.observability.event(
            "dataset_diff_poller_started",
            queue_configured=True,
        )
        return self._thread

    def stop(self):
        """Signal the poller loop to exit."""

        self.stop_event.set()

    def run_forever(self):
        """Long-poll until stopped."""

        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as error:
                self.workbench.observability.event(
                    "dataset_diff_poller_error",
                    level="ERROR",
                    error_type=type(error).__name__,
                )

    def poll_once(self):
        """Receive up to one message and process a dataset diff when applicable."""

        response = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=self.wait_seconds,
            VisibilityTimeout=self.visibility_timeout,
        )
        messages = response.get("Messages") or []
        if not messages:
            return None
        message = messages[0]
        return self.handle_message(message)

    def handle_message(self, message):
        """Run apply_dataset_diff for dataset.json events; ignore other keys."""

        trace_id = "tr_" + uuid.uuid4().hex[:16]
        receipt = message.get("ReceiptHandle")
        keys = dataset_keys_from_sqs_body(message.get("Body"))
        dataset_keys = [
            key for key in keys if is_dataset_json_key(key, prefix=self.evidence_prefix)
        ]
        if keys and not dataset_keys:
            if receipt:
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
            return {"status": "ignored", "trace_id": trace_id, "ack": True}
        try:
            current_ids = [
                case["case_id"] for case in self.workbench.client.list_cases(trace_id)
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
