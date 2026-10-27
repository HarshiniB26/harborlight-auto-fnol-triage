"""Build the required cited FNOL draft from already-retrieved evidence."""

import json
import re
from pathlib import Path

from .rules import (
    QUESTIONS,
    REQUIRED_INTAKE_FIELDS,
    classify_loss,
    determine_route,
    extract_assertions,
    reconcile,
    select_policy,
)


PROCEDURE_ID = "AUTO-TRIAGE-01"
SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
DOCUMENT_CITATION_PRIORITY = (
    "loss_type",
    "incident_timestamp",
    "vehicle_id",
    "injury_reported",
)


def _json_fragment(key, value):
    """Return an exact fragment of a normalized JSON evidence source."""

    return f'"{key}": ' + json.dumps(value, sort_keys=True)


def _line_starting(text, prefix):
    """Return the first exact source line beginning with ``prefix``."""

    return next((line for line in text.splitlines() if line.startswith(prefix)), None)


def _section(text, prefix):
    """Return one policy paragraph, preserving its original wrapped lines."""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        paragraph = [line]
        for following in lines[index + 1:]:
            if not following.strip() or following.endswith("section:"):
                break
            paragraph.append(following)
            if following.endswith("."):
                break
        return "\n".join(paragraph)
    return None


def _add_citation(citations, claim, source_id, quote, sources):
    """Add a unique citation only when its quote is exact and redacted."""

    source_text = sources.get(source_id)
    if not isinstance(quote, str) or not quote.strip():
        return
    if "private_token" in quote or "SYNTHETIC-PRIVATE-" in quote:
        return
    citation = {"claim": claim, "source_id": source_id, "quote": quote}
    if isinstance(source_text, str) and quote in source_text and citation not in citations:
        citations.append(citation)


def _load_policy(data_dir, source_id):
    """Load a named supplied policy without allowing path traversal."""

    if not isinstance(source_id, str) or not SAFE_SOURCE_ID.fullmatch(source_id):
        return None
    path = Path(data_dir) / "policies" / f"{source_id}.md"
    return path.read_text() if path.is_file() else None


def _cite_document(citations, source_id, assertions, text, sources):
    """Cite the most decision-relevant recognized assertion in a document."""

    available = [item for item in assertions if item["source_id"] == source_id]
    selected = next(
        (
            item
            for field in DOCUMENT_CITATION_PRIORITY
            for item in available
            if item["field"] == field
        ),
        None,
    )
    if selected:
        claim = f"Reported {selected['field'].replace('_', ' ')}."
        _add_citation(citations, claim, source_id, selected["quote"], sources)
        return
    fallback = _line_starting(text, "Estimate reference:") or _line_starting(
        text, "Vehicle reference:"
    )
    _add_citation(citations, "Supporting incident evidence.", source_id, fallback, sources)


