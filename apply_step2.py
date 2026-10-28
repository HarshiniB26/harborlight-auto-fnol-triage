
from pathlib import Path

runtime = Path("tools/runtime.py")
service = Path("workbench/service.py")
test = Path("tests/test_review_idempotency.py")

r = runtime.read_text()

old = '''                rid = "rev_" + uuid.uuid4().hex[:12]
                with connect(state) as db:
                    db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?)",
                               (rid, cid, canonical(draft), digest(draft), "pending", None))
                    audit(db, "review_requested", cid, review_id=rid, draft_hash=digest(draft))
                return self.reply(201, {"review_id": rid, "status": "pending", "draft_hash": digest(draft)})'''

new = '''                draft_hash = digest(draft)
                with connect(state) as db:
                    # Serialize review creation across HTTP threads.
                    db.execute("BEGIN IMMEDIATE")

                    previous = db.execute(
                        "SELECT id, status FROM reviews "
                        "WHERE case_id=? AND hash=? ORDER BY rowid LIMIT 1",
                        (cid, draft_hash),
                    ).fetchone()

                    if previous:
                        audit(
                            db,
                            "review_replayed",
                            cid,
                            review_id=previous["id"],
                            draft_hash=draft_hash,
                        )
                        result = {
                            "review_id": previous["id"],
                            "status": previous["status"],
                            "draft_hash": draft_hash,
                        }
                        code = 200
                    else:
                        rid = "rev_" + uuid.uuid4().hex[:12]

                        db.execute(
                            "INSERT INTO reviews VALUES(?,?,?,?,?,?)",
                            (
                                rid,
                                cid,
                                canonical(draft),
                                draft_hash,
                                "pending",
                                None,
                            ),
                        )

                        audit(
                            db,
                            "review_requested",
                            cid,
                            review_id=rid,
                            draft_hash=draft_hash,
                        )

                        result = {
                            "review_id": rid,
                            "status": "pending",
                            "draft_hash": draft_hash,
                        }
                        code = 201

                return self.reply(code, result)'''

if r.count(old) != 1:
    raise SystemExit(
        "STOP: Runtime review block does not match expected version. "
        "No files changed."
    )

s = service.read_text()

old_s = 'if status != 201 or not isinstance(payload, dict):'

if s.count(old_s) != 1:
    raise SystemExit(
        "STOP: Workbench review response check does not match. "
        "No files changed."
    )

if test.exists():
    raise SystemExit(
        "STOP: tests/test_review_idempotency.py already exists. "
        "No files changed."
    )

runtime.write_text(r.replace(old, new, 1))

service.write_text(
    s.replace(
        old_s,
        'if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("review_id"):',
        1,
    )
)

test.write_text('''"""Review submission must be idempotent even when the first response is lost."""

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
''')

print(
    "Step 2 applied: Runtime atomic review replay "
    "+ Workbench HTTP 200 handling + 4 tests."
)
