"""Stable key for one logical draft. The same draft replays; a different draft conflicts."""

import hashlib
import json


def creation_block(draft: dict) -> str | None:
    """Return a reason to refuse record creation, or None when a human-approved write may proceed."""
    indicators = draft.get("indicators") if isinstance(draft, dict) else None
    if not isinstance(indicators, dict):
        return "draft_incomplete"
    if not indicators.get("claims_service_ok"):
        return "claims_history_incomplete"
    if indicators.get("duplicate_intake") or indicators.get("record_exists"):
        return "duplicate_loss"
    return None


def idempotency_key(case_id: str, draft: dict) -> str:
    raw = json.dumps({"case_id": case_id, "draft": draft}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()
