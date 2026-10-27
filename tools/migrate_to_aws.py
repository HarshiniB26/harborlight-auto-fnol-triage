"""Create mutable RDS state and upload the supplied dataset unchanged to S3."""

import argparse
import json
import os
from pathlib import Path

import boto3
import psycopg
from psycopg import sql

from workbench.storage import database_url_from_environment

ROOT = Path(__file__).resolve().parents[1]


def _configure_application_role(connection, secret_arn):
    """Create/update the non-owner database login used by running containers."""

    if not secret_arn:
        return
    secret = json.loads(
        boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION"))
        .get_secret_value(SecretId=secret_arn)["SecretString"]
    )
    username = secret["username"]
    exists = connection.execute(
        "SELECT 1 FROM pg_roles WHERE rolname=%s", (username,)
    ).fetchone()
    if not exists:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(username)))
    connection.execute(
        sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(username)),
        (secret["password"],),
    )
    role = sql.Identifier(username)
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA fnol TO {}").format(role))
    connection.execute(
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA fnol TO {}")
        .format(role)
    )
    connection.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA fnol TO {}").format(role)
    )
    connection.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA fnol "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA fnol "
            "GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(role)
    )


def migrate(
    database_url,
    bucket,
    data_dir,
    prefix="",
    *,
    upload_evidence=True,
    application_secret_arn=None,
):
    """Create mutable tables and copy immutable supplied evidence to S3.

    ``dataset.json`` remains the source of truth for cases, tool responses, and
    deliberate faults. PostgreSQL stores only reviews, records, idempotency,
    counters, audit events, and generated workbench state.
    """

    dataset = json.loads((data_dir / "dataset.json").read_text())
    schema = (ROOT / "deployment" / "schema.sql").read_text()
    with psycopg.connect(database_url) as connection:
        connection.execute(schema)
        _configure_application_role(connection, application_secret_arn)

    uploaded = 0
    if upload_evidence:
        s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION"))
        dataset_key = "/".join(
            part for part in (prefix.strip("/"), "dataset.json") if part
        )
        s3.put_object(
            Bucket=bucket,
            Key=dataset_key,
            Body=(data_dir / "dataset.json").read_bytes(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        uploaded += 1
        for category in ("documents", "policies"):
            for path in sorted((data_dir / category).glob("*.md")):
                key = "/".join(
                    part for part in (prefix.strip("/"), category, path.name) if part
                )
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=path.read_bytes(),
                    ContentType="text/markdown; charset=utf-8",
                    ServerSideEncryption="AES256",
                )
                uploaded += 1
    return len(dataset["cases"]), uploaded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "dev")
    parser.add_argument("--bucket", default=os.getenv("EVIDENCE_BUCKET"))
    parser.add_argument("--prefix", default=os.getenv("EVIDENCE_PREFIX", ""))
    parser.add_argument("--skip-s3", action="store_true")
    args = parser.parse_args()
    if not args.bucket and not args.skip_s3:
        parser.error("--bucket or EVIDENCE_BUCKET is required")
    cases, objects = migrate(
        database_url_from_environment(),
        args.bucket,
        args.data,
        args.prefix,
        upload_evidence=not args.skip_s3,
        application_secret_arn=os.getenv("APP_DB_SECRET_ARN"),
    )
    suffix = f"; {objects} files in S3" if not args.skip_s3 else ""
    print(
        f"Mutable RDS schema ready; supplied dataset contains {cases} cases{suffix}."
    )


if __name__ == "__main__":
    main()
