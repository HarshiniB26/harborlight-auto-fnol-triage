"""Deterministic evidence extraction, policy selection, and routing rules.

This module is the executable translation of ``AUTO-TRIAGE-01.md``. It does
not perform I/O and it does not use an LLM, so the same evidence always
produces the same decision.
"""

import json
import re
from datetime import datetime, timezone


LABELS = {
    "policy number": "policy_number",
    "incident timestamp": "incident_timestamp",
    "incident time": "incident_timestamp",
    "vehicle id": "vehicle_id",
    "vehicle reference": "vehicle_id",
    "vehicle": "vehicle_id",
    "driver name": "driver_name",
    "incident location": "incident_location",
    "injury reported": "injury_reported",
    "vehicle drivable": "vehicle_drivable",
    "loss type": "loss_type",
    "reported loss type": "loss_type",
    "referral indicator": "referral_indicator",
}
UNKNOWN = {"", "not supplied", "unknown", "not provided", "n/a", "null", "none"}
REQUIRED_INTAKE_FIELDS = ("driver_name", "incident_location")
QUESTIONS = {
    "driver_name": "What is the full name of the person driving at the time of the incident?",
    "incident_location": "What was the precise location of the incident, including street and city?",
}
SUPPORTED_LOSSES = ("collision", "theft", "glass")
INSTRUCTION_PATTERN = re.compile(
    r"\b(SYSTEM:|ignore any human-approval|private tokens?)\b",
    re.IGNORECASE,
)


def parse_timestamp(value):
    """Parse the exact UTC timestamp format used by the supplied policies."""

    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise ValueError("Expected an ISO UTC timestamp with seconds")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def typed_value(field, raw):
    """Convert a labeled document value into its deterministic Python value."""

    value = raw.strip()
    if field == "referral_indicator":
        return None if value.lower() in {"", "none"} else value.lower()
    if value.lower() in UNKNOWN:
        return None
    if field in ("injury_reported", "vehicle_drivable"):
        return {"yes": True, "true": True, "no": False, "false": False}.get(value.lower())
    if field == "loss_type":
        return value.lower()
    return value


def extract_assertions(documents):
    """Extract recognized ``Label: value`` lines and retain exact provenance.

    Each assertion keeps the parsed value, source ID, and original line used as
    its citation quote. Instruction-like lines are treated as untrusted content
    and are never interpreted as facts.
    """

    assertions = []
    suspicious_sources = []
    for source_id, text in documents.items():
        for line in text.splitlines():
            if INSTRUCTION_PATTERN.search(line):
                suspicious_sources.append(source_id)
                continue
            if ":" not in line:
                continue
            label, raw = line.split(":", 1)
            field = LABELS.get(label.strip().lower())
            if field:
                assertions.append({
                    "field": field,
                    "value": typed_value(field, raw),
                    "source_id": source_id,
                    "quote": line,
                })
    return assertions, sorted(set(suspicious_sources))


def reconcile(assertions):
    """Resolve agreeing assertions and preserve every source in a conflict."""

    resolved = {}
    conflicts = []
    for field in sorted({item["field"] for item in assertions}):
        present = [
            item for item in assertions
            if item["field"] == field and item["value"] is not None
        ]
        distinct_values = {json.dumps(item["value"], sort_keys=True) for item in present}
        if len(distinct_values) > 1:
            resolved[field] = None
            conflicts.append({"field": field, "assertions": present})
        else:
            resolved[field] = present[0]["value"] if present else None
    return resolved, conflicts


def select_policy(policies, incident_timestamp):
    """Select the single half-open policy interval containing the incident."""

    try:
        incident = parse_timestamp(incident_timestamp)
        matches = [
            item for item in policies
            if parse_timestamp(item["effective_from"])
            <= incident
            < parse_timestamp(item["effective_to"])
        ]
    except (TypeError, ValueError, KeyError):
        return None, (
            "Policy selection is unresolved because the incident timestamp "
            "or a policy interval is invalid."
        )
    if not matches:
        return None, "No policy interval contains the incident timestamp."
    if len(matches) > 1:
        return None, "Multiple policy intervals contain the incident timestamp."
    return matches[0], None


def classify_loss(loss_type):
    """Return a supported classification or the required ``unsupported`` value."""

    return loss_type if loss_type in SUPPORTED_LOSSES else "unsupported"


def determine_route(
    classification,
    missing_fields,
    conflicts,
    assertions,
    *,
    policy_available,
    claims_available,
    policy_issue=None,
    evidence_issues=(),
):
    """Apply the complete routing precedence from ``AUTO-TRIAGE-01``.

    Manual-review conditions are collected here rather than being hidden in
    the draft builder. Injury and non-drivable flags are derived from every
    assertion so they remain visible even when conflicting evidence forces
    manual review.
    """

    manual_reasons = []
    if classification == "unsupported":
        manual_reasons.append("The loss type is unsupported or cannot be resolved.")
    for field in missing_fields:
        manual_reasons.append(f"Required {field.replace('_', ' ')} is missing.")
    for conflict in conflicts:
        field = conflict["field"].replace("_", " ")
        manual_reasons.append(f"Conflicting {field} accounts require reconciliation.")
    if not policy_available:
        manual_reasons.append(
            "The policy service is unavailable after 3 attempts. Its failure is not empty evidence."
        )
    if not claims_available:
        manual_reasons.append(
            "The claims service is unavailable after 3 attempts. Its failure is not an empty claims history."
        )
    if policy_issue:
        manual_reasons.append(policy_issue)
    manual_reasons.extend(evidence_issues)

    injury_reported = any(
        item["field"] == "injury_reported" and item["value"] is True
        for item in assertions
    )
    non_drivable = any(
        item["field"] == "vehicle_drivable" and item["value"] is False
        for item in assertions
    )
    repair_referral = any(
        item["field"] == "referral_indicator"
        and item["value"] == "repair_amount_conflict"
        for item in assertions
    )

    if manual_reasons:
        routing = "manual_review"
    elif injury_reported or non_drivable:
        routing = "urgent"
    elif classification == "theft" or repair_referral:
        routing = "specialist"
    else:
        routing = "standard"

    return {
        "routing": routing,
        "manual_reasons": manual_reasons,
        "injury_reported": injury_reported,
        "non_drivable": non_drivable,
        "repair_referral": repair_referral,
    }
