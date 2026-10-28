"""Trigger an on-demand sequential queue preparation run."""

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--case-id", action="append", dest="case_ids")
    parser.add_argument("--trace-id")
    args = parser.parse_args()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if args.trace_id:
        headers["X-Trace-ID"] = args.trace_id
    body = {"case_ids": args.case_ids} if args.case_ids else {}
    request = Request(
        args.url.rstrip("/") + "/api/queue/prepare",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=900) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as error:
        payload = json.loads(error.read().decode() or "{}")
        print(json.dumps(payload, indent=2))
        raise SystemExit(1) from error
    except (URLError, TimeoutError, OSError) as error:
        raise SystemExit(f"Workbench unreachable: {error}") from error
    print(json.dumps(payload, indent=2))
    if payload.get("error") or payload.get("status") == "completed_with_errors":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
