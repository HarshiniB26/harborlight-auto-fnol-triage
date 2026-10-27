"""Download the exact output-contract array from a running workbench."""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


def main():
    """Export all prepared drafts atomically or explain which cases are missing."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, default=Path("submissions/results.json"))
    args = parser.parse_args()
    try:
        with urlopen(args.url.rstrip("/") + "/api/results", timeout=10) as response:
            results = json.loads(response.read().decode())
    except HTTPError as error:
        detail = json.loads(error.read().decode())
        missing = ", ".join(detail.get("missing_case_ids") or [])
        raise SystemExit(f"Export unavailable: {detail.get('message', detail.get('error'))} {missing}")

    if not isinstance(results, list):
        raise SystemExit("Workbench returned an invalid results payload.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(results, indent=2) + "\n")
    temporary.replace(args.output)
    print(f"Exported {len(results)} drafts to {args.output}")


if __name__ == "__main__":
    main()
