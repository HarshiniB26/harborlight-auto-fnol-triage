# Operator runbook

## Fresh setup

```sh
uv sync --locked
uv run --locked python tools/validate_package.py
uv run --locked python tools/runtime.py serve --port 8767
```

In another shell:

```sh
uv run --locked python -m harborlight draft --out submissions/results.json
uv run --locked python tools/evaluate.py --data data/dev --gold feedback/dev_gold.json --submission submissions/results.json --report reports/dev.json
```

The copilot listens on `http://127.0.0.1:8080` with `uv run --locked python -m harborlight serve`.

Container path: `docker compose -f docker-compose.app.yml up --build`. The mock stays on port 8767. The copilot listens on port 8080 inside the network.

## Health

`GET /health` on the copilot checks the enterprise mock. `GET /metrics` is the counter text used for the draft SLO. Structured events go to stdout and `reports/traces.jsonl`. Private tokens are redacted before either sink.

## Alert

Page when draft failures or enterprise health failures persist for five minutes, or when claims-tool failures are still failing after three attempts on more than one case. Do not page on a single retry. A single 503 is the transient path the retry budget exists for.

## Checkout-equivalent: draft success drops

1. Open `GET /health` on the mock and on the copilot. If the mock is down, restart it with the same `--state` path. Do not reset state while a review is pending unless the rehearsal is being discarded.
2. Read the latest `tool_read_failed` events. Three attempts and then `manual_review` is the expected claims-outage path. Creating a record in that state is not allowed.
3. If the circuit for that case is open, wait for the reset window or start a new process. Do not raise the attempt limit past three.
4. If a record write returns `duplicate_case`, stop. Report the existing record id to the handler.
5. If a write returns `approval_missing_or_draft_changed`, the draft changed after approval or nobody approved it. Submit the current draft again and ask the human to approve that review id. Do not edit the approved body.

## Human approval

```sh
uv run --locked python tools/runtime.py pending
uv run --locked python tools/runtime.py approve REVIEW_ID --reviewer NAME
```

Then `POST /v1/records` with the same case id and review id. Rejection uses `reject`. The copilot has no approve endpoint.

## Reset

Stop clients first. `uv run --locked python tools/runtime.py reset --confirm` clears reviews, records, idempotency keys, and the mock attempt counters.
