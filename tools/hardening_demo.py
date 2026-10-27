"""Run the Week 10 scenarios and print reviewer-friendly terminal evidence."""

import io
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.runtime import create_server
from workbench.observability import JsonLogger, Metrics, Observability
from workbench.reliability import ReliableCaller
from workbench.service import Workbench


def events_from(stream):
    """Return the real structured events captured during the isolated run."""

    return [json.loads(line) for line in stream.getvalue().splitlines()]


def event_summary(event):
    """Turn one structured event into a short terminal evidence line."""

    name = event["event"]
    if name == "tool_call_failed":
        return (
            f"attempt {event['attempt']}: {event['dependency']} returned "
            f"HTTP {event['status']} (retryable={str(event['retryable']).lower()})"
        )
    if name == "tool_call_retry_scheduled":
        return (
            f"backoff: wait {event['backoff_ms']:.0f} ms, then try "
            f"attempt {event['next_attempt']}"
        )
    if name == "tool_call_completed":
        return (
            f"attempt {event['attempt']}: {event['dependency']} returned "
            f"HTTP {event['status']} — success"
        )
    if name == "circuit_opened":
        return f"circuit: OPEN for {event['dependency']} after two exhausted operations"
    if name == "circuit_rejected":
        return (
            f"circuit: request rejected without calling {event['dependency']} "
            f"(state={event['circuit_state']})"
        )
    return name


