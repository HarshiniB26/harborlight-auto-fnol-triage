"""Review submission must be idempotent even when the first response is lost."""

import json
import sqlite3
import tempfile
import threading
import unittest

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

from tools.runtime import create_server

ROOT = Path(__file__).resolve().parents[1]


class ReviewIdempotencyTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "mock.sqlite"

        self.server = create_server(
            ROOT / "data/dev",
            self.state,
            port=0,
        )

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

        self.url = (
            f"http://127.0.0.1:{self.server.server_port}/v1/reviews"
        )

        self.draft = {
            "case_id": "AUTO-D01",
            "classification": "collision",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def submit(self, draft=None):
        body = json.dumps({
            "case_id": "AUTO-D01",
            "draft": draft or self.draft,
        }).encode()

        request = Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
        )

        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_repeat_submission_returns_original_review(self):
        first_code, first = self.submit()
        second_code, second = self.submit()

        self.assertEqual((first_code, second_code), (201, 200))
        self.assertEqual(first["review_id"], second["review_id"])

        with sqlite3.connect(self.state) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM reviews"
            ).fetchone()[0]

            self.assertEqual(count, 1)

    def test_concurrent_submissions_create_one_review(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(lambda _: self.submit(), range(6))
            )

        self.assertEqual(
            len({result[1]["review_id"] for result in results}),
            1,
        )

        with sqlite3.connect(self.state) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM reviews"
            ).fetchone()[0]

            self.assertEqual(count, 1)

    def test_decided_review_is_not_reopened(self):
        _, first = self.submit()

        with sqlite3.connect(self.state) as db:
            db.execute(
                "UPDATE reviews SET status='approved' WHERE id=?",
                (first["review_id"],),
            )

        code, repeated = self.submit()

        self.assertEqual(code, 200)
        self.assertEqual(
            repeated["review_id"],
            first["review_id"],
        )
        self.assertEqual(repeated["status"], "approved")

    def test_changed_draft_creates_distinct_review(self):
        _, first = self.submit()

        _, second = self.submit({
            **self.draft,
            "classification": "theft",
        })

        self.assertNotEqual(
            first["review_id"],
            second["review_id"],
        )
