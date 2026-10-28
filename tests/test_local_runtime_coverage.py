import io
import json
import runpy
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools import runtime


def call(base, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(base + path, data=data, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


class LocalRuntimeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.data = root / "data"
        self.data.mkdir()
        self.state = root / "state.sqlite"
        self.dataset = {
            "name": "local", "allow_write": True,
            "cases": [
                {"case_id": "A", "intake_source": "A-I", "documents": ["A-I"], "logical_key": "loss", "tools": {"policy": {"ok": True}}, "faults": {"policy": {"failures": 1}}},
                {"case_id": "B", "intake_source": "B-I", "documents": ["B-I"], "logical_key": "loss", "tools": {}, "faults": {}},
            ],
        }
        (self.data / "dataset.json").write_text(json.dumps(self.dataset))
        self.server = runtime.create_server(self.data, self.state, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_get_routes_fault_and_missing_states(self):
        self.assertEqual(call(self.base, "/health")[0], 200)
        self.assertEqual(len(call(self.base, "/v1/cases")[1]["cases"]), 2)
        self.assertEqual(call(self.base, "/v1/reviews/no")[0], 404)
        self.assertEqual(call(self.base, "/v1/records/no")[0], 404)
        self.assertEqual(call(self.base, "/v1/records/A")[0], 404)
        self.assertEqual(call(self.base, "/other")[0], 404)
        self.assertEqual(call(self.base, "/v1/cases/no/policy")[0], 404)
        self.assertEqual(call(self.base, "/v1/cases/A/no")[0], 404)
        self.assertEqual(call(self.base, "/v1/cases/A/policy")[0], 503)
        self.assertEqual(call(self.base, "/v1/cases/A/policy")[0], 200)

    def test_review_creation_replay_and_decisions(self):
        empty = Request(self.base + "/v1/reviews", data=b"", method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(empty)
        self.assertEqual(caught.exception.code, 413)
        for raw in (b"not-json", b"[]"):
            request = Request(self.base + "/v1/reviews", data=raw, method="POST")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(call(self.base, "/v1/reviews", {"case_id": "no"})[0], 400)
        self.assertEqual(call(self.base, "/v1/reviews", {"case_id": "A", "draft": []})[0], 400)
        draft = {"case_id": "A", "value": 1}
        status, review = call(self.base, "/v1/reviews", {"case_id": "A", "draft": draft})
        self.assertEqual(status, 201)
        self.assertEqual(call(self.base, "/v1/reviews", {"case_id": "A", "draft": draft})[0], 200)
        review_id = review["review_id"]
        self.assertEqual(call(self.base, f"/v1/reviews/{review_id}")[0], 200)
        path = f"/v1/reviews/{review_id}/decision"
        self.assertEqual(call(self.base, path, {"case_id": "A", "decision": "x", "reviewer": ""})[0], 400)
        self.assertEqual(call(self.base, "/v1/reviews/no/decision", {"case_id": "A", "decision": "approved", "reviewer": "N"})[0], 404)
        self.assertEqual(call(self.base, path, {"case_id": "B", "decision": "approved", "reviewer": "N"})[0], 409)
        self.assertEqual(call(self.base, path, {"case_id": "A", "decision": "approved", "reviewer": " N "})[0], 200)
        self.assertEqual(call(self.base, path, {"case_id": "A", "decision": "rejected", "reviewer": "N"})[0], 409)

    def test_record_write_guards_idempotency_and_duplicates(self):
        draft = {"case_id": "A", "value": 1}
        _, review = call(self.base, "/v1/reviews", {"case_id": "A", "draft": draft})
        review_id = review["review_id"]
        self.assertEqual(call(self.base, "/unsupported", {"case_id": "A"})[0], 404)
        self.assertEqual(call(self.base, "/v1/records", {"case_id": "A"})[0], 400)
        mismatch = {"case_id": "A", "draft": {"case_id": "B"}, "review_id": review_id}
        self.assertEqual(call(self.base, "/v1/records", mismatch, {"Idempotency-Key": "k"})[0], 400)
        body = {"case_id": "A", "draft": draft, "review_id": review_id}
        self.assertEqual(call(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 403)
        call(self.base, f"/v1/reviews/{review_id}/decision", {"case_id": "A", "decision": "approved", "reviewer": "N"})
        self.assertEqual(call(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 201)
        self.assertEqual(call(self.base, "/v1/records/A")[0], 200)
        self.assertEqual(call(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 200)
        changed = {"case_id": "A", "draft": {"case_id": "A", "value": 2}, "review_id": review_id}
        self.assertEqual(call(self.base, "/v1/records", changed, {"Idempotency-Key": "k"})[0], 409)

        draft_b = {"case_id": "B"}
        _, review_b = call(self.base, "/v1/reviews", {"case_id": "B", "draft": draft_b})
        call(self.base, f"/v1/reviews/{review_b['review_id']}/decision", {"case_id": "B", "decision": "approved", "reviewer": "N"})
        body_b = {"case_id": "B", "draft": draft_b, "review_id": review_b["review_id"]}
        self.assertEqual(call(self.base, "/v1/records", body_b, {"Idempotency-Key": "b"})[0], 409)

    def test_read_only_write(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.dataset["allow_write"] = False
        (self.data / "dataset.json").write_text(json.dumps(self.dataset))
        self.server = runtime.create_server(self.data, self.state, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        body = {"case_id": "A", "draft": {"case_id": "A"}, "review_id": "R"}
        self.assertEqual(call(self.base, "/v1/records", body, {"Idempotency-Key": "k"})[0], 403)

    def test_cli_commands_serve_and_main_guard(self):
        with runtime.connect(self.state) as database:
            database.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?)", ("R", "A", "{}", "h", "pending", None))
            runtime.audit(database, "seed", "A")
        commands = [
            ["runtime", "pending", "--state", str(self.state)],
            ["runtime", "audit", "--state", str(self.state)],
            ["runtime", "approve", "R", "--reviewer", "N", "--state", str(self.state)],
            ["runtime", "reset", "--confirm", "--state", str(self.state)],
        ]
        for argv in commands:
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                runtime.main()
        with patch.object(sys, "argv", ["runtime", "reject", "missing", "--reviewer", "N", "--state", str(self.state)]), self.assertRaises(SystemExit):
            runtime.main()
        fake_server = MagicMock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(sys, "argv", ["runtime", "serve", "--data", str(self.data), "--state", str(self.state), "--host", "0.0.0.0", "--port", "99"]), patch.object(runtime, "create_server", return_value=fake_server), redirect_stdout(io.StringIO()):
            runtime.main()
        fake_server.server_close.assert_called_once()

        with patch.object(sys, "argv", ["runtime", "pending", "--state", str(self.state)]), redirect_stdout(io.StringIO()):
            runpy.run_module("tools.runtime", run_name="__main__")


if __name__ == "__main__":
    unittest.main()
