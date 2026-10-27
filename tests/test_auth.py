"""Optional handler sessions. Auth stays off unless the environment asks for it."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.runtime import _reviewer_is_allowed, create_server
from workbench.auth import (
    AuthConfigurationError,
    assert_configured,
    clear_cookie_header,
    clear_sessions,
    cookie_header,
    login,
    session_from_cookie,
)
from workbench.server import create_server as create_workbench
from workbench.service import Workbench

ROOT = Path(__file__).resolve().parents[1]
AUTH_KEYS = ("FNOL_AUTH_REQUIRED", "FNOL_AUTH_USERS_JSON", "FNOL_COOKIE_SECURE")
USERS = {
    "users": [
        {"username": "handler", "password": "handler-pass", "role": "handler"},
        {"username": "reviewer", "password": "reviewer-pass", "role": "reviewer"},
        {"username": "other", "password": "other-pass", "role": "admin"},
    ]
}


class AuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in AUTH_KEYS}
        clear_sessions()

    def tearDown(self):
        clear_sessions()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_auth_off_does_not_require_users(self):
        os.environ["FNOL_AUTH_REQUIRED"] = "false"
        os.environ.pop("FNOL_AUTH_USERS_JSON", None)
        assert_configured()
        self.assertTrue(_reviewer_is_allowed("anyone"))

    def test_required_auth_fails_closed_and_limits_reviewers(self):
        os.environ["FNOL_AUTH_REQUIRED"] = "true"
        os.environ["FNOL_AUTH_USERS_JSON"] = "{}"
        with self.assertRaises(AuthConfigurationError):
            assert_configured()
        os.environ["FNOL_AUTH_USERS_JSON"] = json.dumps(USERS)
        assert_configured()
        self.assertIsNone(login("handler", "wrong-pass"))
        self.assertIsNone(login("other", "other-pass"))
        self.assertIsNone(login(None, "handler-pass"))
        self.assertIsNone(session_from_cookie(None))
        self.assertIsNone(session_from_cookie("other=1"))
        self.assertIsNone(session_from_cookie("fnol_session=missing"))
        self.assertIn("Max-Age=0", clear_cookie_header())
        os.environ["FNOL_AUTH_USERS_JSON"] = json.dumps({"users": []})
        with self.assertRaises(AuthConfigurationError):
            assert_configured()
        os.environ["FNOL_AUTH_USERS_JSON"] = json.dumps({
            "users": ["skip-me", {"username": "handler", "password": "handler-pass", "role": "handler"}],
        })
        token = login("handler", "handler-pass")
        self.assertIsNotNone(session_from_cookie(f"other=1; fnol_session={token}"))
        os.environ["FNOL_AUTH_USERS_JSON"] = json.dumps(USERS)
        os.environ["FNOL_COOKIE_SECURE"] = "true"
        self.assertIn("Secure", cookie_header(token))
        self.assertTrue(_reviewer_is_allowed("reviewer"))
        with self.assertRaises(SystemExit):
            _reviewer_is_allowed("handler")

    def test_expired_session_is_ignored(self):
        os.environ["FNOL_AUTH_REQUIRED"] = "true"
        os.environ["FNOL_AUTH_USERS_JSON"] = json.dumps(USERS)
        token = login("handler", "handler-pass")
        from workbench import auth

        auth._SESSIONS[token]["expires"] = 0
        self.assertIsNone(session_from_cookie(f"fnol_session={token}"))


class AuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in AUTH_KEYS}
        clear_sessions()
        os.environ["FNOL_AUTH_REQUIRED"] = "true"
        os.environ["FNOL_AUTH_USERS_JSON"] = json.dumps(USERS)
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.runtime = create_server(ROOT / "data" / "dev", directory / "mock.sqlite", port=0)
        self.runtime_thread = threading.Thread(
            target=self.runtime.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.runtime_thread.start()
        self.workbench = Workbench(
            api_url=f"http://127.0.0.1:{self.runtime.server_port}",
            data_dir=ROOT / "data" / "dev",
            state_path=directory / "workbench.json",
        )
        self.server = create_workbench(self.workbench, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.shutdown()
        self.runtime.server_close()
        self.thread.join(timeout=2)
        self.runtime_thread.join(timeout=2)
        self.temporary.cleanup()
        clear_sessions()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_handlers_must_sign_in_and_still_cannot_approve(self):
        self.assertFalse(hasattr(self.workbench, "approve"))
        health = self._json("GET", "/health")
        self.assertEqual(health["status"], "ok")
        ready = self._json("GET", "/ready")
        self.assertEqual(ready["status"], "ready")
        denied = self._json("GET", "/api/cases")
        self.assertEqual(denied["error"], "unauthorized")
        failed = self._json("POST", "/api/login", {"username": "handler", "password": "nope"})
        self.assertEqual(failed["error"], "unauthorized")
        _body, cookie = self._login("handler", "handler-pass")
        cases = self._json("GET", "/api/cases", cookie=cookie)
        self.assertIn("AUTO-D01", [item["case_id"] for item in cases["cases"]])
        missing = self._json("POST", "/api/cases/AUTO-D01/approve", cookie=cookie)
        self.assertEqual(missing["error"], "not_found")
        self.assertEqual(self._json("GET", "/api/results")["error"], "unauthorized")
        self.assertEqual(self._json("GET", "/api/cases/AUTO-D01")["error"], "unauthorized")
        self.assertEqual(self._json("POST", "/api/cases/AUTO-D01/process")["error"], "unauthorized")
        self.assertFalse(self._json("POST", "/api/logout")["authenticated"])
        self.assertEqual(self._raw("POST", "/api/login", b"not-json")["error"], "invalid_json")
        self.assertEqual(self._raw("POST", "/api/login", b"[]")["error"], "invalid_json")
        os.environ["FNOL_AUTH_USERS_JSON"] = "{}"
        unavailable = self._json("GET", "/api/cases", cookie=cookie)
        self.assertEqual(unavailable["error"], "auth_unavailable")

    def _login(self, username, password):
        request = Request(
            self.base + "/api/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode())
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        return body, cookie

    def _raw(self, method, path, data):
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode())
        except HTTPError as error:
            return json.loads(error.read().decode())

    def _json(self, method, path, body=None, cookie=None):
        data = None
        if method == "POST":
            data = json.dumps({} if body is None else body).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        request = Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode())
        except HTTPError as error:
            return json.loads(error.read().decode())


class ReadinessTests(unittest.TestCase):
    def test_health_stays_up_when_the_runtime_is_down(self):
        saved = os.environ.get("FNOL_AUTH_REQUIRED")
        os.environ.pop("FNOL_AUTH_REQUIRED", None)
        try:
            self._assert_runtime_down()
        finally:
            if saved is None:
                os.environ.pop("FNOL_AUTH_REQUIRED", None)
            else:
                os.environ["FNOL_AUTH_REQUIRED"] = saved

    def _assert_runtime_down(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench = Workbench(
                api_url="http://127.0.0.1:9",
                data_dir=ROOT / "data" / "dev",
                state_path=Path(temporary) / "workbench.json",
            )
            server = create_workbench(workbench, port=0)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 2)
            base = f"http://127.0.0.1:{server.server_port}"
            health = json.loads(urlopen(base + "/health", timeout=3).read().decode())
            self.assertEqual(health["status"], "ok")
            request = Request(base + "/ready", method="GET")
            try:
                with urlopen(request, timeout=3) as response:
                    ready = json.loads(response.read().decode())
                    status = response.status
            except HTTPError as error:
                ready = json.loads(error.read().decode())
                status = error.code
            self.assertEqual(status, 503)
            self.assertEqual(ready["status"], "not_ready")
            session = json.loads(urlopen(base + "/api/session", timeout=3).read().decode())
            self.assertFalse(session["auth_required"])


if __name__ == "__main__":
    unittest.main()
