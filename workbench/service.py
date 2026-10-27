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
    ):
        self.client = MockClient(api_url)
        self.data_dir = Path(data_dir)
        self.state_path = Path(state_path)
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
            self._file_if_approved(case_id)
            state = self._read()
            entry = state.get(case_id) or {}
            draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else None
            summary = {
                "case_id": case_id,
                "documents": case.get("documents") or [],
                "status": self._status(case_id, state),
            }
            if draft:
                summary["classification"] = draft.get("classification")
                summary["routing"] = draft.get("routing")
                summary["processed_at"] = entry.get("processed_at")
            listed.append(summary)
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
                review = self._live_review(entry, trace_id)
                if review and review.get("status") in ("pending", "approved") and not self._live_record(case_id, trace_id):
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
                self.observability.event(
                    "evidence_retrieval_completed",
                    trace_id=trace_id,
                    case_id=case_id,
                    policy_available=isinstance(policy, dict),
                    claims_available=isinstance(claims, dict),
                )
                draft, warnings = build_draft(case, documents, policy, claims, self.data_dir)
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
            if status != 201 or not isinstance(payload, dict):
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
        self._file_if_approved(case_id, trace_id)
        state = self._read()
        entry = state.get(case_id) or {}
        review = self._live_review(entry, trace_id)
        record = self._live_record(case_id, trace_id)
        documents = self._documents(case.get("documents") or [])
        return {
            "case_id": case_id,
            "documents": case.get("documents") or [],
            "sources": [
                {"source_id": source_id, "text": documents[source_id]}
                for source_id in case.get("documents") or []
                if source_id in documents
            ],
            "status": self._status(case_id, state),
            "draft": entry.get("draft"),
            "warnings": entry.get("warnings") or [],
            "review": _public_review(review),
            "record": record,
            "activity": self._activity(entry, review, record),
        }

    def _activity(self, entry, review, record):
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
                "label": "Record created",
                "at": entry.get("recorded_at"),
                "trace_id": entry.get("record_trace_id"),
                "record_id": record.get("record_id"),
            })
        return activity

    def _documents(self, source_ids):
        """Load only the document IDs returned by the supplied case API."""

        loaded = {}
        for source_id in source_ids:
            self._check_id(source_id)
            path = self.data_dir / "documents" / f"{source_id}.md"
            if path.is_file():
                loaded[source_id] = path.read_text()
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

    def _file_if_approved(self, case_id, trace_id=None):
        """File the exact draft once a reviewer has approved it outside the app."""

        entry = self._read().get(case_id) or {}
        if not entry.get("draft") or not entry.get("review_id"):
            return None
        if self._live_record(case_id, trace_id):
            return None
        review = self._live_review(entry, trace_id)
        if not review or review.get("status") != "approved":
            return None
        return self.file_record(case_id, trace_id)

    def _status(self, case_id, state):
        """Derive the handler-facing lifecycle status in precedence order."""

        if self._live_record(case_id):
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

        if not self.state_path.is_file():
            return {}
        stored = json.loads(self.state_path.read_text())
        if not isinstance(stored, dict):
            return {}
        return {
            case_id: entry
            for case_id, entry in stored.items()
            if case_id != "cases"
            and isinstance(entry, dict)
            and isinstance(entry.get("draft"), dict)
        }

    def _write(self, state):
        """Atomically persist generated drafts, review IDs, and request keys."""

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(self.state_path)

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
