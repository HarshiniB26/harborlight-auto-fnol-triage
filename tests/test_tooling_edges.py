"""Behavioral coverage for operator-facing command modules."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from tools import (
    batch_prepare,
    check_secrets,
    evaluate,
    export_results,
    migrate_to_aws,
    validate_package,
    view_logs,
)


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class ToolingTests(unittest.TestCase):
    def test_batch_prepare_success_errors_and_transport_failure(self):
        with (
            patch.object(sys, "argv", ["batch_prepare", "--url", "http://unit", "--case-id", "A", "--trace-id", "tr_x"]),
            patch.object(batch_prepare, "urlopen", return_value=Response({"status": "completed"})) as opened,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            batch_prepare.main()
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"case_ids": ["A"]})
        self.assertEqual(request.headers["X-trace-id"], "tr_x")
        self.assertIn("completed", output.getvalue())

        error = HTTPError("http://unit", 409, "bad", {}, io.BytesIO(b'{"error":"bad"}'))
        with (
            patch.object(sys, "argv", ["batch_prepare"]),
            patch.object(batch_prepare, "urlopen", side_effect=error),
            patch("sys.stdout", new_callable=io.StringIO),
            self.assertRaises(SystemExit) as raised,
        ):
            batch_prepare.main()
        self.assertEqual(raised.exception.code, 1)

        for failure in (URLError("down"), TimeoutError("late"), OSError("closed")):
            with (
                patch.object(sys, "argv", ["batch_prepare"]),
                patch.object(batch_prepare, "urlopen", side_effect=failure),
                self.assertRaises(SystemExit) as raised,
            ):
                batch_prepare.main()
            self.assertIn("Workbench unreachable", str(raised.exception))

        for payload in ({"error": "bad"}, {"status": "completed_with_errors"}):
            with (
                patch.object(sys, "argv", ["batch_prepare"]),
                patch.object(batch_prepare, "urlopen", return_value=Response(payload)),
                patch("sys.stdout", new_callable=io.StringIO),
                self.assertRaises(SystemExit) as raised,
            ):
                batch_prepare.main()
            self.assertEqual(raised.exception.code, 1)

    def test_export_results_writes_atomically_and_rejects_bad_responses(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "nested" / "results.json"
            with (
                patch.object(sys, "argv", ["export_results", "--url", "http://unit/", "--output", str(output)]),
                patch.object(export_results, "urlopen", return_value=Response([{"case_id": "A"}])),
                patch("sys.stdout", new_callable=io.StringIO) as printed,
            ):
                export_results.main()
            self.assertEqual(json.loads(output.read_text()), [{"case_id": "A"}])
            self.assertIn("Exported 1 drafts", printed.getvalue())
            self.assertFalse(output.with_suffix(".json.tmp").exists())

            with (
                patch.object(sys, "argv", ["export_results", "--output", str(output)]),
                patch.object(export_results, "urlopen", return_value=Response({"not": "a list"})),
                self.assertRaises(SystemExit) as raised,
            ):
                export_results.main()
            self.assertIn("invalid results", str(raised.exception))

            detail = {"message": "prepare", "missing_case_ids": ["A", "B"]}
            error = HTTPError("http://unit", 409, "bad", {}, io.BytesIO(json.dumps(detail).encode()))
            with (
                patch.object(sys, "argv", ["export_results"]),
                patch.object(export_results, "urlopen", side_effect=error),
                self.assertRaises(SystemExit) as raised,
            ):
                export_results.main()
            self.assertIn("A, B", str(raised.exception))

    def test_secret_scanner_handles_safe_binary_synthetic_and_findings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.txt").write_text("SYNTHETIC-PRIVATE-ASIAABCDEFGHIJKLMNOP\n")
            (root / "binary.bin").write_bytes(b"\xff\xfe")
            skipped = root / ".git"
            skipped.mkdir()
            (skipped / "secret.txt").write_text('password="abcdefgh"')
            with (
                patch.object(check_secrets, "ROOT", root),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(check_secrets.main(), 0)
            self.assertIn("Credential scan passed", output.getvalue())

            (root / "unsafe.txt").write_text('password="abcdefgh"\nAKIAABCDEFGHIJKLMNOP\n')
            with (
                patch.object(check_secrets, "ROOT", root),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(check_secrets.main(), 1)
            self.assertIn("assigned secret", output.getvalue())
            self.assertIn("AWS access key", output.getvalue())

    def test_evaluator_reports_format_and_semantic_outcomes(self):
        dataset = {"sources": {"S": "exact evidence"}}
        gold = {
            "A": {
                "classification": ["collision"],
                "routing": ["standard"],
                "policy_ids": ["P"],
                "facts": {"vehicle_id": "V"},
                "missing_information": [],
                "deadline": None,
                "evidence_groups": [["S"]],
                "restricted_values": ["SECRET"],
            },
            "MISSING": {
                "classification": ["collision"], "routing": ["standard"],
                "policy_ids": [], "facts": {}, "missing_information": [],
                "deadline": None, "evidence_groups": [], "restricted_values": [],
            },
        }
        submission = [
            "bad",
            {"case_id": "EXTRA"},
            {
                "case_id": "A", "classification": "collision", "routing": "standard",
                "policy_ids": ["P"], "facts": {"vehicle_id": "V"},
                "missing_information": [], "deadline": None,
                "citations": [{"source_id": "S", "quote": "exact evidence"}],
                "follow_up_questions": [], "narrative": "A sufficiently long narrative for review.",
            },
            {"case_id": "A", "citations": ["bad", {"source_id": "X", "quote": "no"}],
             "missing_information": "bad", "policy_ids": "bad", "facts": "bad"},
        ]
        result = evaluate.evaluate(dataset, gold, submission)
        self.assertFalse(result["passed"])
        self.assertEqual(result["total_cases"], 2)
        self.assertGreaterEqual(len(result["format_errors"]), 3)

    def test_evaluate_main_writes_report_and_rejects_invalid_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "dataset.json").write_text('{"sources":{}}')
            gold = root / "gold.json"
            gold.write_text("{}")
            submission = root / "submission.json"
            submission.write_text("[]")
            report = root / "nested" / "report.json"
            argv = ["evaluate", "--data", str(data), "--gold", str(gold), "--submission", str(submission), "--report", str(report)]
            with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
                evaluate.main()
            self.assertEqual(raised.exception.code, 0)
            self.assertTrue(report.is_file())

            submission.write_text("{}")
            with patch.object(sys, "argv", argv[:-2]), self.assertRaises(SystemExit) as raised:
                evaluate.main()
            self.assertIn("Submission must be", str(raised.exception))

    def test_validate_fixture_and_render_log_timeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "policies").mkdir()
            (root / "documents" / "I.md").write_text("intake")
            (root / "policies" / "P.md").write_text("policy")
            payload = {"source_id": "T", "value": 1}
            dataset = {
                "cases": [{"case_id": "A", "intake_source": "I", "documents": ["I"], "tools": {"tool": payload}, "faults": {"tool": {}}}],
                "sources": {"I": "intake", "P": "policy", "T": json.dumps(payload, sort_keys=True)},
            }
            (root / "dataset.json").write_text(json.dumps(dataset))
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                validate_package.validate(root)
            self.assertIn("1 cases", output.getvalue())

            log = root / "events.log"
            log.write_text(
                "not-json\n"
                + json.dumps({"event": "processing_started", "trace_id": "t1", "case_id": "A", "timestamp": "now"}) + "\n"
                + json.dumps({"event": "record_created", "trace_id": "t1", "case_id": "A", "record_id": "R"}) + "\n"
                + json.dumps({"event": "unknown", "case_id": "A"}) + "\n"
                + json.dumps({"event": "processing_started", "case_id": "B"}) + "\n"
            )
            with patch.object(sys, "argv", ["view_logs", str(log), "--case", "A"]), patch("sys.stdout", new_callable=io.StringIO) as output:
                view_logs.main()
            self.assertIn("TRACE t1", output.getvalue())
            self.assertIn("Created record R", output.getvalue())
            self.assertIn("Ignored 1", output.getvalue())

            empty = root / "empty.log"
            empty.write_text("bad\n")
            with patch.object(sys, "argv", ["view_logs", str(empty)]), patch("sys.stdout", new_callable=io.StringIO) as output:
                view_logs.main()
            self.assertIn("No matching", output.getvalue())

    def test_migration_configures_role_and_uploads_all_evidence(self):
        class Result:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value = Result(None)
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {"SecretString": '{"username":"fnol_app","password":"pw"}'}
        s3 = MagicMock()
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / "documents").mkdir()
            (data / "policies").mkdir()
            (data / "documents" / "D.md").write_text("doc")
            (data / "policies" / "P.md").write_text("policy")
            (data / "dataset.json").write_text('{"cases":[{"case_id":"A"}]}')

            def client(name, **_):
                return secrets if name == "secretsmanager" else s3

            with (
                patch.object(migrate_to_aws.psycopg, "connect", return_value=connection),
                patch.object(migrate_to_aws.boto3, "client", side_effect=client),
                patch.object(migrate_to_aws, "ROOT", Path(__file__).resolve().parents[1]),
            ):
                cases, uploaded = migrate_to_aws.migrate("db", "bucket", data, "prefix/", application_secret_arn="secret")
                no_upload = migrate_to_aws.migrate("db", None, data, upload_evidence=False)
            self.assertEqual((cases, uploaded), (1, 3))
            self.assertEqual(no_upload, (1, 0))
            self.assertTrue(any("CREATE ROLE" in str(call.args[0]) for call in connection.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
