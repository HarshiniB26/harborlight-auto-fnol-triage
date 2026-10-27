"""SQLite and PostgreSQL storage for runtime workflow state.

Local development and the existing tests use SQLite. PostgreSQL is selected
only when a URL is passed or an AWS database setting is present. Record
creation and its idempotency row commit or roll back together.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    """Serialize a value the same way the runtime hashes request bodies."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class StoreIntegrityError(Exception):
    """A uniqueness constraint rejected a write."""


def open_store(state_path, database_url=None):
    """Open SQLite unless ``database_url`` or the AWS database settings select PostgreSQL."""

    if database_url is None:
        from tools.aws_config import application_database_url

        database_url = application_database_url()
    if database_url:
        return PostgresStore(database_url)
    return SqliteStore(state_path)


class _Transaction:
    def __init__(self, store, connection):
        self.store = store
        self.connection = connection

    def execute(self, sql, params=()):
        cursor = self.connection.execute(self.store.adapt(sql), tuple(params))
        return cursor.rowcount

    def fetchone(self, sql, params=()):
        cursor = self.connection.execute(self.store.adapt(sql), tuple(params))
        row = cursor.fetchone()
        if row is None:
            return None
        return _as_dict(row)

    def fetchall(self, sql, params=()):
        cursor = self.connection.execute(self.store.adapt(sql), tuple(params))
        return [_as_dict(row) for row in cursor.fetchall()]

    def audit(self, event, case_id="", **detail):
        self.execute(
            "INSERT INTO audit(at, event, case_id, detail) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), event, case_id, canonical(detail)),
        )


def _as_dict(row):
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


class _Store:
    dialect = ""

    def adapt(self, sql):
        return sql

    def transaction(self):
        raise NotImplementedError

    def close(self):
        return None

    def insert_review(self, review_id, case_id, draft_canonical, draft_hash):
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO reviews(id, case_id, draft, hash, status, reviewer) VALUES(?,?,?,?,?,?)",
                (review_id, case_id, draft_canonical, draft_hash, "pending", None),
            )
            tx.audit("review_requested", case_id, review_id=review_id, draft_hash=draft_hash)

    def get_review(self, review_id):
        with self.transaction() as tx:
            return tx.fetchone(
                "SELECT id, case_id, draft, hash, status, reviewer FROM reviews WHERE id=?",
                (review_id,),
            )

    def transition_review(self, review_id, status, reviewer):
        """Approve or reject a review that is still pending. A second call does not win."""

        with self.transaction() as tx:
            row = tx.fetchone("SELECT id, case_id, draft, hash, status, reviewer FROM reviews WHERE id=?", (review_id,))
            if not row or row["status"] != "pending":
                return None
            updated = tx.execute(
                "UPDATE reviews SET status=?, reviewer=? WHERE id=? AND status='pending'",
                (status, reviewer, review_id),
            )
            if updated != 1:
                return None
            tx.audit(
                "human_" + status,
                row["case_id"],
                review_id=review_id,
                reviewer=reviewer,
                draft_hash=row["hash"],
            )
            changed = dict(row)
            changed["status"] = status
            changed["reviewer"] = reviewer
            return changed

    def list_pending(self):
        with self.transaction() as tx:
            return tx.fetchall(
                "SELECT id, case_id, draft, hash, status, reviewer FROM reviews WHERE status='pending'"
            )

    def get_record(self, logical_key):
        """Return the public record columns. The dataset version stays off this body."""

        with self.transaction() as tx:
            return tx.fetchone(
                "SELECT id, case_id, draft, review_id FROM records WHERE id=?",
                (logical_key,),
            )

    def record_tool_attempt(self, case_id, tool, fault):
        with self.transaction() as tx:
            self.lock_call(tx, case_id, tool)
            row = tx.fetchone("SELECT n FROM calls WHERE case_id=? AND tool=?", (case_id, tool))
            count = (row["n"] if row else 0) + 1
            self.upsert_call(tx, case_id, tool, count)
            failed = bool(fault) and (fault.get("failures") == "always" or count <= fault["failures"])
            tx.audit("tool_read", case_id, tool=tool, attempt=count, status=503 if failed else 200)
            return count, failed

    def lock_call(self, tx, case_id, tool):
        return None

    def upsert_call(self, tx, case_id, tool, count):
        raise NotImplementedError

    def audit(self, event, case_id="", **detail):
        with self.transaction() as tx:
            tx.audit(event, case_id, **detail)

    def list_audit(self):
        with self.transaction() as tx:
            return tx.fetchall("SELECT seq, at, event, case_id, detail FROM audit ORDER BY seq")

    def reset(self):
        with self.transaction() as tx:
            for table in ("reviews", "records", "keys", "calls", "audit"):
                tx.execute(f"DELETE FROM {table}")

    def commit_record(
        self,
        *,
        case_id,
        draft_canonical,
        draft_hash,
        review_id,
        key,
        fingerprint,
        logical_key,
        dataset_version,
    ):
        """Insert a record and its idempotency result, or replay the earlier result."""

        try:
            return self._commit_record_once(
                case_id=case_id,
                draft_canonical=draft_canonical,
                draft_hash=draft_hash,
                review_id=review_id,
                key=key,
                fingerprint=fingerprint,
                logical_key=logical_key,
                dataset_version=dataset_version,
            )
        except StoreIntegrityError:
            return self._commit_record_once(
                case_id=case_id,
                draft_canonical=draft_canonical,
                draft_hash=draft_hash,
                review_id=review_id,
                key=key,
                fingerprint=fingerprint,
                logical_key=logical_key,
                dataset_version=dataset_version,
            )

    def _commit_record_once(
        self,
        *,
        case_id,
        draft_canonical,
        draft_hash,
        review_id,
        key,
        fingerprint,
        logical_key,
        dataset_version,
    ):
        try:
            with self.transaction() as tx:
                self.lock_record(tx, key, logical_key)
                previous = tx.fetchone("SELECT key, hash, response FROM keys WHERE key=?", (key,))
                if previous:
                    if previous["hash"] != fingerprint:
                        tx.audit("idempotency_conflict", case_id)
                        return 409, {"error": "idempotency_conflict"}
                    tx.audit("write_replayed", case_id)
                    return 200, json.loads(previous["response"])
                review = tx.fetchone(
                    "SELECT id, case_id, draft, hash, status, reviewer FROM reviews WHERE id=?",
                    (review_id,),
                )
                if (
                    not review
                    or review["case_id"] != case_id
                    or review["status"] != "approved"
                    or review["hash"] != draft_hash
                ):
                    tx.audit("unapproved_write_attempt", case_id, review_id=review_id)
                    return 403, {"error": "approval_missing_or_draft_changed"}
                existing = tx.fetchone("SELECT id FROM records WHERE id=?", (logical_key,))
                if existing:
                    tx.audit("duplicate_prevented", case_id, existing_record_id=logical_key)
                    return 409, {"error": "duplicate_case", "existing_record_id": logical_key}
                response = {
                    "record_id": logical_key,
                    "case_id": case_id,
                    "status": "created",
                    "review_id": review_id,
                }
                tx.execute(
                    "INSERT INTO records(id, case_id, draft, review_id, dataset_version) VALUES(?,?,?,?,?)",
                    (logical_key, case_id, draft_canonical, review_id, dataset_version),
                )
                tx.execute(
                    "INSERT INTO keys(key, hash, response) VALUES(?,?,?)",
                    (key, fingerprint, canonical(response)),
                )
                tx.audit(
                    "record_created",
                    case_id,
                    record_id=logical_key,
                    review_id=review_id,
                    draft_hash=draft_hash,
                    reviewer=review["reviewer"],
                    dataset_version=dataset_version,
                )
                return 201, response
        except Exception as error:
            if _is_integrity_error(error):
                raise StoreIntegrityError(str(error)) from error
            raise

    def lock_record(self, tx, key, logical_key):
        return None


