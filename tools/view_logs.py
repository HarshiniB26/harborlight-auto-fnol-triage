"""Render structured workbench JSON logs as a concise human timeline."""

import argparse
import json
from pathlib import Path

EVENT_TEXT = {
    "server_started": lambda row: f"Workbench started on {row.get('host')}:{row.get('port')}",
    "processing_started": lambda row: "Draft preparation started",
    "evidence_documents_loaded": lambda row: (
        f"Loaded {row.get('loaded_count')}/{row.get('expected_count')} documents"
    ),
    "tool_call_started": lambda row: (
        f"{row.get('dependency')} API attempt {row.get('attempt')} started"
    ),
    "tool_call_completed": lambda row: (
        f"{row.get('dependency')} API succeeded on attempt {row.get('attempt')} "
        f"({row.get('duration_ms')} ms)"
    ),
    "tool_call_failed": lambda row: (
        f"{row.get('dependency')} API failed on attempt {row.get('attempt')} "
        f"with HTTP {row.get('status')}"
    ),
    "tool_call_retry_scheduled": lambda row: (
        f"Retry {row.get('dependency')} API after {row.get('backoff_ms')} ms"
    ),
    "circuit_opened": lambda row: f"{row.get('dependency')} circuit opened",
    "circuit_rejected": lambda row: f"{row.get('dependency')} circuit failed fast",
    "evidence_retrieval_completed": lambda row: (
        f"Evidence ready: policy={row.get('policy_available')}, claims={row.get('claims_available')}"
    ),
    "untrusted_document_instruction_ignored": lambda row: (
        "Ignored instruction-like content from untrusted evidence"
    ),
    "triage_decision_completed": lambda row: (
        f"Decision: {row.get('classification')} → {row.get('routing')}; "
        f"{row.get('citation_count')} citations"
    ),
    "processing_completed": lambda row: f"Draft completed in {row.get('duration_ms')} ms",
    "review_submitted": lambda row: f"Submitted review {row.get('review_id')}",
    "record_created": lambda row: f"Created record {row.get('record_id')}",
    "record_replayed": lambda row: f"Replayed record {row.get('record_id')}",
    "duplicate_prevented": lambda row: f"Prevented duplicate record {row.get('record_id') or ''}".rstrip(),
}


def main():
    """Print relevant events grouped visually by request trace."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--case", dest="case_id", help="show only one case ID")
    args = parser.parse_args()

    previous_trace = None
    shown = 0
    skipped = 0
    for line in args.log.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if args.case_id and row.get("case_id") != args.case_id:
            continue
        formatter = EVENT_TEXT.get(row.get("event"))
        if not formatter:
            continue
        trace_id = row.get("trace_id") or "service"
        if trace_id != previous_trace:
            print(f"\nTRACE {trace_id}  CASE {row.get('case_id', '-')}")
            previous_trace = trace_id
        timestamp = row.get("timestamp", "")
        print(f"  {timestamp}  {formatter(row)}")
        shown += 1
    if not shown:
        print("No matching structured events found.")
    if skipped:
        print(f"\nIgnored {skipped} non-JSON startup line(s).")


if __name__ == "__main__":
    main()
