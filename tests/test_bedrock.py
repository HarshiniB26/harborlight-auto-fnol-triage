"""Bedrock stays out of routing and fails as a warning."""

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.runtime import create_server
from workbench.bedrock import UNAVAILABLE, advisory_annotation
from workbench.service import Workbench, _with_optional_model_note

ROOT = Path(__file__).resolve().parents[1]


class _Client:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.body = b""

    def invoke_model(self, **kwargs):
        self.body = kwargs["body"]
        if self.error:
            raise self.error
        return {"body": io.BytesIO(json.dumps(self.payload).encode())}


class BedrockTests(unittest.TestCase):
    def test_disabled_client_returns_nothing(self):
        self.assertIsNone(advisory_annotation({"case_id": "C1"}, enabled=False))
        self.assertEqual(_with_optional_model_note(["kept"], {"case_id": "C1"}), ["kept"])
        saved = os.environ.get("FNOL_BEDROCK_ENABLED")
        os.environ["FNOL_BEDROCK_ENABLED"] = "true"
        try:
            with patch("workbench.bedrock.advisory_annotation", return_value=None):
                self.assertEqual(_with_optional_model_note(["kept"], {"case_id": "C1"}), ["kept"])
        finally:
            if saved is None:
                os.environ.pop("FNOL_BEDROCK_ENABLED", None)
            else:
                os.environ["FNOL_BEDROCK_ENABLED"] = saved
        workbench = Workbench(
            api_url="http://127.0.0.1:9",
            data_dir=ROOT / "data" / "dev",
            state_path=ROOT / "unused.json",
            database_url="",
        )
        self.assertIsNone(workbench._database_url)

    def test_errors_and_success_do_not_carry_document_text(self):
        summary = {"case_id": "AUTO-D01", "classification": "collision", "routing": "standard"}
        self.assertEqual(
            advisory_annotation(summary, client=_Client(error=TimeoutError("slow")), model_id="model", enabled=True),
            UNAVAILABLE,
        )
        self.assertEqual(
            advisory_annotation(summary, client=_Client(payload={"content": []}), model_id="model", enabled=True),
            UNAVAILABLE,
        )
        client = _Client(payload={"content": [{"text": "A person must approve this draft."}]})
        note = advisory_annotation(summary, client=client, model_id="anthropic.example", enabled=True)
        self.assertIn("Non-authoritative model note", note)
        self.assertIn("A person must approve", note)
        self.assertNotIn("private_token", client.body.decode())
        self.assertNotIn("INTAKE", client.body.decode())
        text_client = _Client(payload=None)
        text_client.invoke_model = lambda **kwargs: {
            "body": json.dumps({"content": [{"text": "A person must approve."}]})
        }
        self.assertIn("A person must approve", advisory_annotation(
            summary, client=text_client, model_id="anthropic.example", enabled=True
        ))
        self.assertEqual(
            advisory_annotation(summary, client=_Client(error=RuntimeError("down")), model_id="model", enabled=True),
            UNAVAILABLE,
        )

    def test_client_factory_and_missing_sdk(self):
        import sys

        import boto3

        from workbench.bedrock import _client

        with patch.object(boto3, "client", return_value="runtime-client") as created:
            self.assertEqual(_client(), "runtime-client")
        self.assertEqual(created.call_args.kwargs["config"].read_timeout, 8)
        with patch.dict(sys.modules, {"boto3": None}):
            self.assertIsNone(_client())
            self.assertEqual(
                advisory_annotation({"case_id": "C1"}, enabled=True, model_id="model"),
                UNAVAILABLE,
            )

    def test_rules_and_drafts_do_not_call_the_model(self):
        for name in ("rules.py", "draft.py"):
            text = (ROOT / "workbench" / name).read_text().lower()
            self.assertNotIn("bedrock", text)

    def test_enabled_process_adds_a_warning_without_changing_the_route(self):
        saved = os.environ.get("FNOL_BEDROCK_ENABLED")
        model = os.environ.get("FNOL_BEDROCK_MODEL_ID")
        os.environ["FNOL_BEDROCK_ENABLED"] = "true"
        os.environ.pop("FNOL_BEDROCK_MODEL_ID", None)
        temporary = tempfile.TemporaryDirectory()
        directory = Path(temporary.name)
        runtime = create_server(ROOT / "data" / "dev", directory / "mock.sqlite", port=0)
        thread = threading.Thread(target=runtime.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            workbench = Workbench(
                api_url=f"http://127.0.0.1:{runtime.server_port}",
                data_dir=ROOT / "data" / "dev",
                state_path=directory / "workbench.json",
            )
            result = workbench.process("AUTO-D01")
            self.assertEqual(result["draft"]["routing"], "standard")
            self.assertEqual(result["draft"]["classification"], "collision")
            self.assertTrue(any("Model note unavailable" in warning for warning in result["warnings"]))
        finally:
            runtime.shutdown()
            runtime.server_close()
            thread.join(timeout=2)
            temporary.cleanup()
            if saved is None:
                os.environ.pop("FNOL_BEDROCK_ENABLED", None)
            else:
                os.environ["FNOL_BEDROCK_ENABLED"] = saved
            if model is None:
                os.environ.pop("FNOL_BEDROCK_MODEL_ID", None)
            else:
                os.environ["FNOL_BEDROCK_MODEL_ID"] = model

    def test_database_backed_draft_state_is_optional(self):
        saved = os.environ.get("FNOL_DATABASE_URL")
        os.environ["FNOL_DATABASE_URL"] = "postgresql://example.invalid/fnol"
        try:
            with patch("tools.aws_config.application_database_url", return_value="postgresql://example.invalid/fnol"):
                workbench = Workbench(
                    api_url="http://127.0.0.1:9",
                    data_dir=ROOT / "data" / "dev",
                    state_path=Path("/tmp/unused-workbench.json"),
                )
            self.assertEqual(workbench._database_url, "postgresql://example.invalid/fnol")
            stored = {}

            def write_document(url, state):
                stored["document"] = state

            def read_document(url):
                return stored.get("document") or {}

            with patch("tools.workbench_state.write_document", write_document), \
                    patch("tools.workbench_state.read_document", read_document):
                workbench._write({"AUTO-D01": {"draft": {"case_id": "AUTO-D01"}}})
                self.assertEqual(workbench._read()["AUTO-D01"]["draft"]["case_id"], "AUTO-D01")
        finally:
            if saved is None:
                os.environ.pop("FNOL_DATABASE_URL", None)
            else:
                os.environ["FNOL_DATABASE_URL"] = saved


if __name__ == "__main__":
    unittest.main()
