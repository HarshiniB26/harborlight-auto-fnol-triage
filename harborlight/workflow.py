"""Case workflow: tools and documents in, contract draft out. No human approval."""

import json
from pathlib import Path

from harborlight.enterprise import EnterpriseClient, ToolResult
from harborlight.evidence import (
    canonical_json,
    excerpt,
    extract_fields,
    injection_flags,
    is_missing,
    load_text,
    parse_bool,
)
from harborlight.observe import Tracer, incr
from harborlight.redact import redact_text
from harborlight.resilience import UpstreamUnavailable
from harborlight.triage import route, select_policy, wording_quote

TRIAGE_ID = "AUTO-TRIAGE-01"
TRIAGE_QUOTE = "Supported losses: collision, theft, glass. Other loss types classify as unsupported and route manual_review."
QUESTIONS = {
    "driver_name": "What is the full name of the person who was driving or last in charge of the vehicle?",
    "incident_location": "Where did the incident occur, including street and area if known?",
}


def _cite(claim: str, source_id: str, quote: str) -> dict:
    return {"claim": claim, "source_id": source_id, "quote": quote}


class Workflow:
    def __init__(self, data_dir: Path, client: EnterpriseClient, tracer: Tracer):
        self.data_dir = data_dir
        self.client = client
        self.tracer = tracer
        procedure = load_text(data_dir, TRIAGE_ID)
        if not procedure or TRIAGE_QUOTE not in procedure:
            raise RuntimeError("AUTO-TRIAGE-01 is missing or unreadable")
        self.procedure = procedure

    def draft_case(self, case: dict) -> dict:
        case_id = case["case_id"]
        with self.tracer.span("draft_case", case_id=case_id):
            documents = self._documents(case.get("documents") or [])
            policy = self.client.read_tool(case_id, "policy")
            claims = self.client.read_tool(case_id, "claims")
            try:
                existing = self.client.get_record(case_id)
            except (UpstreamUnavailable, OSError, TimeoutError) as exc:
                existing = None
                self.tracer.event("record_lookup_failed", case_id=case_id, error=type(exc).__name__)
            output = self._assemble(case_id, documents, policy, claims, existing)
            incr("drafts_total")
            return output

    def _documents(self, source_ids: list[str]) -> list[dict]:
        loaded = []
        for source_id in source_ids:
            text = load_text(self.data_dir, source_id)
            if text is None:
                self.tracer.event("document_missing", source_id=source_id)
                continue
            loaded.append(
                {
                    "source_id": source_id,
                    "text": text,
                    "fields": extract_fields(text),
                    "injections": injection_flags(source_id, text),
                }
            )
            if loaded[-1]["injections"]:
                incr("injection_blocked_total")
                self.tracer.event("untrusted_instruction_ignored", source_id=source_id)
        return loaded

    def _assemble(self, case_id, documents, policy: ToolResult, claims: ToolResult, existing) -> dict:
        by_field: dict[str, set[str]] = {}
        for doc in documents:
            for key, value in doc["fields"].items():
                by_field.setdefault(key, set()).add(value)
        intake = next((doc for doc in documents if doc["source_id"].endswith("-INTAKE")), documents[0] if documents else None)
        intake_fields = intake["fields"] if intake else {}

        def one(field: str):
            values = by_field.get(field) or set()
            if field in intake_fields:
                return intake_fields[field]
            return next(iter(values)) if len(values) == 1 else None

        conflicts = sorted(
            field
            for field, values in by_field.items()
            if field in {"incident_timestamp", "policy_number", "vehicle_id", "loss_type", "injury_reported", "driver_name", "incident_location"}
            and len({item.strip().lower() for item in values if not is_missing(item)}) > 1
        )
        loss = (one("loss_type") or "").strip().lower()
        classification = loss if loss in {"collision", "theft", "glass"} else "unsupported"
        incident = one("incident_timestamp")
        injury_values = {parse_bool(item) for item in by_field.get("injury_reported", set())}
        injury = True in injury_values
        drivable = parse_bool(one("vehicle_drivable"))
        non_drivable = drivable is False
        referral = one("referral_indicator")
        if referral and referral.strip().lower() == "none":
            referral = None
        missing = [field for field in ("driver_name", "incident_location") if is_missing(one(field))]

        policy_body = policy.body if policy.ok and isinstance(policy.body, dict) else None
        matches: list[dict] = []
        policy_uncertain = not policy.ok or policy_body is None
        if policy_body is not None:
            matches = select_policy(policy_body.get("policies") or [], incident)
            if len(matches) != 1:
                policy_uncertain = True
        selected = matches[0] if len(matches) == 1 else None
        policy_ids = [TRIAGE_ID]
        if selected:
            policy_ids.append(selected["wording_source"])
            policy_ids.extend(selected.get("endorsements") or [])

        citations = [_cite("triage_and_potential_coverage", TRIAGE_ID, TRIAGE_QUOTE)]
        if selected:
            wording = load_text(self.data_dir, selected["wording_source"])
            if wording:
                citations.append(
                    _cite(
                        "triage_and_potential_coverage",
                        selected["wording_source"],
                        wording_quote(selected["wording_source"], classification if classification != "unsupported" else "collision", wording),
                    )
                )
            for endorsement in selected.get("endorsements") or []:
                text = load_text(self.data_dir, endorsement)
                if text:
                    line = next((row.strip() for row in text.splitlines() if row.startswith("Endorsement:")), f"Source ID: {endorsement}")
                    citations.append(_cite("triage_and_potential_coverage", endorsement, line))
        for doc in documents:
            quote = excerpt(doc["text"], _document_quote(doc))
            if quote:
                citations.append(_cite("incident_evidence", doc["source_id"], quote))
        if policy.ok and policy_body and policy_body.get("source_id"):
            encoded = canonical_json(policy_body)
            fragment = f'"policy_number": "{policy_body.get("policy_number")}"'
            if fragment not in encoded:
                fragment = f'"source_id": "{policy_body["source_id"]}"'
            citations.append(_cite("policy_or_claim_history", policy_body["source_id"], fragment))
        if claims.ok and isinstance(claims.body, dict) and claims.body.get("source_id"):
            encoded = canonical_json(claims.body)
            fragment = '"existing_claims": []' if '"existing_claims": []' in encoded else f'"source_id": "{claims.body["source_id"]}"'
            citations.append(_cite("policy_or_claim_history", claims.body["source_id"], fragment))

        routing = route(
            classification,
            missing=missing,
            conflicts=conflicts,
            policy_ok=not policy_uncertain,
            claims_ok=claims.ok,
            injury=injury,
            non_drivable=non_drivable,
            referral=referral,
        )
        duplicate = False
        if claims.ok and isinstance(claims.body, dict):
            deliveries = claims.body.get("intake_deliveries")
            duplicate = bool(claims.body.get("existing_claims")) or (isinstance(deliveries, int) and deliveries > 1)
        if existing:
            duplicate = True

        policy_number = None
        vehicle_id = None
        if policy_body:
            policy_number = policy_body.get("policy_number")
            vehicle_id = policy_body.get("vehicle_id")
        policy_number = policy_number or one("policy_number")
        vehicle_id = vehicle_id or one("vehicle_id")

        facts = {
            "policy_number": policy_number,
            "incident_timestamp": incident,
            "vehicle_id": vehicle_id,
            "injury_reported": injury,
            "selected_policy_version": None if selected is None else str(selected.get("version")),
        }
        narrative = _narrative(
            case_id,
            classification,
            routing,
            facts,
            missing,
            conflicts,
            policy,
            claims,
            duplicate,
            documents,
            injury,
            non_drivable,
            referral,
        )
        output = {
            "case_id": case_id,
            "classification": classification,
            "routing": routing,
            "policy_ids": policy_ids,
            "facts": facts,
            "missing_information": missing,
            "deadline": None,
            "citations": citations,
            "follow_up_questions": [{"field": field, "question": QUESTIONS[field]} for field in missing],
            "narrative": redact_text(narrative),
            "indicators": {
                "injury_reported": injury,
                "non_drivable": non_drivable,
                "referral_indicator": referral,
                "duplicate_intake": duplicate,
                "policy_service_ok": policy.ok,
                "claims_service_ok": claims.ok,
                "policy_attempts": policy.attempts,
                "claims_attempts": claims.attempts,
                "conflicts": conflicts,
                "untrusted_instructions_ignored": sorted({sid for doc in documents for sid in doc["injections"]}),
                "record_exists": existing is not None,
            },
        }
        return json.loads(redact_text(json.dumps(output)))


