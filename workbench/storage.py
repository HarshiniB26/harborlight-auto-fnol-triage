"""Storage adapters for local development and the AWS deployment.

The workflow depends on these small interfaces instead of knowing whether
evidence lives on disk or in S3 and whether workflow state lives in JSON or
PostgreSQL.  This keeps the deterministic rules identical in both modes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

SAFE_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
QUEUE_META_KEY = "fnol-queue-meta"


def database_url_from_secret(secret_arn, database=None):
    """Build a TLS PostgreSQL URL from one named Secrets Manager secret."""

    import boto3

    payload = boto3.client(
        "secretsmanager", region_name=os.getenv("AWS_REGION")
    ).get_secret_value(SecretId=secret_arn)
    secret = json.loads(payload["SecretString"])
    database = database or os.getenv("DB_NAME", secret.get("dbname", "fnol"))
    user = quote(secret["username"], safe="")
    password = quote(secret["password"], safe="")
    host = secret["host"]
    port = secret.get("port", 5432)
    return f"postgresql://{user}:{password}@{host}:{port}/{database}?sslmode=require"


def database_url_from_environment():
    """Resolve PostgreSQL connectivity from an explicit URL or AWS secret."""

    direct = os.getenv("DATABASE_URL")
    if direct:
        return direct
    secret_arn = os.getenv("DB_SECRET_ARN")
    if not secret_arn:
        raise ValueError("Set DATABASE_URL or DB_SECRET_ARN.")
    return database_url_from_secret(secret_arn)


class LocalEvidenceStore:
    """Read the supplied development evidence from the repository."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    def document(self, source_id):
        return self._read("documents", source_id)

    def policy(self, source_id):
        return self._read("policies", source_id)

    def _read(self, category, source_id):
        if not isinstance(source_id, str) or not SAFE_STORAGE_ID.fullmatch(source_id):
            return None
        path = self.data_dir / category / f"{source_id}.md"
        return path.read_text() if path.is_file() else None


class S3EvidenceStore:
    """Read private documents and policy wording from an S3 bucket."""

    def __init__(self, bucket, prefix="", client=None):
        if not bucket:
            raise ValueError("An S3 evidence bucket is required.")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def document(self, source_id):
        return self._get("documents", source_id)

    def policy(self, source_id):
        return self._get("policies", source_id)

    def _get(self, category, source_id):
        if not isinstance(source_id, str) or not SAFE_STORAGE_ID.fullmatch(source_id):
            return None
        key = "/".join(part for part in (self.prefix, category, f"{source_id}.md") if part)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            response_code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if response_code in {"NoSuchKey", "404"}:
                return None
            raise
        return response["Body"].read().decode("utf-8")


class JsonStateStore:
    """Persist local workflow state in one atomic JSON file."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.is_file():
            return {}
        stored = json.loads(self.path.read_text())
        if not isinstance(stored, dict):
            return {}
        return {
            case_id: entry
            for case_id, entry in stored.items()
            if case_id != "cases"
            and isinstance(entry, dict)
            and isinstance(entry.get("draft"), dict)
        }

    def write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(self.path)


class PostgresStateStore:
    """Persist shared workflow state in Amazon RDS PostgreSQL."""

    def __init__(self, database_url):
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgreSQL storage.")
        self.database_url = database_url

    def read(self):
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            rows = connection.execute(
                "SELECT case_id, entry FROM fnol.workbench_state"
            ).fetchall()
        return {case_id: entry for case_id, entry in rows}

    def write(self, state):
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            for case_id, entry in state.items():
                connection.execute(
                    """
                    INSERT INTO fnol.workbench_state(case_id, entry, updated_at)
                    VALUES (%s, %s::jsonb, CURRENT_TIMESTAMP)
                    ON CONFLICT (case_id) DO UPDATE
                    SET entry = EXCLUDED.entry, updated_at = EXCLUDED.updated_at
                    """,
                    (case_id, json.dumps(entry)),
                )

