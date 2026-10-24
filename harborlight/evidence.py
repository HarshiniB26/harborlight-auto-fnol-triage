"""Load local evidence files and extract labeled facts. Text is untrusted data."""

import json
import re
from pathlib import Path

_FIELD = re.compile(r"^(?P<label>[A-Za-z][A-Za-z ]+):\s*(?P<value>.+?)\s*$", re.MULTILINE)
_INJECTION = re.compile(
    r"(?i)(ignore (any |all |your )?previous|you are now|system\s*:|print all private|disregard)",
)

LABELS = {
    "incident timestamp": "incident_timestamp",
    "policy number": "policy_number",
    "vehicle id": "vehicle_id",
    "vehicle reference": "vehicle_id",
    "vehicle": "vehicle_id",
    "loss type": "loss_type",
    "reported loss type": "loss_type",
    "driver name": "driver_name",
    "incident location": "incident_location",
    "injury reported": "injury_reported",
    "vehicle drivable": "vehicle_drivable",
    "referral indicator": "referral_indicator",
    "loss reference": "loss_reference",
    "estimate reference": "estimate_reference",
    "repair amount": "repair_amount",
}

MISSING_MARKERS = {"", "not supplied", "unknown", "n/a", "none provided"}


def load_text(data_dir: Path, source_id: str) -> str | None:
    for folder in ("documents", "policies"):
        path = data_dir / folder / f"{source_id}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def extract_fields(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _FIELD.finditer(text):
        key = LABELS.get(match.group("label").strip().lower())
        if key and key not in found:
            found[key] = match.group("value").strip()
    return found


def is_missing(value: str | None) -> bool:
    return value is None or value.strip().lower() in MISSING_MARKERS


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    token = value.strip().lower()
    if token in {"yes", "true", "y"}:
        return True
    if token in {"no", "false", "n"}:
        return False
    return None


def injection_flags(source_id: str, text: str) -> list[str]:
    return [source_id] if _INJECTION.search(text) else []


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def excerpt(text: str, needle: str) -> str | None:
    if needle and needle in text:
        return needle
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 12 and not line.startswith("#") and "private" not in line.lower():
            return line
    return None
