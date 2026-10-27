"""Dataset loading, caching, and refresh behavior. No AWS account is required."""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tools.dataset_loader import (
    DatasetAccessError,
    DatasetCache,
    DatasetInvalid,
    DatasetNotFound,
    load_local,
    load_s3,
)
from tools.runtime import create_server

ROOT = Path(__file__).resolve().parents[1]


def _document(name="demo", source_text="hello\n"):
    return {
        "name": name,
        "allow_write": True,
        "cases": [{
            "case_id": "C1",
            "logical_key": "loss-1",
            "tools": {"policy": {"source_id": "C1-POLICY"}},
        }],
        "sources": {"DOC": source_text},
    }


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = 0
        self.error = None

    def get_object(self, Bucket, Key):
        self.calls += 1
        if self.error is not None:
            raise self.error
        found = self.objects.get((Bucket, Key))
        if found is None:
            error = Exception("missing")
            error.response = {
                "Error": {"Code": "NoSuchKey"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
            raise error
        raw, etag, version = found
        return {"Body": _Body(raw), "ETag": etag, "VersionId": version}


class DatasetLoaderTests(unittest.TestCase):
    def test_local_development_dataset_matches_evidence_files(self):
        loaded = load_local(ROOT / "data" / "dev" / "dataset.json")
        raw = (ROOT / "data" / "dev" / "dataset.json").read_bytes()
        self.assertEqual(loaded.name, "auto-fnol-dev")
        self.assertEqual(loaded.version, hashlib.sha256(raw).hexdigest())
        self.assertEqual(loaded.source, "local")

    def test_missing_and_malformed_local_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "dataset.json"
            with self.assertRaises(DatasetNotFound):
                load_local(missing)
            missing.write_text("{")
            with self.assertRaises(DatasetInvalid):
                load_local(missing)

    def test_s3_load_missing_object_malformed_json_and_api_error(self):
        client = FakeS3()
        with self.assertRaises(DatasetNotFound):
            load_s3("bucket", "dataset.json", client)
        client.objects[("bucket", "dataset.json")] = (b"{", '"etag"', "version-1")
        with self.assertRaises(DatasetInvalid):
            load_s3("bucket", "dataset.json", client)
        client.error = Exception("denied")
        client.error.response = {
            "Error": {"Code": "AccessDenied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }
        with self.assertRaises(DatasetAccessError):
            load_s3("bucket", "dataset.json", client)

    def test_cache_serves_memory_until_explicit_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "documents" / "DOC.md").write_text("hello\n")
            first = json.dumps(_document("first")).encode()
            second = json.dumps(_document("second")).encode()
            client = FakeS3()
            client.objects[("bucket", "dataset.json")] = (first, '"v1"', "version-1")
            cache = DatasetCache(root, bucket="bucket", key="dataset.json", client=client)
            self.assertEqual(cache.get().name, "first")
            self.assertEqual(cache.get().version, "version-1")
            client.objects[("bucket", "dataset.json")] = (second, '"v2"', "version-2")
            self.assertEqual(cache.get().name, "first")
            self.assertEqual(client.calls, 1)
            self.assertEqual(cache.refresh().name, "second")
            self.assertEqual(client.calls, 2)

    def test_failed_refresh_keeps_the_previous_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "documents" / "DOC.md").write_text("hello\n")
            client = FakeS3()
            client.objects[("bucket", "dataset.json")] = (
                json.dumps(_document("first")).encode(),
                '"v1"',
                "version-1",
            )
            cache = DatasetCache(root, bucket="bucket", key="dataset.json", client=client)
            cache.get()
            client.objects[("bucket", "dataset.json")] = (b"{", '"bad"', "version-bad")
            with self.assertRaises(DatasetInvalid):
                cache.refresh()
            self.assertEqual(cache.get().name, "first")

    def test_local_source_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "documents").mkdir()
            (root / "documents" / "DOC.md").write_text("different\n")
            (root / "dataset.json").write_text(json.dumps(_document()))
            cache = DatasetCache(root)
            with self.assertRaises(DatasetInvalid):
                cache.get()

    def test_runtime_refresh_is_loopback_and_reports_the_version(self):
        from urllib.request import Request, urlopen

        with tempfile.TemporaryDirectory() as temporary:
            server = create_server(ROOT / "data" / "dev", Path(temporary) / "mock.sqlite", port=0)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 2)
            base = f"http://127.0.0.1:{server.server_port}"
            health = json.loads(urlopen(base + "/health", timeout=3).read().decode())
            self.assertEqual(health["dataset"], "auto-fnol-dev")
            self.assertTrue(health["dataset_version"])
            request = Request(base + "/internal/dataset/refresh", data=b"{}", method="POST")
            with urlopen(request, timeout=3) as response:
                body = json.loads(response.read().decode())
                self.assertEqual(response.status, 200)
            self.assertEqual(body["status"], "refreshed")
            self.assertEqual(body["dataset_version"], health["dataset_version"])


if __name__ == "__main__":
    unittest.main()