def _is_integrity_error(error):
    name = type(error).__name__
    if name in {"IntegrityError", "UniqueViolation"}:
        return True
    text = str(error).lower()
    return "unique" in text or "duplicate key" in text


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews(
  id TEXT PRIMARY KEY, case_id TEXT, draft TEXT, hash TEXT, status TEXT, reviewer TEXT);
CREATE TABLE IF NOT EXISTS records(
  id TEXT PRIMARY KEY, case_id TEXT UNIQUE, draft TEXT, review_id TEXT, dataset_version TEXT);
CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY, hash TEXT, response TEXT);
CREATE TABLE IF NOT EXISTS calls(
  case_id TEXT, tool TEXT, n INTEGER, PRIMARY KEY(case_id, tool));
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, event TEXT, case_id TEXT, detail TEXT);
"""


class SqliteStore(_Store):
    dialect = "sqlite"

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def ensure_schema(self):
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            connection.executescript(_SQLITE_SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
            if "dataset_version" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN dataset_version TEXT")
            connection.commit()
        finally:
            connection.close()

    def transaction(self):
        return _SqliteTransaction(self)

    def upsert_call(self, tx, case_id, tool, count):
        tx.execute(
            "INSERT OR REPLACE INTO calls(case_id, tool, n) VALUES(?,?,?)",
            (case_id, tool, count),
        )


class _SqliteTransaction:
    def __init__(self, store):
        self.store = store
        self.connection = sqlite3.connect(store.path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.tx = _Transaction(store, self.connection)
        self.connection.execute("BEGIN IMMEDIATE")

    def __enter__(self):
        return self.tx

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False


class PostgresStore(_Store):
    dialect = "postgres"

    def __init__(self, url):
        self.url = url

    def adapt(self, sql):
        return sql.replace("?", "%s")

    def transaction(self):
        return _PostgresTransaction(self)

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.url, row_factory=dict_row)

    def lock_call(self, tx, case_id, tool):
        tx.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"call:{case_id}:{tool}",))

    def lock_record(self, tx, key, logical_key):
        tx.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"key:{key}",))
        tx.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"record:{logical_key}",))

    def upsert_call(self, tx, case_id, tool, count):
        tx.execute(
            """
            INSERT INTO calls(case_id, tool, n) VALUES(?,?,?)
            ON CONFLICT(case_id, tool) DO UPDATE SET n = EXCLUDED.n
            """,
            (case_id, tool, count),
        )


class _PostgresTransaction:
    def __init__(self, store):
        self.connection = store.connect()
        self.tx = _Transaction(store, self.connection)

    def __enter__(self):
        return self.tx

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False
