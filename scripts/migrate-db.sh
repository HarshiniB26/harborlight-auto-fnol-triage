#!/bin/bash
# Apply PostgreSQL migrations using FNOL_DATABASE_URL or the master secret.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python -m tools.migrate
