#!/bin/sh
# Start the runtime and the workbench. PostgreSQL migrations run only when a
# database URL or secret is configured. Both processes stay on loopback.
set -eu
cd /app

if [ -n "${FNOL_DATABASE_URL:-}" ] || [ -n "${FNOL_DATABASE_SECRET_ARN:-}" ]; then
  python -m tools.migrate
fi

runtime_port="${FNOL_RUNTIME_PORT:-8767}"
workbench_port="${FNOL_WORKBENCH_PORT:-8080}"

python tools/runtime.py serve --host 127.0.0.1 --port "$runtime_port" &
runtime_pid=$!
trap 'kill "$runtime_pid" 2>/dev/null || true' EXIT INT TERM

exec python -m workbench.server \
  --host 127.0.0.1 \
  --port "$workbench_port" \
  --api "http://127.0.0.1:${runtime_port}"
