"""Resolve AWS settings from the environment and Secrets Manager.

The module does not import boto3 until a secret must be read. Callers that
only need a local database URL never touch the network.
"""

import json
import os
from urllib.parse import quote, unquote, urlparse, urlunparse


def _flag(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def auth_required():
    """Return whether the workbench must authenticate handlers."""

    return _flag("FNOL_AUTH_REQUIRED")


def bedrock_enabled():
    """Return whether the optional model annotation is switched on."""

    return _flag("FNOL_BEDROCK_ENABLED")


def _secret_string(secret_id):
    import boto3

    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION") or None)
    response = client.get_secret_value(SecretId=secret_id)
    secret = response.get("SecretString")
    if not secret:
        raise RuntimeError("The configured secret has no string value.")
    return secret


def _rds_url_from_payload(payload):
    username = quote(str(payload["username"]), safe="")
    password = quote(str(payload["password"]), safe="")
    host = payload["host"]
    port = payload.get("port") or 5432
    database = payload.get("dbname") or payload.get("dbName") or "fnol"
    return f"postgresql://{username}:{password}@{host}:{port}/{database}"


def master_database_url():
    """Return the migration URL, or None when local SQLite should be used."""

    configured = os.environ.get("FNOL_DATABASE_URL", "").strip()
    if configured:
        return configured
    secret_id = os.environ.get("FNOL_DATABASE_SECRET_ARN", "").strip()
    if not secret_id:
        return None
    payload = json.loads(_secret_string(secret_id))
    if "host" in payload and "username" in payload and "password" in payload:
        return _rds_url_from_payload(payload)
    if "url" in payload:
        return str(payload["url"])
    raise RuntimeError("The database secret is missing host, username, or password.")


def application_database_url():
    """Return the URL the application should use for ordinary queries.

    When an application-role secret is configured, the host and database name
    still come from the master URL and the role password comes from that
    secret. The password is not logged.
    """

    master = master_database_url()
    app_secret = os.environ.get("FNOL_APP_SECRET_ARN", "").strip()
    if not app_secret:
        return master
    if not master:
        raise RuntimeError("The application database secret requires the master database secret or URL.")
    payload = json.loads(_secret_string(app_secret))
    username = str(payload["username"])
    password = str(payload["password"])
    return _replace_user(master, username, password)


def _replace_user(url, username, password):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    user = quote(username, safe="")
    secret = quote(password, safe="")
    return urlunparse(parsed._replace(netloc=f"{user}:{secret}@{host}"))


def load_auth_document():
    """Return the auth document, or an empty document when nothing is configured."""

    raw = os.environ.get("FNOL_AUTH_USERS_JSON", "").strip()
    if not raw:
        secret_id = os.environ.get("FNOL_AUTH_SECRET_ARN", "").strip()
        if not secret_id:
            return {"users": []}
        raw = _secret_string(secret_id).strip()
        if not raw:
            return {"users": []}
    document = json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("users"), list):
        raise RuntimeError("Auth configuration must be an object with a users list.")
    return document


def database_parts(url):
    """Return hostname, port, and database name from a PostgreSQL URL."""

    parsed = urlparse(url)
    name = unquote((parsed.path or "").lstrip("/")) or "fnol"
    return parsed.hostname, parsed.port or 5432, name
