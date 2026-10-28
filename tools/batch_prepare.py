"""Trigger on-demand queue prepare against a running workbench."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    """POST /api/queue/prepare and print the per-case summary."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        default=None,
        help="Optional case ID to include (repeatable). Default: entire queue.",
    )
    parser.add_argument("--trace-id", default=None)
    args = parser.parse_args()
    body = {}
    if args.case_ids:
        body["case_ids"] = args.case_ids
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if args.trace_id:
        headers["X-Trace-ID"] = args.trace_id
    request = Request(
        args.url.rstrip("/") + "/api/queue/prepare",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode())
            trace = response.headers.get("X-Trace-ID")
    except HTTPError as error:
        detail = error.read().decode()
        try:
            payload = json.loads(detail) if detail else {"error": "http_error"}
        except json.JSONDecodeError:
            payload = {"error": "http_error", "message": detail}
        print(json.dumps(payload, indent=2))
        raise SystemExit(1)
    except (URLError, TimeoutError, OSError) as error:
        raise SystemExit(f"Workbench unreachable: {error}") from error

    print(json.dumps(payload, indent=2))
    if trace:
        print(f"trace_id={trace}", file=sys.stderr)
    if payload.get("status") == "completed_with_errors" or payload.get("error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
