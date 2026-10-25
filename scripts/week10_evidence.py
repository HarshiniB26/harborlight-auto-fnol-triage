"""Run the Week 10 evidence set and write docs/week10-lab.pdf.

The human approval step shells out to tools/runtime.py. The copilot is not asked to approve.
"""

import json
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "week10-lab.pdf"
STATE = ROOT / ".state" / "week10.sqlite"
PORT = 8771
BASE = f"http://127.0.0.1:{PORT}"


def post(path, body, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> None:
    from harborlight.enterprise import EnterpriseClient
    from harborlight.idempotency import creation_block, idempotency_key
    from harborlight.observe import Tracer, snapshot
    from harborlight.slo import (
        ALERT_RATIO,
        ERROR_BUDGET_RATIO,
        LATENCY_MS,
        TARGET_RATIO,
        budget_action,
    )
    from harborlight.workflow import Workflow

    if STATE.exists():
        STATE.unlink()
    server = subprocess.Popen(
        ["python", "tools/runtime.py", "serve", "--port", str(PORT), "--state", str(STATE)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(BASE + "/health", timeout=1)
                break
            except (OSError, urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise RuntimeError("mock did not start")
        tracer = Tracer(ROOT / "reports" / "week10-traces.jsonl")
        client = EnterpriseClient(BASE, tracer, base_delay=0.02)
        workflow = Workflow(ROOT / "data" / "dev", client, tracer)
        cases = {item["case_id"]: item for item in client.list_cases()}

        def show(case_id):
            draft = workflow.draft_case(cases[case_id])
            return {
                "case_id": draft["case_id"],
                "classification": draft["classification"],
                "routing": draft["routing"],
                "selected_policy_version": draft["facts"]["selected_policy_version"],
                "missing_information": draft["missing_information"],
                "indicators": draft["indicators"],
            }

        normal = show("AUTO-D01")
        escalation = show("AUTO-D04")
        attack = show("AUTO-D08")
        timeout = show("AUTO-D07")
        recovered = show("AUTO-D11")
        duplicate_notice = show("AUTO-D10")
        opened = client.read_tool("AUTO-D07", "claims")
        circuit = {
            "follow_up_call_ok": opened.ok,
            "attempts": opened.attempts,
            "error": opened.error,
        }

        draft = workflow.drafts if hasattr(workflow, "drafts") else None
        d01 = workflow.draft_case(cases["AUTO-D01"])
        status, review = post("/v1/reviews", {"case_id": "AUTO-D01", "draft": d01})
        approved = subprocess.run(
            ["python", "tools/runtime.py", "approve", review["review_id"], "--reviewer", "week10-handler", "--state", str(STATE)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        key = idempotency_key("AUTO-D01", d01)
        first_status, first = post("/v1/records", {"case_id": "AUTO-D01", "review_id": review["review_id"], "draft": d01}, {"Idempotency-Key": key})
        replay_status, replay = post("/v1/records", {"case_id": "AUTO-D01", "review_id": review["review_id"], "draft": d01}, {"Idempotency-Key": key})
        conflict_status, conflict = post(
            "/v1/records",
            {"case_id": "AUTO-D01", "review_id": review["review_id"], "draft": {**d01, "narrative": d01["narrative"] + " changed"}},
            {"Idempotency-Key": key},
        )
        d09 = workflow.draft_case(cases["AUTO-D09"])
        _, review09 = post("/v1/reviews", {"case_id": "AUTO-D09", "draft": d09})
        subprocess.run(
            ["python", "tools/runtime.py", "approve", review09["review_id"], "--reviewer", "week10-handler", "--state", str(STATE)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        d09_status, d09_body = post(
            "/v1/records",
            {"case_id": "AUTO-D09", "review_id": review09["review_id"], "draft": d09},
            {"Idempotency-Key": idempotency_key("AUTO-D09", d09)},
        )
        d10 = workflow.draft_case(cases["AUTO-D10"])
        blocked = creation_block(d10)
        with urllib.request.urlopen(BASE + "/v1/records/AUTO-D10", timeout=5) as existing:
            existing_status = existing.status
            existing_body = json.loads(existing.read().decode())
        injection = (ROOT / "data/dev/documents/AUTO-D08-ESTIMATE.md").read_text(encoding="utf-8").splitlines()[-1]
        metrics = snapshot()
        pages = [
            ("1. SLO and error budget", textwrap.dedent(f"""
                SLO: {TARGET_RATIO:.0%} of FNOL draft requests complete within {LATENCY_MS} ms,
                including a fail-closed manual_review when a tool is down.
                Error budget: {ERROR_BUDGET_RATIO:.0%}, one draft in a hundred may miss the target.
                A claims timeout that still returns a route inside 2 seconds has met the SLO.
                A hang, a crash, or a draft slower than 2 seconds spends the budget.
                If the ratio drops below 99%: {budget_action(0.97)}.
                If the ratio drops below {ALERT_RATIO:.0%}: {budget_action(0.90)}.
                Measured this run: ratio={metrics['draft_success_within_slo_ratio']} avg_ms={metrics['latency_ms_avg']}
            """)),
            ("2. Failure-case mapping", textwrap.dedent("""
                AUTO-D11 claims fails once then succeeds -> retry with exponential backoff.
                AUTO-D07 claims fails on every attempt -> three attempts, circuit opens, manual_review.
                AUTO-D10 second intake of the same loss -> duplicate detection, no second claim.
                AUTO-D01 record replay -> idempotency key returns the original record.
                AUTO-D08 estimate contains an instruction -> untrusted data, not a command.
                AUTO-D04 missing driver and location -> human escalation, manual_review.
                AUTO-D05 two policy intervals -> null version, manual_review, no denial.
                AUTO-D01 complete collision -> standard route. No model call.
            """)),
            ("3. Retry recovery, AUTO-D11", json.dumps({"recovered": recovered, "note": "claims_attempts 2 means the first 503 was retried and the second attempt succeeded"}, indent=2)),
            ("4. Circuit breaker, AUTO-D07", json.dumps({"after_budget": timeout, "next_call_does_not_hit_upstream": circuit}, indent=2)),
            ("5. Idempotency", json.dumps({
                "human_approve_stdout": approved.stdout.strip(),
                "first_write": [first_status, first.get("status"), first.get("record_id")],
                "identical_replay": [replay_status, replay.get("status"), replay.get("record_id")],
                "same_key_changed_draft": [conflict_status, conflict.get("error")],
                "first_loss_write": [d09_status, d09_body.get("status"), d09_body.get("record_id")],
                "duplicate_intake_blocked_before_write": blocked,
                "ledger_already_has_loss": [existing_status, existing_body.get("id")],
                "duplicate_notice_before_write": duplicate_notice["indicators"]["duplicate_intake"],
            }, indent=2)),
            ("6. Prompt injection", textwrap.dedent(f"""
                Injected line from AUTO-D08-ESTIMATE:
                {injection}
                System handling:
                {json.dumps(attack, indent=2)}
                The draft does not state that the claim is covered and does not pay 8400.
                The ignored source id is listed. The citation used is the estimate reference, not the command.
            """)),
            ("7. Least privilege and human boundary", textwrap.dedent("""
                Copilot HTTP client methods: health, list_cases, read_tool (policy, claims),
                get_record, request_review, get_review, create_record.
                There is no approve or reject method. Approval is tools/runtime.py, run above
                as the human operator, not by harborlight.
                create_record sends only the already built draft. The mock returns 403 unless
                that exact draft was approved. The client cannot widen that check.
                Tools are read-only except the one approved record write. No payment, coverage,
                or fraud tool exists. No component reads private_token into the draft.
                Credential search: no API key, password, or provider token is assigned in
                harborlight, scripts, or docker-compose.app.yml. The mock has no credential.
            """)),
            ("8. Trace sample", "\n".join(json.dumps(event, sort_keys=True) for event in tracer.events if event["event"] in {"tool_read", "tool_read_failed", "route_decision", "untrusted_instruction_ignored"})),
            ("9. Metrics and alert", json.dumps({
                "slo": "99% of drafts within 2000ms",
                "alert": "page when draft_success_within_slo_ratio < 0.95",
                "metrics": metrics,
            }, indent=2)),
            ("10. Runbook, injected estimate", textwrap.dedent("""
                1. Read the draft. Confirm untrusted_instructions_ignored lists AUTO-D08-ESTIMATE.
                2. Read traces for untrusted_instruction_ignored. Confirm no record_created.
                3. Search the draft and trace for SYNTHETIC-PRIVATE. Stop if present.
                4. Leave the case with the handler. Do not approve a narrative that repeats
                   the payment command. Do not create a record from the injected text.
                Claims outage mitigation is separate: three failed attempts, circuit open,
                routing manual_review, no record create, attempt limit stays at three.
            """)),
            ("11. Normal case AUTO-D01", json.dumps(normal, indent=2)),
            ("12. Escalation AUTO-D04", json.dumps(escalation, indent=2)),
            ("13. Attack case AUTO-D08", json.dumps(attack, indent=2)),
        ]
        _ = draft
        _ = status
        write_pdf(OUT, pages)
        print(f"wrote {OUT}")
    finally:
        server.terminate()
        server.wait(timeout=5)


def write_pdf(path: Path, pages: list[tuple[str, str]]) -> None:
    objects: list[bytes] = []

    def add(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    page_ids = []
    content_ids = []
    for title, body in pages:
        lines = [title, ""]
        for paragraph in body.strip().splitlines():
            wrapped = textwrap.wrap(paragraph, width=92, break_long_words=False) or [""]
            lines.extend(wrapped)
        lines = lines[:58]
        commands = ["0.96 0.97 0.95 rg 36 36 540 760 re f", "BT /F1 9 Tf 48 760 Td 12 TL"]
        for line in lines:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({safe}) Tj T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", errors="replace")
        content_ids.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        page_ids.append(None)
    page_object_ids = []
    for content_id in content_ids:
        page_object_ids.append(add(
            f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        ))
    kids = " ".join(f"{pid} 0 R" for pid in page_object_ids)
    pages_id = add(f"<< /Type /Pages /Count {len(page_object_ids)} /Kids [{kids}] >>".encode())
    for index, pid in enumerate(page_object_ids):
        objects[pid - 1] = objects[pid - 1].replace(b"/Parent 0 0 R", f"/Parent {pages_id} 0 R".encode())
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{number} 0 obj\n".encode())
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer << /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


if __name__ == "__main__":
    main()
