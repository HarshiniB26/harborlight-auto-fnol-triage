"""Operator CLI. Approval stays on tools/runtime.py and is never called from here."""

import argparse
import json
import os
from pathlib import Path

from harborlight.service import App, serve

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Harborlight FNOL triage copilot")
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "dev")
    parser.add_argument("--enterprise", default=os.environ.get("ENTERPRISE_URL", "http://127.0.0.1:8767"))
    parser.add_argument("--traces", type=Path, default=ROOT / "reports" / "traces.jsonl")
    parser.add_argument("--retry-base", type=float, default=float(os.environ.get("HARBORLIGHT_RETRY_BASE_SECONDS", "0.05")))
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft")
    draft.add_argument("--case-id")
    draft.add_argument("--out", type=Path)
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve_cmd.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    app = App(args.data, args.enterprise, args.traces, retry_base=args.retry_base)
    if args.command == "serve":
        serve(app, args.host, args.port)
        return
    results = app.draft(args.case_id)
    raw = json.dumps(results, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(raw, encoding="utf-8")
    print(raw)


if __name__ == "__main__":
    main()
