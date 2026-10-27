"""Fail when source files contain strings that resemble live credentials."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "__pycache__"}
PATTERNS = {
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "LLM/API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|aws_secret_access_key|password|secret)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    ),
}


def main():
    """Scan text files while allowing the package's named synthetic fixtures."""

    findings = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        scanned += 1
        for line_number, line in enumerate(text.splitlines(), 1):
            if "SYNTHETIC-PRIVATE-" in line:
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}: {name}")

    if findings:
        print("Credential scan failed:")
        print("\n".join(findings))
        return 1
    print(f"Credential scan passed: {scanned} text files; no likely live credentials found.")
    print("Known SYNTHETIC-PRIVATE-* training fixtures were allowed and remain restricted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
