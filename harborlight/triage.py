"""Deterministic FNOL classification, policy selection, and routing.

Supplied procedure AUTO-TRIAGE-01 is the only authority. This module does not
decide coverage, liability, fraud, reserves, settlement, denial, or payment.
"""

from datetime import datetime

SUPPORTED = {"collision", "theft", "glass"}
ROUTES = ("manual_review", "urgent", "specialist", "standard")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def select_policy(policies: list[dict], incident: str | None) -> list[dict]:
    if not incident:
        return []
    moment = parse_time(incident)
    chosen = []
    for item in policies:
        start = parse_time(item["effective_from"])
        end = parse_time(item["effective_to"])
        if start <= moment < end:
            chosen.append(item)
    return chosen


def route(classification: str, *, missing: list[str], conflicts: list[str], policy_ok: bool, claims_ok: bool, injury: bool, non_drivable: bool, referral: str | None) -> str:
    if (
        classification == "unsupported"
        or missing
        or conflicts
        or not policy_ok
        or not claims_ok
    ):
        return "manual_review"
    if injury or non_drivable:
        return "urgent"
    if classification == "theft" or referral == "repair_amount_conflict":
        return "specialist"
    return "standard"


WORDING = {
    "collision": {
        "AUTO-WORDING-V1": "Collision section: accidental impact involving the described vehicle may be considered under the",
        "AUTO-WORDING-V2": "Collision section: physical damage from accidental impact involving the described vehicle is",
    },
    "theft": {
        "AUTO-WORDING-V1": "Theft section: reported taking of the described vehicle may be considered under the theft section,",
        "AUTO-WORDING-V2": "Theft section: reported theft of the described vehicle is addressed by the theft wording; the",
    },
    "glass": {
        "AUTO-WORDING-V1": "Glass section: damage to vehicle glazing is addressed under the base physical-damage wording.",
        "AUTO-WORDING-V2": "Glass section: glazing damage is addressed by the physical-damage wording and any listed glass endorsement.",
    },
}


def wording_quote(source_id: str, classification: str, text: str) -> str:
    preferred = WORDING.get(classification, {}).get(source_id)
    if preferred and preferred in text:
        return preferred
    marker = f"Source ID: {source_id}"
    if marker in text:
        return marker
    for line in text.splitlines():
        if line.startswith(("Policy form:", "Endorsement:")):
            return line
    raise ValueError(f"no quote in {source_id}")