def _document_quote(doc: dict) -> str:
    fields = doc["fields"]
    if "loss_type" in fields and doc["source_id"].endswith("-INTAKE"):
        return f"Loss type: {fields['loss_type']}"
    if "vehicle_id" in fields and "REPORT" in doc["source_id"]:
        return f"Vehicle reference: {fields['vehicle_id']}"
    if "estimate_reference" in fields:
        return f"Estimate reference: {fields['estimate_reference']}"
    return ""


def _narrative(case_id, classification, routing, facts, missing, conflicts, policy, claims, duplicate, documents, injury, non_drivable, referral) -> str:
    parts = [
        f"Draft FNOL for a licensed human handler, case {case_id}.",
        f"Classification from supplied loss labels: {classification}. Recommended route: {routing}.",
        f"Incident timestamp: {facts['incident_timestamp']}. Policy {facts['policy_number']}, vehicle {facts['vehicle_id']}, selected policy version {facts['selected_policy_version']}.",
        "Cited wording is potentially relevant only. This draft makes no coverage, liability, fraud, reserve, settlement, denial, or payment decision.",
        "A referral indicator is not a fraud finding. A policy-period mismatch is not a denial.",
    ]
    if injury:
        parts.append("Injury was reported. Keep this visible for the handler even if the route is manual review.")
    if non_drivable:
        parts.append("The vehicle was reported not drivable. Keep this urgency visible for the handler.")
    if referral:
        parts.append(f"Supplied referral indicator: {referral}. This is a routing signal only.")
    if missing:
        parts.append("Missing required intake fields: " + ", ".join(missing) + ".")
    if conflicts:
        parts.append("Conflicting supplied facts require human reconciliation: " + ", ".join(conflicts) + ".")
    if facts["selected_policy_version"] is None:
        parts.append("No single policy interval contains the incident timestamp. Version is null pending human review.")
    if not policy.ok:
        parts.append(f"Policy service was unavailable after {policy.attempts} attempts. Facts that depend on it are incomplete.")
    if not claims.ok:
        parts.append(f"Claims history was unavailable after {claims.attempts} attempts. Duplicate review is incomplete, so creation must not proceed.")
    elif duplicate:
        parts.append("Claims history or the creation ledger shows this loss was already delivered. Do not create another claim.")
    else:
        parts.append("Claims history was retrieved before any creation proposal. No existing ledger record was required to classify the notice.")
    if any(doc["injections"] for doc in documents):
        parts.append("Untrusted instruction-like text was present in case material and was ignored. It is not an operator instruction.")
    parts.append("Customer communication, if any, remains a draft until a human approves the exact text. Claim creation requires that approval and a fresh idempotency key.")
    return " ".join(parts)
