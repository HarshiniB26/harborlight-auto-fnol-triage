"""Local workbench HTTP behavior."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.runtime import create_server
from workbench.server import create_server as create_workbench
from workbench.service import Workbench

ROOT = Path(__file__).resolve().parents[1]


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.mock = create_server(ROOT / "data/dev", directory / "mock.sqlite", port=0)
        self.mock_thread = threading.Thread(target=self.mock.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.mock_thread.start()
        workbench = Workbench(
            api_url=f"http://127.0.0.1:{self.mock.server_port}",
            data_dir=ROOT / "data/dev",
            state_path=directory / "workbench.json",
        )
        self.server = create_workbench(workbench, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.mock.shutdown()
        self.mock.server_close()
        self.thread.join(timeout=2)
        self.mock_thread.join(timeout=2)
        self.temporary.cleanup()

    def test_queue_lists_ids_and_case_page_prepares_a_draft(self):
        page = urlopen(self.base + "/", timeout=3).read().decode()
        self.assertIn("FNOL queue", page)
        self.assertIn("Export results.json", page)
        cases = self._json("GET", "/api/cases")["cases"]
        self.assertIn("AUTO-D01", [item["case_id"] for item in cases])
        self.assertNotIn("routing", cases[0])
        prepared = self._json("POST", "/api/cases/AUTO-D01/process")
        self.assertEqual(prepared["draft"]["routing"], "standard")
        self.assertEqual(prepared["draft"]["classification"], "collision")
        blocked = self._json("POST", "/api/cases/AUTO-D01/record")
        self.assertEqual(blocked["error"], "approval_required")
        case = self._json("GET", "/api/cases/AUTO-D01")
        self.assertEqual(case["activity"][0]["label"], "Draft prepared")
        incomplete = self._json("GET", "/api/results")
        self.assertEqual(incomplete["error"], "drafts_incomplete")

    def test_health_readiness_metrics_and_trace_header(self):
        health = self._json("GET", "/health")
        self.assertEqual(health["status"], "ok")
        ready = self._json("GET", "/ready")
        self.assertEqual(ready["status"], "ready")
        metrics = self._json("GET", "/metrics")
        self.assertIn("slo", metrics)

        request = Request(
            self.base + "/api/cases/AUTO-D01/process",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Trace-ID": "tr_http_test",
            },
        )
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode())
            self.assertEqual(response.headers["X-Trace-ID"], "tr_http_test")
        self.assertEqual(body["trace_id"], "tr_http_test")

    def _json(self, method, path):
        request = Request(self.base + path, data=b"{}" if method == "POST" else None, method=method,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode())
        except HTTPError as error:
            return json.loads(error.read().decode())


if __name__ == "__main__":
    unittest.main()