def build_draft(case, documents, policy, claims, data_dir):
    """Create one output-contract draft and its safe processing warnings."""

    sources = dict(documents)
    assertions, suspicious_sources = extract_assertions(documents)
    warnings = []
    evidence_issues = []
    citations = []

    if suspicious_sources:
        warnings.append("Instruction-like text in the evidence was ignored.")

    missing_documents = [
        source_id for source_id in case.get("documents", [])
        if source_id not in documents
    ]
    if missing_documents:
        evidence_issues.append(
            "Some listed documents are unavailable: " + ", ".join(missing_documents) + "."
        )

    policy_available = (
        isinstance(policy, dict)
        and isinstance(policy.get("policies"), list)
        and isinstance(policy.get("source_id"), str)
    )
    claims_available = (
        isinstance(claims, dict)
        and isinstance(claims.get("existing_claims"), list)
        and isinstance(claims.get("source_id"), str)
    )

    if policy_available:
        sources[policy["source_id"]] = json.dumps(policy, sort_keys=True)
        for field in ("policy_number", "vehicle_id"):
            if field in policy:
                assertions.append({
                    "field": field,
                    "value": policy[field],
                    "source_id": policy["source_id"],
                    "quote": _json_fragment(field, policy[field]),
                })
    if claims_available:
        sources[claims["source_id"]] = json.dumps(claims, sort_keys=True)

    resolved, conflicts = reconcile(assertions)
    missing_fields = [
        field for field in REQUIRED_INTAKE_FIELDS
        if resolved.get(field) is None
    ]
    classification = classify_loss(resolved.get("loss_type"))

    selected_policy = None
    policy_issue = None
    if policy_available:
        selected_policy, policy_issue = select_policy(
            policy["policies"], resolved.get("incident_timestamp")
        )

    policy_ids = []
    procedure = _load_policy(data_dir, PROCEDURE_ID)
    if procedure:
        sources[PROCEDURE_ID] = procedure
        policy_ids.append(PROCEDURE_ID)
        _add_citation(
            citations,
            "Supplied classification and routing procedure.",
            PROCEDURE_ID,
            _line_starting(procedure, "Supported losses:"),
            sources,
        )
    else:
        evidence_issues.append("The triage procedure is unavailable.")

    if selected_policy:
        policy_sources = [
            selected_policy.get("wording_source"),
            *(selected_policy.get("endorsements") or []),
        ]
        for source_id in policy_sources:
            if not source_id or source_id in policy_ids:
                continue
            text = _load_policy(data_dir, source_id)
            if not text:
                evidence_issues.append(
                    "Selected policy wording or an endorsement is unavailable."
                )
                continue
            sources[source_id] = text
            policy_ids.append(source_id)
            prefix = (
                classification.capitalize() + " section:"
                if source_id == selected_policy.get("wording_source")
                else "Endorsement:"
            )
            quote = _section(text, prefix) or _line_starting(text, "Policy form:")
            _add_citation(
                citations,
                "Potentially relevant policy wording; no coverage decision.",
                source_id,
                quote,
                sources,
            )

    for source_id, text in documents.items():
        _cite_document(citations, source_id, assertions, text, sources)

    # A conflict must show the exact quote from every participating source.
    for conflict in conflicts:
        for assertion in conflict["assertions"]:
            _add_citation(
                citations,
                f"Conflicting reported {conflict['field'].replace('_', ' ')}.",
                assertion["source_id"],
                assertion["quote"],
                sources,
            )

    if policy_available:
        policy_citation_claim = (
            f"Policy selection unresolved: {policy_issue}"
            if policy_issue
            else "Policy intervals used for incident-time selection."
        )
        _add_citation(
            citations,
            policy_citation_claim,
            policy["source_id"],
            _json_fragment("policies", policy["policies"]),
            sources,
        )
    if claims_available:
        _add_citation(
            citations,
            "Existing claims checked before record creation.",
            claims["source_id"],
            _json_fragment("existing_claims", claims["existing_claims"]),
            sources,
        )

    decision = determine_route(
        classification,
        missing_fields,
        conflicts,
        assertions,
        policy_available=policy_available,
        claims_available=claims_available,
        policy_issue=policy_issue,
        evidence_issues=evidence_issues,
    )
    selected_version = selected_policy.get("version") if selected_policy else None
    facts = {
        "policy_number": resolved.get("policy_number"),
        "incident_timestamp": resolved.get("incident_timestamp"),
        "vehicle_id": resolved.get("vehicle_id"),
        "injury_reported": resolved.get("injury_reported"),
        "selected_policy_version": (
            None if selected_version is None else str(selected_version)
        ),
    }
    narrative_parts = [
        f"Draft FNOL for human handler: {classification} incident at "
        f"{facts['incident_timestamp'] or 'unresolved'}.",
        f"Recommended route: {decision['routing']}.",
    ]
    if decision["injury_reported"]:
        narrative_parts.append(
            "Injury was reported. This stays visible even when manual review takes precedence."
        )
    if decision["non_drivable"]:
        narrative_parts.append(
            "The vehicle was reported non-drivable. This stays visible even when manual review takes precedence."
        )
    if decision["repair_referral"]:
        narrative_parts.append(
            "A repair-amount discrepancy is a referral indicator only. It is not a fraud finding."
        )
    narrative_parts.extend(decision["manual_reasons"])
    narrative_parts.extend(warnings)
    if len(policy_ids) > 1:
        narrative_parts.append("Cited coverage wording is potentially relevant only.")
    else:
        narrative_parts.append(
            "No policy wording is treated as applicable while policy selection is unresolved."
        )
    narrative_parts.append(
        "No coverage, liability, fraud, reserve, settlement, denial or payment decision is made. "
        "Draft only; human approval is required."
    )

    draft = {
        "case_id": case["case_id"],
        "classification": classification,
        "routing": decision["routing"],
        "policy_ids": policy_ids,
        "facts": facts,
        "missing_information": missing_fields,
        "deadline": None,
        "citations": citations,
        "follow_up_questions": [
            {"field": field, "question": QUESTIONS[field]}
            for field in missing_fields
        ],
        "narrative": " ".join(narrative_parts),
    }
    return draft, warnings
