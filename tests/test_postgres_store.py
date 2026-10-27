"""PostgreSQL contract tests. They skip unless a local database is reachable."""

import os
import unittest
from pathlib import Path

from tests.test_state_store import exercise_store
from tools.migrate import _statements, apply_migrations
from tools.state_store import PostgresStore
from tools.workbench_state import read_document, write_document
from workbench.service import Workbench

ROOT = Path(__file__).resolve().parents[1]
LOCAL_URL = os.environ.get(
    "FNOL_TEST_DATABASE_URL",
    "postgresql://fnol:fnol_local_test@127.0.0.1:5432/fnol",
)


def _reachable():
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(LOCAL_URL, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
        return True
    except Exception:
        return False


class MigrationScriptTests(unittest.TestCase):
    def test_migration_files_split_into_statements(self):
        script = (ROOT / "infra" / "db" / "migrations" / "001_initial.sql").read_text()
        statements = _statements(script)
        self.assertGreaterEqual(len(statements), 6)
        self.assertTrue(any(statement.upper().startswith("CREATE TABLE") for statement in statements))


@unittest.skipUnless(_reachable(), "local PostgreSQL is not running")
class PostgresStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apply_migrations(LOCAL_URL)

    def setUp(self):
        self.store = PostgresStore(LOCAL_URL)
        self.store.reset()
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM workbench_state")

    def test_postgres_matches_the_sqlite_workflow_contract(self):
        exercise_store(self, self.store)

    def test_workbench_document_round_trip(self):
        write_document(LOCAL_URL, {"AUTO-D02": {"draft": {"case_id": "AUTO-D02"}, "warnings": []}})
        self.assertEqual(read_document(LOCAL_URL)["AUTO-D02"]["draft"]["case_id"], "AUTO-D02")
        workbench = Workbench(
            api_url="http://127.0.0.1:9",
            data_dir=ROOT / "data" / "dev",
            state_path=ROOT / "unused.json",
            database_url=LOCAL_URL,
        )
        self.assertEqual(workbench._read()["AUTO-D02"]["draft"]["case_id"], "AUTO-D02")


if __name__ == "__main__":
    unittest.main()