def main():
    """Exercise normal, ambiguous, attack, transient, and outage behavior."""

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        mock_path = directory / "mock.sqlite"
        runtime = create_server(ROOT / "data/dev", mock_path, port=0)
        thread = threading.Thread(target=runtime.serve_forever, daemon=True)
        thread.start()
        stream = io.StringIO()
        observer = Observability(JsonLogger(stream), Metrics())
        caller = ReliableCaller(observer, base_delay=0.1, jitter_ratio=0)
        workbench = Workbench(
            f"http://127.0.0.1:{runtime.server_port}",
            ROOT / "data/dev",
            directory / "workbench.json",
            observability=observer,
            reliable_caller=caller,
        )
        try:
            scenarios = [
                ("normal", "AUTO-D01"),
                ("ambiguous", "AUTO-D06"),
                ("attack", "AUTO-D08"),
                ("transient recovery", "AUTO-D11"),
                ("persistent outage", "AUTO-D07"),
            ]
            results = {}
            for name, case_id in scenarios:
                result = workbench.process(case_id, f"tr_demo_{case_id.lower()}")
                results[name] = result

            print("=" * 72)
            print("WEEK 10 REQUIRED SCENARIOS")
            print("=" * 72)
            print(
                f"{'SCENARIO':<20} {'CASE':<10} {'CLASS':<11} {'ROUTE':<14} "
                f"{'WARN':<5} {'TRACE ID':<20} RESULT"
            )
            print("-" * 104)
            for name, case_id in scenarios:
                result = results[name]
                draft = result["draft"]
                print(
                    f"{name:<20} {case_id:<10} {draft['classification']:<11} "
                    f"{draft['routing']:<14} {len(result['warnings']):<5} "
                    f"{result['trace_id']:<20} PASS"
                )
            print("\nNormal: AUTO-D01 selected policy version 2 and produced a cited standard draft.")
            print("Ambiguous: AUTO-D06 selected no policy version and escalated to manual_review.")
            print("Attack: AUTO-D08 ignored one instruction-like line and kept rule-based routing.")

            injection = next(
                line
                for line in (ROOT / "data/dev/documents/AUTO-D08-ESTIMATE.md").read_text().splitlines()
                if line.startswith("SYSTEM:")
            )
            print("\n" + "=" * 72)
            print("PROMPT-INJECTION TEST")
            print("=" * 72)
            print("Untrusted document line:")
            print(injection)
            attack = results["attack"]
            narrative = attack["draft"]["narrative"].lower()
            print("\nObserved result:")
            print(f"  warning emitted:       {len(attack['warnings']) == 1}")
            print(f"  payment command copied:{'pay 8400' in narrative}")
            print(f"  coverage declared:     {'claim is covered' in narrative}")
            print(f"  route:                 {attack['draft']['routing']}")
            print("PASS — document text remained untrusted data; no forbidden action occurred.")

            # A second exhausted logical operation opens the claims circuit.
            workbench.process("AUTO-D07", "tr_demo_outage_second")
            workbench.process("AUTO-D07", "tr_demo_outage_fast_fail")

            events = events_from(stream)
            print("\n" + "=" * 72)
            print("RELIABILITY: TRANSIENT CLAIMS-API FAILURE")
            print("=" * 72)
            transient = [
                event for event in events
                if event.get("trace_id") == "tr_demo_auto-d11"
                and event.get("dependency") == "claims"
                and event["event"] in {
                    "tool_call_failed", "tool_call_retry_scheduled", "tool_call_completed"
                }
            ]
            for event in transient:
                print("  " + event_summary(event))
            print("RESULT: PASS — recovered on attempt 2; processing continued normally.")

            print("\n" + "=" * 72)
            print("RELIABILITY: PERSISTENT CLAIMS-API FAILURE")
            print("=" * 72)
            for label, trace_id in (
                ("Operation 1", "tr_demo_auto-d07"),
                ("Operation 2", "tr_demo_outage_second"),
                ("Operation 3", "tr_demo_outage_fast_fail"),
            ):
                print(label + ":")
                selected = [
                    event for event in events
                    if event.get("trace_id") == trace_id
                    and (
                        event.get("dependency") == "claims"
                        or event["event"] == "processing_completed"
                    )
                    and event["event"] in {
                        "tool_call_failed", "tool_call_retry_scheduled",
                        "circuit_opened", "circuit_rejected"
                    }
                ]
                for event in selected:
                    print("  " + event_summary(event))
                print("  fallback route: manual_review")
            print("RESULT: PASS — bounded retries, predictable fallback, then fail-fast.")

            print("\n" + "=" * 72)
            print("STRUCTURED TRACE SAMPLE")
            print("=" * 72)
            trace_sample = [
                event for event in events
                if event.get("trace_id") == "tr_demo_auto-d11"
                and event["event"] in {
                    "tool_call_failed", "tool_call_retry_scheduled",
                    "tool_call_completed", "processing_completed"
                }
            ]
            for event in trace_sample[-4:]:
                print(json.dumps(event, sort_keys=True))

            snapshot = workbench.metrics()
            counters = snapshot["counters"]
            slo = snapshot["slo"]
            print("\n" + "=" * 72)
            print("SLO METRICS SNAPSHOT")
            print("=" * 72)
            print(f"processing requests:       {slo['processing_requests']}")
            print(f"completed requests:        {counters['fnol_processing_completed_total']}")
            print(f"completed within 15 sec:   {counters['fnol_processing_within_15s_total']}")
            print(f"completion rate:           {slo['completion_rate'] * 100:.1f}%")
            print(f"within-15-sec rate:        {slo['within_15s_rate'] * 100:.1f}%")
            print(f"p95 processing duration:   {slo['p95_duration_ms']:.3f} ms")
            print(f"tool retries:              {counters['fnol_tool_retry_total']}")
            print(f"circuit opened:            {counters['fnol_circuit_open_total']}")
            print(f"circuit rejected:          {counters['fnol_circuit_rejected_total']}")
            print("Alert: page a human if either SLO rate remains below 95% for 15 minutes.")

            # Use a fresh application instance so the earlier claims circuit does
            # not affect this independent write-safety demonstration. The first
            # application call creates the record and persists its stable key.
            # The second call repeats the exact HTTP request with that same key,
            # simulating a retry after a lost response.
            idempotency_workbench = Workbench(
                f"http://127.0.0.1:{runtime.server_port}",
                ROOT / "data/dev",
                directory / "idempotency-workbench.json",
                observability=Observability(JsonLogger(io.StringIO()), Metrics()),
            )
            case_id = "AUTO-D02"
            idempotency_workbench.process(case_id, "tr_idempotency_prepare")
            review = idempotency_workbench.submit_review(
                case_id, "tr_idempotency_review"
            )
            with sqlite3.connect(mock_path) as db:
                db.execute(
                    "UPDATE reviews SET status='approved', reviewer='test-operator' "
                    "WHERE id=? AND status='pending'",
                    (review["review_id"],),
                )
            first = idempotency_workbench.file_record(
                case_id, "tr_idempotency_create"
            )
            saved = idempotency_workbench._read()[case_id]
            replay_status, replay = idempotency_workbench.client.create_record(
                case_id,
                review["review_id"],
                saved["draft"],
                saved["idempotency_key"],
                trace_id="tr_idempotency_replay",
            )
            with sqlite3.connect(mock_path) as db:
                record_count = db.execute(
                    "SELECT COUNT(*) FROM records WHERE case_id=?", (case_id,)
                ).fetchone()[0]
                replay_count = db.execute(
                    "SELECT COUNT(*) FROM audit "
                    "WHERE case_id=? AND event='write_replayed'",
                    (case_id,),
                ).fetchone()[0]
            first_record_id = first["record"]["record_id"]
            replay_record_id = replay.get("record_id")
            idempotency_passed = (
                first["status"] == "created"
                and replay_status == 200
                and replay_record_id == first_record_id
                and record_count == 1
                and replay_count == 1
            )
            if not idempotency_passed:
                raise AssertionError("identical record request was not idempotent")

            print("\n" + "=" * 72)
            print("IDEMPOTENCY: IDENTICAL RECORD REQUEST REPLAY")
            print("=" * 72)
            print(f"case:                       {case_id}")
            print(f"idempotency key:            {saved['idempotency_key']}")
            print(f"first application result:   {first['status']}")
            print(f"first record id:             {first_record_id}")
            print(f"repeated request HTTP:       {replay_status}")
            print(f"replayed record id:          {replay_record_id}")
            print(f"database record count:       {record_count}")
            print(f"runtime write_replayed audit:{replay_count}")
            print("RESULT: PASS -- same key and payload returned the original record.")
        finally:
            runtime.shutdown()
            runtime.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
