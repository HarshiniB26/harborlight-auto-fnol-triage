"""Orchestrate case processing, human review, and approved-record filing."""
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .client import MockClient
from .draft import build_draft
from .observability import Observability
from .reliability import ReliableCaller
from .storage import (
    QUEUE_META_KEY,
    JsonStateStore,
    LocalEvidenceStore,
    PostgresStateStore,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def canonical(value):
    """Serialize a value exactly as the supplied runtime hashes it."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    """Return the SHA-256 hash used to bind approval to an exact draft."""

    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now():
    """Return a stable UTC timestamp for handler-facing activity history."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remove_private_tokens(value):
    """Recursively remove restricted ``private_token`` response fields."""

    if isinstance(value, dict):
        return {
            key: _remove_private_tokens(item)
            for key, item in value.items()
            if key != "private_token"
        }
    if isinstance(value, list):
        return [_remove_private_tokens(item) for item in value]
    return value


class Workbench:
    """Coordinate the UI workflow while keeping the supplied runtime private."""

    def __init__(
        self,
        api_url,
        data_dir,
        state_path,
        *,
        observability=None,
        reliable_caller=None,
        monotonic=None,
        evidence_store=None,
        state_store=None,
        queue_cursor=None,
    ):
        self.client = MockClient(api_url)
        self.data_dir = Path(data_dir)
        self.state_path = Path(state_path)
        self.evidence_store = evidence_store or LocalEvidenceStore(self.data_dir)
        self.state_store = state_store or JsonStateStore(self.state_path)
        self.supports_queue_cursor = (
            isinstance(self.state_store, PostgresStateStore)
            if queue_cursor is None
            else bool(queue_cursor)
        )
        self._lock = threading.Lock()
        self.observability = observability or Observability()
        self.reliable_caller = reliable_caller or ReliableCaller(self.observability)
        self.monotonic = monotonic or time.monotonic

    def list_cases(self):
        """Return the upstream case queue enriched with local workflow status."""

        state = self._read()
        listed = []
        for case in self.client.list_cases():
            case_id = case["case_id"]
            entry = state.get(case_id) or {}
            draft = entry.get("draft") or {}
            record = self._live_record(case_id)
            listed.append({
                "case_id": case_id,
                "documents": case.get("documents") or [],
                "status": self._status(case_id, state, record=record),
                "duplicate_intake": self._duplicate_intake(case_id, record),
                "record_id": (record or {}).get("record_id"),
                "record_case_id": (record or {}).get("case_id"),
                "classification": draft.get("classification"),
                "routing": draft.get("routing"),
                "processed_at": entry.get("processed_at"),
            })
        return listed

    def health(self):
        """Return process health without making an upstream dependency call."""

        return {
            "status": "ok",
            "service": "fnol-workbench",
            "circuits": self.reliable_caller.health(),
        }

    def readiness(self, trace_id=None):
        """Return whether the supplied runtime is reachable for new work."""

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        status, payload = self.client.health(trace_id)
        ready = status == 200 and isinstance(payload, dict)
        self.observability.event(
            "readiness_checked",
            trace_id=trace_id,
            ready=ready,
            dependency_status=status,
        )
        return {
            "status": "ready" if ready else "not_ready",
            "dependency": "supplied-runtime",
            "trace_id": trace_id,
        }

    def metrics(self):
        """Return the current in-process metric snapshot."""

        return self.observability.metrics.snapshot()

    def export_results(self):
        """Return the exact output-contract array once every case has a draft."""

        requested = [case["case_id"] for case in self.client.list_cases()]
        state = self._read()
        missing = [case_id for case_id in requested if not (state.get(case_id) or {}).get("draft")]
        if missing:
            return {
                "error": "drafts_incomplete",
                "message": "Prepare every requested case before exporting results.",
                "missing_case_ids": missing,
            }
        return [state[case_id]["draft"] for case_id in requested]

    def prepare_queue(self, trace_id=None, case_ids=None):
        """Sequentially prepare cases and submit each resulting draft for review."""

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        if case_ids is None:
            targets = [case["case_id"] for case in self.client.list_cases(trace_id)]
        elif not isinstance(case_ids, list) or any(
            not isinstance(case_id, str) for case_id in case_ids
        ):
            return {
                "error": "invalid_case_ids",
                "message": "case_ids must be an array of strings.",
                "trace_id": trace_id,
            }
        else:
            targets = list(dict.fromkeys(case_ids))

        self.observability.event(
            "queue_prepare_started", trace_id=trace_id, case_count=len(targets)
        )
        rows = []
        handled = []
        failed = []
        for case_id in targets:
            row = {"case_id": case_id}
            try:
                processed = self.process(case_id, trace_id)
                row["process"] = processed
                if processed.get("error") == "draft_locked":
                    handled.append(case_id)
                    self._merge_last_seen([case_id])
                    rows.append(row)
                    continue
                if processed.get("error"):
                    raise RuntimeError(processed.get("message") or processed["error"])
                review = self.submit_review(case_id, trace_id)
                row["review"] = review
                if review.get("error"):
                    raise RuntimeError(review.get("message") or review["error"])
            except Exception as error:
                failed.append(case_id)
                row["error"] = "batch_case_failed"
                row["message"] = str(error) or type(error).__name__
                self.observability.event(
                    "queue_case_failed",
                    level="ERROR",
                    trace_id=trace_id,
                    case_id=case_id,
                    error_type=type(error).__name__,
                )
            else:
                handled.append(case_id)
                # Checkpoint each success immediately. SQS redelivery then retries
                # only cases that did not reach a durable pending/locked state.
                self._merge_last_seen([case_id])
            rows.append(row)

        status = "completed" if not failed else "completed_with_errors"
        self.observability.event(
            "queue_prepare_completed",
            trace_id=trace_id,
            status=status,
            case_count=len(targets),
            handled_count=len(handled),
            error_count=len(failed),
        )
        return {
            "status": status,
            "trace_id": trace_id,
            "handled_case_ids": handled,
            "failed_case_ids": failed,
            "cases": rows,
        }

    def apply_dataset_diff(self, current_ids, trace_id=None):
        """Process additions against the durable handled-ID cursor."""

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        if not self.supports_queue_cursor:
            return {
                "error": "cursor_unavailable",
                "message": "Dataset diff requires durable queue cursor storage.",
                "trace_id": trace_id,
                "ack": False,
            }
        if not isinstance(current_ids, (list, tuple, set)) or any(
            not isinstance(case_id, str) for case_id in current_ids
        ):
            return {
                "error": "invalid_current_ids",
                "message": "current_ids must contain only strings.",
                "trace_id": trace_id,
                "ack": False,
            }

        current = set(current_ids)
        last_seen = self._read_last_seen()
        added = sorted(current - last_seen)
        removed = sorted(last_seen - current)
        prepared = self.prepare_queue(trace_id=trace_id, case_ids=added) if added else {
            "status": "completed",
            "handled_case_ids": [],
            "failed_case_ids": [],
            "cases": [],
        }
        failed = prepared.get("failed_case_ids") or []
        result = {
            "status": prepared.get("status", "completed"),
            "trace_id": trace_id,
            "added_case_ids": added,
            "removed_case_ids": removed,
            "handled_case_ids": prepared.get("handled_case_ids") or [],
            "failed_case_ids": failed,
            "prepare": prepared,
            "ack": not failed,
        }
        self.observability.event(
            "dataset_diff_completed",
            trace_id=trace_id,
            added_count=len(added),
            removed_count=len(removed),
            handled_count=len(result["handled_case_ids"]),
            failed_count=len(failed),
            ack=result["ack"],
        )
        return result

    def process(self, case_id, trace_id=None):
        """Retrieve evidence and build a new deterministic draft for one case."""

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        started = self.monotonic()
        self.observability.metrics.increment("fnol_processing_total")
        self.observability.event(
            "processing_started", trace_id=trace_id, case_id=case_id
        )
        self._check_id(case_id)
        try:
            case = self._find(case_id, trace_id)
            with self._lock:
                state = self._read()
                entry = state.get(case_id) or {}
                record = self._live_record(case_id, trace_id)
                if record:
                    return {
                        "error": "draft_locked",
                        "message": "A record already exists for this loss.",
                        "draft": entry.get("draft"),
                        "record": record,
                        "trace_id": trace_id,
                    }
                review = self._live_review(entry, trace_id)
                if review and review.get("status") in ("pending", "approved"):
                    return {
                        "error": "draft_locked",
                        "message": "This draft is waiting on the current review.",
                        "draft": entry.get("draft"),
                        "trace_id": trace_id,
                    }
                documents = self._documents(case.get("documents") or [])
                self.observability.event(
                    "evidence_documents_loaded",
                    trace_id=trace_id,
                    case_id=case_id,
                    expected_count=len(case.get("documents") or []),
                    loaded_count=len(documents),
                )
                policy = _remove_private_tokens(
                    self.reliable_caller.call(
                        "policy",
                        self.client.get_policy,
                        case_id=case_id,
                        trace_id=trace_id,
                    )
                )
                claims = _remove_private_tokens(
                    self.reliable_caller.call(
                        "claims",
                        self.client.get_claims,
                        case_id=case_id,
                        trace_id=trace_id,
                    )
                )
                # Unavailable evidence must not be treated as empty evidence.
                # The deterministic rules route these cases to manual_review.
                unavailable = [
                    name
                    for name, value in (
                        ("policy", policy),
                        ("claims", claims),
                    )
                    if not isinstance(value, dict)
                ]
                if unavailable:
                    self.observability.event(
                        "processing_dependency_unavailable",
                        level="WARNING",
                        trace_id=trace_id,
                        case_id=case_id,
                        dependencies=unavailable,
                    )

                self.observability.event(
                    "evidence_retrieval_completed",
                    trace_id=trace_id,
                    case_id=case_id,
                    policy_available=isinstance(policy, dict),
                    claims_available=isinstance(claims, dict),
                )
                draft, warnings = build_draft(
                    case,
                    documents,
                    policy,
                    claims,
                    policy_loader=self.evidence_store.policy,
                )
                if warnings:
                    self.observability.event(
                        "untrusted_document_instruction_ignored",
                        level="WARNING",
                        trace_id=trace_id,
                        case_id=case_id,
                        warning_count=len(warnings),
                    )
                self.observability.event(
                    "triage_decision_completed",
                    trace_id=trace_id,
                    case_id=case_id,
                    classification=draft["classification"],
                    routing=draft["routing"],
                    missing_count=len(draft["missing_information"]),
                    citation_count=len(draft["citations"]),
                )
                state[case_id] = {
                    "draft": draft,
                    "warnings": warnings,
                    "review_id": None,
                    "idempotency_key": None,
                    "processed_at": utc_now(),
                    "process_trace_id": trace_id,
                }
                self._write(state)
            duration_ms = (self.monotonic() - started) * 1000
            self.observability.metrics.increment("fnol_processing_completed_total")
            if duration_ms <= 15000:
                self.observability.metrics.increment("fnol_processing_within_15s_total")
            self.observability.metrics.observe_processing(duration_ms)
            self.observability.event(
                "processing_completed",
                trace_id=trace_id,
                case_id=case_id,
                classification=draft["classification"],
                routing=draft["routing"],
                warning_count=len(warnings),
                duration_ms=round(duration_ms, 3),
            )
            return {
                "draft": draft,
                "warnings": warnings,
                "status": "draft_ready",
                "trace_id": trace_id,
            }
        except Exception as error:
            self.observability.metrics.increment("fnol_processing_error_total")
            self.observability.event(
                "processing_failed",
                level="ERROR",
                trace_id=trace_id,
                case_id=case_id,
                error_type=type(error).__name__,
            )
            raise

    def submit_review(self, case_id, trace_id=None):
        """Submit the saved draft once and remember its external review ID."""

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        self._check_id(case_id)
        self._find(case_id, trace_id)
        with self._lock:
            state = self._read()
            entry = state.get(case_id) or {}
            draft = entry.get("draft")
            if not draft:
                return {"error": "draft_required", "message": "Process this case before submitting it."}
            if self._live_record(case_id, trace_id):
                return {"error": "existing_record", "message": "This loss already has a record."}
            review = self._live_review(entry, trace_id)
            if review and review.get("status") in ("pending", "approved"):
                return review
            status, payload = self.client.submit_review(case_id, draft, trace_id)
            if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("review_id"):
                return {"error": "review_failed", "message": "The review was not accepted."}
            entry["review_id"] = payload["review_id"]
            entry["review_submitted_at"] = utc_now()
            entry["review_trace_id"] = trace_id
            state[case_id] = entry
            self._write(state)
            self.observability.event(
                "review_submitted",
                trace_id=trace_id,
                case_id=case_id,
                review_id=payload["review_id"],
                draft_hash=payload.get("draft_hash"),
            )
            payload["trace_id"] = trace_id
            return payload

    def decide_review(self, case_id, reviewer, decision, trace_id=None):
        """Record an explicit person's decision on the exact pending draft.

        This method is reachable only from a deliberate UI/API action. No
        processing or routing path calls it automatically.
        """

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        self._check_id(case_id)
        if decision not in {"approved", "rejected"}:
            return {"error": "invalid_decision", "message": "Choose approve or reject."}
        if (
            not isinstance(reviewer, str)
            or not reviewer.strip()
            or len(reviewer.strip()) > 80
            or any(ord(character) < 32 for character in reviewer)
        ):
            return {
                "error": "reviewer_required",
                "message": "Enter the reviewer's name (1–80 characters).",
            }
        self._find(case_id, trace_id)
        with self._lock:
            entry = self._read().get(case_id) or {}
            draft = entry.get("draft")
            review = self._live_review(entry, trace_id)
            if not draft or not review:
                return {"error": "review_required", "message": "Submit this draft for review first."}
            if review.get("status") != "pending":
                return {"error": "review_already_decided", "message": "This review is no longer pending."}
            review_hash = review.get("hash") or review.get("draft_hash")
            if review_hash != digest(draft):
                return {"error": "draft_changed", "message": "The pending review does not match the current draft."}
            status, payload = self.client.decide_review(
                review.get("id") or review.get("review_id"),
                case_id,
                reviewer.strip(),
                decision,
                trace_id,
            )
        if status != 200 or not isinstance(payload, dict):
            return {
                "error": payload.get("error", "review_decision_failed")
                if isinstance(payload, dict)
                else "review_decision_failed",
                "message": "The review decision was not saved.",
            }
        self.observability.event(
            "human_review_decided",
            trace_id=trace_id,
            case_id=case_id,
            review_id=payload.get("review_id"),
            reviewer=reviewer.strip(),
            decision=decision,
        )
        payload["trace_id"] = trace_id

        # A rejection is terminal: never create a claim.
        if decision != "approved":
            return payload

        # Approval has already been persisted by the runtime.
        # Claim creation is automatic and uses file_record's
        # stable idempotency key and exact-draft validation.
        try:
            filing = self.file_record(case_id, trace_id)
        except Exception as exc:
            self.observability.event(
                "automatic_record_creation_failed",
                trace_id=trace_id,
                case_id=case_id,
                error_type=type(exc).__name__,
            )
            filing = {
                "error": "record_creation_pending",
                "message": "Approval saved; claim creation requires retry.",
            }

        payload["record_creation"] = filing
        payload["record_creation_status"] = (
            "created"
            if filing.get("status") in ("created", "replayed", "existing")
            else "pending_retry"
        )
        return payload

    def file_record(self, case_id, trace_id=None):
        """File only the exact human-approved draft using a stable request key."""

        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        self._check_id(case_id)
        self._find(case_id, trace_id)
        with self._lock:
            entry = self._read().get(case_id) or {}
            existing = self._live_record(case_id, trace_id)
            if existing:
                self.observability.metrics.increment("fnol_duplicate_prevented_total")
                self.observability.event(
                    "duplicate_prevented",
                    trace_id=trace_id,
                    case_id=case_id,
                    record_id=existing.get("record_id"),
                )
                return {"status": "existing", "record": existing}
            review = self._live_review(entry, trace_id)
            if not review or review.get("status") != "approved":
                return {"error": "approval_required", "message": "The exact draft has not been approved. No record was requested."}
            try:
                draft = json.loads(review["draft"]) if isinstance(review.get("draft"), str) else review.get("draft")
            except (TypeError, json.JSONDecodeError):
                return {"error": "invalid_review", "message": "The approved review has an unreadable draft."}
            if not isinstance(draft, dict) or digest(draft) != review.get("hash") or draft != entry.get("draft"):
                return {"error": "draft_changed", "message": "The current draft differs from the approved draft. No record was requested."}
            key = entry.get("idempotency_key") or "fnol-" + digest({
                "case_id": case_id, "review_id": review["id"], "hash": review["hash"],
            })
            state = self._read()
            state[case_id]["idempotency_key"] = key
            self._write(state)
            status, payload = self.client.create_record(
                case_id, review["id"], draft, key, trace_id
            )
        if status in (200, 201) and isinstance(payload, dict):
            with self._lock:
                state = self._read()
                if case_id in state:
                    state[case_id]["recorded_at"] = utc_now()
                    state[case_id]["record_trace_id"] = trace_id
                    self._write(state)
            self.observability.event(
                "record_created" if status == 201 else "record_replayed",
                trace_id=trace_id,
                case_id=case_id,
                record_id=payload.get("record_id") or payload.get("id"),
            )
            return {"status": "created" if status == 201 else "replayed", "record": payload}
        if status == 409 and isinstance(payload, dict) and payload.get("error") == "duplicate_case":
            self.observability.metrics.increment("fnol_duplicate_prevented_total")
            self.observability.event(
                "duplicate_prevented", trace_id=trace_id, case_id=case_id
            )
            found = self._live_record(case_id, trace_id)
            return {"status": "existing", "record": found or payload}
        return {"error": "record_failed", "message": "The record was not created.", "detail": payload if isinstance(payload, dict) else {}}

    def case_view(self, case_id, trace_id=None):
        """Combine local draft state with live review and record state for the UI."""

        self._check_id(case_id)
        trace_id = trace_id or "tr_" + uuid.uuid4().hex[:16]
        case = self._find(case_id, trace_id)
        state = self._read()
        entry = state.get(case_id) or {}
        review = self._live_review(entry, trace_id)
        record = self._live_record(case_id, trace_id)
        duplicate = self._duplicate_intake(case_id, record)
        return {
            "case_id": case_id,
            "documents": case.get("documents") or [],
            "status": self._status(case_id, state, record=record),
            "duplicate_intake": duplicate,
            "draft": entry.get("draft"),
            "warnings": entry.get("warnings") or [],
            "review": _public_review(review),
            "record": record,
            "activity": self._activity(entry, review, record, case_id=case_id),
        }

    def _activity(self, entry, review, record, case_id):
        """Build a concise handler timeline while retaining raw traces separately."""

        activity = []
        if entry.get("draft"):
            activity.append({
                "label": "Draft prepared",
                "at": entry.get("processed_at"),
                "trace_id": entry.get("process_trace_id"),
            })
        if entry.get("review_id"):
            activity.append({
                "label": "Submitted for human review",
                "at": entry.get("review_submitted_at"),
                "trace_id": entry.get("review_trace_id"),
                "review_id": entry.get("review_id"),
            })
        if review and review.get("status") in {"approved", "rejected"}:
            activity.append({
                "label": "Human approved exact draft" if review["status"] == "approved" else "Human rejected draft",
                "reviewer": review.get("reviewer"),
                "review_id": entry.get("review_id"),
            })
        if record:
            activity.append({
                "label": (
                    "Duplicate intake resolved to existing record"
                    if self._duplicate_intake(case_id, record)
                    else "Record created"
                ),
                "at": entry.get("recorded_at"),
                "trace_id": entry.get("record_trace_id"),
                "record_id": record.get("record_id"),
                "record_case_id": record.get("case_id"),
            })
        return activity

    def _documents(self, source_ids):
        """Load only the document IDs returned by the supplied case API."""

        loaded = {}
        for source_id in source_ids:
            self._check_id(source_id)
            text = self.evidence_store.document(source_id)
            if text is not None:
                loaded[source_id] = text
        return loaded

    def _find(self, case_id, trace_id=None):
        """Find a case through the API rather than the private fixture."""

        found = next(
            (
                case
                for case in self.client.list_cases(trace_id)
                if case.get("case_id") == case_id
            ),
            None,
        )
        if not found:
            raise KeyError(case_id)
        return found

    def _live_review(self, entry, trace_id=None):
        """Return the current runtime review associated with a local draft."""

        review_id = entry.get("review_id")
        if not review_id:
            return None
        status, payload = self.client.get_review(review_id, trace_id)
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("The review service is unavailable.")
        return payload

    def _live_record(self, case_id, trace_id=None):
        """Normalize the runtime's live logical-loss record for the UI."""

        row = self.client.get_record(case_id, trace_id)
        if not row:
            return None
        return {
            "record_id": row.get("record_id") or row.get("id"),
            "case_id": row.get("case_id"),
            "review_id": row.get("review_id"),
            "status": row.get("status") or "created",
        }

    def _duplicate_intake(self, case_id, record):
        """True when a live record exists but was filed under a different intake ID."""

        if not record or not isinstance(case_id, str) or not case_id:
            return False
        source = record.get("case_id")
        return isinstance(source, str) and bool(source) and source != case_id

    def _status(self, case_id, state, record=None):
        """Derive the handler-facing lifecycle status in precedence order."""

        if record is None:
            record = self._live_record(case_id)
        if record:
            return "record_created"
        review = self._live_review(state.get(case_id) or {})
        if review:
            return review.get("status") or "pending"
        if (state.get(case_id) or {}).get("draft"):
            return "draft_ready"
        return "new"

    def _read(self):
        """Read only current flat case entries from generated workbench state.

        Earlier prototypes stored data below a top-level ``cases`` object. That
        obsolete shape is ignored and disappears the next time state is saved.
        """

        return {
            case_id: entry
            for case_id, entry in self.state_store.read().items()
            if case_id != QUEUE_META_KEY
        }

    def _write(self, state):
        """Atomically persist generated drafts, review IDs, and request keys."""

        self.state_store.write(state)

    def _read_last_seen(self):
        if not self.supports_queue_cursor:
            return set()
        entry = self.state_store.read().get(QUEUE_META_KEY) or {}
        values = entry.get("last_seen_case_ids") or []
        return {value for value in values if isinstance(value, str)}

    def _merge_last_seen(self, case_ids):
        if not self.supports_queue_cursor or not case_ids:
            return
        current = self._read_last_seen()
        self.state_store.write({
            QUEUE_META_KEY: {
                "last_seen_case_ids": sorted(current | set(case_ids)),
                "updated_at": utc_now(),
            }
        })

    @staticmethod
    def _check_id(value):
        """Reject unsafe identifiers before they reach a filesystem or URL."""

        if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
            raise KeyError(value)


def _public_review(review):
    if not review:
        return None
    return {
        "review_id": review.get("id") or review.get("review_id"),
        "status": review.get("status"),
        "draft_hash": review.get("hash") or review.get("draft_hash"),
        "reviewer": review.get("reviewer"),
    }
