import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import hardening_demo


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class DemoAndEntrypointTests(unittest.TestCase):
    def test_hardening_demo_and_all_event_summaries(self):
        rows = [
            ({"event": "tool_call_failed", "attempt": 1, "dependency": "policy", "status": 503, "retryable": True}, "retryable=true"),
            ({"event": "tool_call_retry_scheduled", "backoff_ms": 10, "next_attempt": 2}, "attempt 2"),
            ({"event": "tool_call_completed", "attempt": 2, "dependency": "policy", "status": 200}, "success"),
            ({"event": "circuit_opened", "dependency": "policy"}, "OPEN"),
            ({"event": "circuit_rejected", "dependency": "policy", "circuit_state": "open"}, "rejected"),
            ({"event": "anything_else"}, "anything_else"),
        ]
        for row, expected in rows:
            self.assertIn(expected, hardening_demo.event_summary(row))
        stream = io.StringIO('{"event":"one"}\nnot-json\n')
        with self.assertRaises(json.JSONDecodeError):
            hardening_demo.events_from(stream)

        output = io.StringIO()
        real_process = hardening_demo.Workbench.process

        def process_without_draft_for_table(self, case_id, trace_id=None):
            result = real_process(self, case_id, trace_id)
            if trace_id == "tr_demo_auto-d07":
                return {"warnings": [], "trace_id": result.get("trace_id", trace_id)}
            return result

        with redirect_stdout(output), patch.object(
            hardening_demo.Workbench, "process", process_without_draft_for_table
        ):
            hardening_demo.main()
        rendered = output.getvalue()
        self.assertIn("WEEK 10 REQUIRED SCENARIOS", rendered)
        self.assertIn("deferred", rendered)
        self.assertIn("IDEMPOTENCY", rendered)

        hardening_demo.assert_idempotent_replay(
            {"status": "created"}, 200, "record", "record", 1, 1
        )
        with self.assertRaises(AssertionError):
            hardening_demo.assert_idempotent_replay(
                {"status": "failed"}, 500, "other", "record", 2, 0
            )

        root_text = str(hardening_demo.ROOT)
        original_path = list(sys.path)
        sys.path[:] = [entry for entry in sys.path if entry != root_text]
        try:
            runpy.run_module("tools.hardening_demo", run_name="coverage_import")
        finally:
            sys.path[:] = original_path
        self._execute_main_guard(Path(hardening_demo.__file__), MagicMock())

    def test_simple_module_entrypoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            with patch("urllib.request.urlopen", return_value=Response({"status": "completed"})), \
                 patch.object(sys, "argv", ["batch_prepare", "--url", "http://test"]), \
                 redirect_stdout(io.StringIO()):
                runpy.run_module("tools.batch_prepare", run_name="__main__")

            output = root / "results.json"
            with patch("urllib.request.urlopen", return_value=Response([])), \
                 patch.object(sys, "argv", ["export_results", "--url", "http://test", "--output", str(output)]), \
                 redirect_stdout(io.StringIO()):
                runpy.run_module("tools.export_results", run_name="__main__")
            self.assertEqual(json.loads(output.read_text()), [])

            log = root / "events.log"
            log.write_text('{"event":"processing_started","trace_id":"t"}\n')
            with patch.object(sys, "argv", ["view_logs", str(log)]), redirect_stdout(io.StringIO()):
                runpy.run_module("tools.view_logs", run_name="__main__")

            with redirect_stdout(io.StringIO()):
                runpy.run_module("tools.validate_package", run_name="__main__")

            with patch("pathlib.Path.rglob", return_value=[]), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_module("tools.check_secrets", run_name="__main__")
            self.assertEqual(stopped.exception.code, 0)

    def test_evaluate_and_migration_entrypoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "dataset.json").write_text(json.dumps({"sources": {}}))
            gold = root / "gold.json"
            gold.write_text("{}")
            submission = root / "submission.json"
            submission.write_text("[]")
            report = root / "report.json"
            argv = ["evaluate", "--data", str(data), "--gold", str(gold), "--submission", str(submission), "--report", str(report)]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_module("tools.evaluate", run_name="__main__")
            self.assertEqual(stopped.exception.code, 0)
            self.assertTrue(report.exists())

            without_report = ["evaluate", "--data", str(data), "--gold", str(gold), "--submission", str(submission)]
            with patch.object(sys, "argv", without_report), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runpy.run_module("tools.evaluate", run_name="__main__")

            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.__exit__.return_value = False
            with patch("psycopg.connect", return_value=connection), \
                 patch("workbench.storage.database_url_from_environment", return_value="postgresql://test"), \
                 patch.object(sys, "argv", ["migrate_to_aws", "--data", "data/dev", "--skip-s3"]), \
                 redirect_stdout(io.StringIO()):
                runpy.run_module("tools.migrate_to_aws", run_name="__main__")

            with patch.object(sys, "argv", ["migrate_to_aws", "--data", "data/dev"]), \
                 patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit), \
                 redirect_stdout(io.StringIO()):
                runpy.run_module("tools.migrate_to_aws", run_name="__main__")

    @staticmethod
    def _execute_main_guard(path, main):
        """Execute the source file's real two-line main guard in isolation."""

        lines = path.read_text().splitlines()
        guard = "\n" * (len(lines) - 2) + "\n".join(lines[-2:]) + "\n"
        exec(compile(guard, str(path), "exec"), {"__name__": "__main__", "main": main})
        main.assert_called_once()


if __name__ == "__main__":
    unittest.main()
