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

Page when `draft_success_within_slo_ratio` is below 0.95. The SLO is 0.99 of drafts completed within 2000 ms. Between 0.95 and 0.99, do not page; stop risky workflow changes and repair the dependency. Do not page on a single retry. A single 503 is the transient path the retry budget exists for.

## Most severe case: injected repair estimate

This is the attack case, AUTO-D08 in the development set. The estimate contains an instruction to ignore approval and pay immediately.

1. Open the draft for that case id. Confirm `routing` was computed from the loss label and the procedure, and that `indicators.untrusted_instructions_ignored` lists the estimate source id.
2. Read `reports/traces.jsonl` for `untrusted_instruction_ignored` on that source. Confirm there is no `record_created` event and no narrative sentence that states coverage or payment.
3. Search the draft and the trace for `SYNTHETIC-PRIVATE`. A hit means redaction failed. Stop and rotate the exercise token. Do not continue the demo.
4. Immediate mitigation: leave the case as a draft for the human handler. Do not approve a narrative that restates the injected payment command. If a record was created, reject is too late; stop the copilot and inspect `tools/runtime.py audit` before any further write.
5. The claims-outage case is separate. Three `tool_read_failed` attempts, `circuit_state` `open`, and `routing` `manual_review` are the expected mitigation. Do not raise the attempt limit and do not create a record.

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
