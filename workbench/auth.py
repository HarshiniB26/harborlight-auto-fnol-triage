"""Optional handler sessions for the deployed workbench.

Authentication is off unless FNOL_AUTH_REQUIRED is true. IAM is not involved.
No role, including reviewer, can approve a draft through this process.
"""

import hmac
import json
import os
import secrets
import threading
import time

from tools.aws_config import auth_required, load_auth_document

_LOCK = threading.Lock()
_SESSIONS = {}
_SESSION_TTL = 12 * 60 * 60
COOKIE = "fnol_session"


class AuthConfigurationError(RuntimeError):
    """Auth is required and the user directory cannot be used."""


def assert_configured():
    """Fail closed before serving when required credentials are missing."""

    if not auth_required():
        return
    try:
        users = _users()
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        raise AuthConfigurationError(
            "Authentication is required but the user directory is not usable."
        ) from error
    if not users:
        raise AuthConfigurationError("Authentication is required but no users are configured.")


def clear_sessions():
    """Drop in-memory sessions. Tests use this between cases."""

    with _LOCK:
        _SESSIONS.clear()


def login(username, password):
    """Return a session token, or None when the credentials do not match."""

    if not isinstance(username, str) or not isinstance(password, str):
        return None
    user = _users().get(username)
    if not user or not hmac.compare_digest(user["password"], password):
        return None
    if user["role"] not in {"handler", "reviewer"}:
        return None
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {
            "username": username,
            "role": user["role"],
            "expires": time.time() + _SESSION_TTL,
        }
    return token


def session_from_cookie(header):
    """Return the session for a Cookie header, ignoring expired tokens."""

    token = _cookie_value(header, COOKIE)
    if not token:
        return None
    with _LOCK:
        session = _SESSIONS.get(token)
        if not session:
            return None
        if session["expires"] <= time.time():
            _SESSIONS.pop(token, None)
            return None
        return {"username": session["username"], "role": session["role"]}


def public_session(header):
    """Return the session payload the browser uses to decide whether to show sign-in."""

    if not auth_required():
        return {"auth_required": False, "authenticated": False}
    try:
        assert_configured()
    except (AuthConfigurationError, RuntimeError, json.JSONDecodeError):
        return {"auth_required": True, "authenticated": False, "error": "auth_unavailable"}
    session = session_from_cookie(header)
    if not session:
        return {"auth_required": True, "authenticated": False}
    return {
        "auth_required": True,
        "authenticated": True,
        "username": session["username"],
        "role": session["role"],
    }


def cookie_header(token):
    """Build a Set-Cookie value. Secure is added only when explicitly requested."""

    flags = "HttpOnly; SameSite=Lax; Path=/"
    if os.environ.get("FNOL_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}:
        flags += "; Secure"
    return f"{COOKIE}={token}; {flags}"


def clear_cookie_header():
    flags = "HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
    return f"{COOKIE}=; {flags}"


def _users():
    document = load_auth_document()
    users = {}
    for entry in document.get("users", []):
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        password = entry.get("password")
        role = entry.get("role")
        if isinstance(username, str) and isinstance(password, str) and isinstance(role, str):
            users[username] = {"password": password, "role": role}
    return users


def _cookie_value(header, name):
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        prefix = name + "="
        if part.startswith(prefix):
            return part[len(prefix):]
    return None
