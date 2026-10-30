"""Storage adapter behavior without requiring live AWS or PostgreSQL."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from workbench.storage import (
    JsonStateStore,
    LocalEvidenceStore,
    PostgresStateStore,
    S3EvidenceStore,
    database_url_from_environment,
)


class StorageTests(unittest.TestCase):
    def test_local_evidence_and_json_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "policies").mkdir()
            (root / "documents" / "DOC-1.md").write_text("document")
            (root / "policies" / "POL-1.md").write_text("policy")
            evidence = LocalEvidenceStore(root)
            self.assertEqual(evidence.document("DOC-1"), "document")
            self.assertEqual(evidence.policy("POL-1"), "policy")
            self.assertIsNone(evidence.document("../unsafe"))
            self.assertIsNone(evidence.policy("missing"))

            path = root / "state" / "workbench.json"
            state = JsonStateStore(path)
            self.assertEqual(state.read(), {})
            expected = {"AUTO-D01": {"draft": {"case_id": "AUTO-D01"}}}
            state.write(expected)
            self.assertEqual(state.read(), expected)
            path.write_text(json.dumps({"cases": {}, "bad": {}, **expected}))
            self.assertEqual(state.read(), expected)
            path.write_text("[]")
            self.assertEqual(state.read(), {})

    def test_database_url_environment_and_secret(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://direct"}, clear=True):
            self.assertEqual(database_url_from_environment(), "postgresql://direct")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
                database_url_from_environment()

        secrets = MagicMock()
        secrets.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {
                    "username": "app user",
                    "password": "p@ss/word",
                    "host": "db.internal",
                    "port": 5432,
                    "dbname": "fnol",
                }
            )
        }
        with (
            patch.dict(
                "os.environ",
                {"DB_SECRET_ARN": "secret-arn", "AWS_REGION": "us-east-1"},
                clear=True,
            ),
            patch("boto3.client", return_value=secrets),
        ):
            url = database_url_from_environment()
        self.assertEqual(
            url,
            "postgresql://app%20user:p%40ss%2Fword@db.internal:5432/fnol?sslmode=require",
        )

    def test_s3_evidence_reads_and_handles_missing_keys(self):
        client = MagicMock()
        client.get_object.return_value = {"Body": io.BytesIO(b"private evidence")}
        store = S3EvidenceStore("bucket", "prefix", client)
        self.assertEqual(store.document("AUTO-D01-INTAKE"), "private evidence")
        client.get_object.assert_called_with(
            Bucket="bucket", Key="prefix/documents/AUTO-D01-INTAKE.md"
        )
        self.assertIsNone(store.policy("../unsafe"))

        missing = RuntimeError("missing")
        missing.response = {"Error": {"Code": "NoSuchKey"}}
        client.get_object.side_effect = missing
        self.assertIsNone(store.policy("MISSING"))
        denied = RuntimeError("denied")
        denied.response = {"Error": {"Code": "AccessDenied"}}
        client.get_object.side_effect = denied
        with self.assertRaises(RuntimeError):
            store.policy("DENIED")
        with self.assertRaisesRegex(ValueError, "bucket"):
            S3EvidenceStore("", client=client)

    def test_postgres_state_store_reads_and_upserts(self):
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            PostgresStateStore("")

        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = [
            ("AUTO-D01", {"draft": {"case_id": "AUTO-D01"}})
        ]
        store = PostgresStateStore("postgresql://database")
        with patch("psycopg.connect", return_value=connection):
            state = store.read()
            store.write(state)
        self.assertEqual(state["AUTO-D01"]["draft"]["case_id"], "AUTO-D01")
        self.assertGreaterEqual(connection.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
