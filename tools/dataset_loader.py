"""Load the FNOL dataset from a local file or from S3.

The parsed document is cached on the loader. A later S3 upload is invisible
until ``refresh`` or a process restart. Callers pass an S3 client in tests so
those tests do not need AWS credentials.
"""

import hashlib
import json
import os
from pathlib import Path


class DatasetError(Exception):
    """Base error for dataset loading."""


class DatasetNotFound(DatasetError):
    """The configured dataset object or file does not exist."""


class DatasetInvalid(DatasetError):
    """The dataset JSON is missing, malformed, or inconsistent with local evidence."""


class DatasetAccessError(DatasetError):
    """S3 denied the request or the API call failed."""


class LoadedDataset:
    """A validated dataset plus the version recorded on new records."""

    def __init__(self, dataset, version, source):
        self.dataset = dataset
        self.version = version
        self.source = source

    @property
    def name(self):
        return self.dataset["name"]


def validate_dataset(dataset):
    """Reject a document that does not have the fixture's required shape."""

    if not isinstance(dataset, dict):
        raise DatasetInvalid("Dataset JSON must be an object.")
    if not isinstance(dataset.get("name"), str) or not dataset["name"].strip():
        raise DatasetInvalid("Dataset name is required.")
    if not isinstance(dataset.get("allow_write"), bool):
        raise DatasetInvalid("Dataset allow_write must be a boolean.")
    cases = dataset.get("cases")
    sources = dataset.get("sources")
    if not isinstance(cases, list):
        raise DatasetInvalid("Dataset cases must be a list.")
    if not isinstance(sources, dict):
        raise DatasetInvalid("Dataset sources must be an object.")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise DatasetInvalid("Each case must be an object.")
        for field in ("case_id", "logical_key", "tools"):
            if field not in case:
                raise DatasetInvalid(f"A case is missing {field}.")
        if not isinstance(case["case_id"], str) or not isinstance(case["logical_key"], str):
            raise DatasetInvalid("case_id and logical_key must be strings.")
        if not isinstance(case["tools"], dict):
            raise DatasetInvalid("Case tools must be an object.")
        if case["case_id"] in seen:
            raise DatasetInvalid(f"Duplicate case_id {case['case_id']}.")
        seen.add(case["case_id"])
    return dataset


def verify_local_sources(dataset, data_dir):
    """Fail when a local evidence file differs from the loaded dataset source."""

    sources = dataset["sources"]
    root = Path(data_dir)
    for folder in ("documents", "policies"):
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            source_id = path.stem
            if source_id not in sources:
                continue
            actual = path.read_text()
            if actual != sources[source_id]:
                raise DatasetInvalid(
                    f"Local file {folder}/{path.name} does not match dataset source {source_id}."
                )


def load_local(path):
    """Load and validate a dataset file. The version is the file's SHA-256."""

    file_path = Path(path)
    if not file_path.is_file():
        raise DatasetNotFound(f"Dataset file {file_path} does not exist.")
    raw = file_path.read_bytes()
    try:
        dataset = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DatasetInvalid("Dataset file is not valid JSON.") from error
    version = hashlib.sha256(raw).hexdigest()
    return LoadedDataset(validate_dataset(dataset), version, "local")


def _missing_object(error):
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "404", "NotFound", "NoSuchBucket"} or status == 404


def load_s3(bucket, key, client):
    """Read one object with GetObject and validate the JSON body."""

    if client is None:
        try:
            import boto3
        except ImportError as error:
            raise DatasetAccessError("boto3 is required to read the dataset from S3.") from error
        client = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if _missing_object(error):
            raise DatasetNotFound(f"s3://{bucket}/{key} was not found.") from error
        if getattr(error, "response", None) or error.__class__.__name__ in {"ClientError", "BotoCoreError", "EndpointConnectionError"}:
            raise DatasetAccessError("The dataset could not be read from S3.") from error
        raise
    body = response["Body"]
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, str):
        raw = raw.encode()
    try:
        dataset = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DatasetInvalid("Dataset object is not valid JSON.") from error
    version = response.get("VersionId") or str(response.get("ETag", "")).strip('"')
    if not version:
        version = hashlib.sha256(raw).hexdigest()
    return LoadedDataset(validate_dataset(dataset), str(version), "s3")


class DatasetCache:
    """Hold one validated dataset until an explicit refresh."""

    def __init__(self, data_dir, bucket=None, key="dataset.json", client=None):
        self.data_dir = Path(data_dir)
        self.bucket = bucket or None
        self.key = key or "dataset.json"
        self.client = client
        self.loaded = None

    @classmethod
    def from_env(cls, data_dir, client=None):
        return cls(
            data_dir,
            bucket=os.environ.get("FNOL_DATASET_BUCKET", "").strip() or None,
            key=os.environ.get("FNOL_DATASET_KEY", "").strip() or "dataset.json",
            client=client,
        )

    def get(self):
        """Return the cached dataset, loading it on first use."""

        if self.loaded is None:
            self.loaded = self._fetch()
        return self.loaded

    def refresh(self):
        """Replace the cache only after a new document validates.

        A failed refresh leaves the previous document in place and raises.
        """

        loaded = self._fetch()
        self.loaded = loaded
        return loaded

    def _fetch(self):
        if self.bucket:
            loaded = load_s3(self.bucket, self.key, self.client)
        else:
            loaded = load_local(self.data_dir / "dataset.json")
        verify_local_sources(loaded.dataset, self.data_dir)
        return loaded
