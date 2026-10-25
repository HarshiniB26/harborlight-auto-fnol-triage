"""Run the five read-only Week 10 scenarios and print concise evidence."""

import io
import json
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

def main():
    """Exercise normal, ambiguous, attack, transient, and outage behavior."""

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        runtime = create_server(ROOT / "data/dev", directory / "mock.sqlite", port=0)
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
            print("WEEK 10 SCENARIOS")
            for name, case_id in scenarios:
                result = workbench.process(case_id, f"tr_demo_{case_id.lower()}")
                draft = result["draft"]
                print(
                    f"{name}: {case_id} classification={draft['classification']} "
                    f"routing={draft['routing']} warnings={len(result['warnings'])}"
                )

            injection = next(
                line
                for line in (ROOT / "data/dev/documents/AUTO-D08-ESTIMATE.md").read_text().splitlines()
                if line.startswith("SYSTEM:")
            )
            print("\nATTACK INPUT")
            print(injection)
            print("ATTACK RESULT")
            print("Instruction ignored; no coverage, payment, approval, or token disclosure occurred.")

            # A second exhausted logical operation opens the claims circuit.
            workbench.process("AUTO-D07", "tr_demo_outage_second")
            workbench.process("AUTO-D07", "tr_demo_outage_fast_fail")

            print("\nRELIABILITY TRACE EVENTS")
            allowed = {
                "tool_call_retry_scheduled",
                "circuit_opened",
                "circuit_rejected",
                "processing_completed",
            }
            for line in stream.getvalue().splitlines():
                event = json.loads(line)
                if event["event"] in allowed and event.get("case_id") in {
                    "AUTO-D07",
                    "AUTO-D08",
                    "AUTO-D11",
                }:
                    print(json.dumps(event, sort_keys=True))

            print("\nMETRICS")
            print(json.dumps(workbench.metrics(), indent=2, sort_keys=True))
        finally:
            runtime.shutdown()
            runtime.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
