# Personal Auto FNOL Triage Copilot

A local claims-handler workbench that prepares a cited First Notice of Loss
(FNOL) draft, recommends routing, requires human approval, and creates one
idempotent mock record. All supplied people, policies, claims, and currency are
fictional training data.

## What we built

- A small browser UI for the case queue, drafts, citations, reviews, and records.
- A backend that calls the supplied `/v1` APIs instead of reading
  `data/dev/dataset.json` directly.
- Deterministic extraction, policy-period selection, conflict detection, and
  routing (`standard`, `urgent`, `specialist`, or `manual_review`).
- Exact source citations and output matching `docs/OUTPUT_CONTRACT.md`.
- A hard human boundary: the application cannot approve its own draft, and a
  changed draft needs new approval.
- Retries with exponential backoff, circuit breakers, and idempotent writes.
- Prompt-injection isolation, `private_token` removal, structured logs, trace
  IDs, health/readiness endpoints, and SLO metrics.

## Application flow

```text
Browser UI (port 8080)
        |
        v
workbench backend
        |
        +--> supplied runtime APIs (port 8767)
        |       cases, policy, claims, reviews, records
        |
        +--> deterministic rules and cited draft
        |
        +--> human approval through the supplied operator CLI
        |
        +--> approved, idempotent mock record
```

The supplied runtime is an external dependency simulator. `workbench/server.py`
is our browser-facing backend; having both does not mean the frontend calls two
backends. The browser calls only the workbench.

## Project structure

```text
workbench/
  server.py          browser API and static-file server
  service.py         end-to-end workflow and human boundary
  client.py          supplied runtime API adapter
  rules.py           extraction, reconciliation, and routing rules
  draft.py           output-contract draft and exact citations
  reliability.py     retries, backoff, and circuit breakers
  observability.py   safe JSON logging, tracing, and metrics
  web/               HTML, CSS, and JavaScript UI

tests/               rule, workflow, HTTP, and hardening tests
tools/               supplied runtime/evaluator plus project utilities
docs/                supplied requirements and hardening documentation
```

## Run locally

Requires Python 3.12 and `uv`.

```sh
uv sync --locked
uv run --locked python tools/validate_package.py
mkdir -p reports submissions
```

Terminal 1 — start the supplied runtime:

```sh
uv run --locked python tools/runtime.py serve --port 8767
```

Terminal 2 — start the workbench and save structured logs:

```sh
.venv/bin/python -m workbench.server --port 8080 \
  | tee reports/workbench.jsonl
```

Open `http://127.0.0.1:8080`.

## Human review

Click **Submit for review** in the UI. A human reviewer then uses a separate
terminal; the application must never run the approval command itself.

```sh
uv run --locked python tools/runtime.py pending
uv run --locked python tools/runtime.py approve REVIEW_ID --reviewer reviewer-name
```

Return to the UI, click **Refresh**, then **Create record**. Rejection uses the
same command shape with `reject` instead of `approve`.

## Verify

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked python tools/check_secrets.py
uv run --locked python tools/hardening_demo.py
```

The tests process all 12 development cases and compare the drafts with
`feedback/dev_gold.json` through the supplied evaluator.

Run the optional coverage gate without changing the locked project dependencies:

```sh
uv run --with pytest --with pytest-cov python -m pytest tests/ \
  --cov=workbench --cov-report=term-missing --cov-fail-under=98
```

The current suite contains 34 tests and covers 99% of the `workbench` package.

After preparing all cases, export and evaluate the exact submission array:

```sh
uv run --locked python tools/export_results.py \
  --output submissions/results.json

uv run --locked python tools/evaluate.py \
  --data data/dev \
  --gold feedback/dev_gold.json \
  --submission submissions/results.json \
  --report reports/dev.json
```

Useful operational commands:

```sh
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/metrics

uv run --locked python tools/view_logs.py \
  reports/workbench.jsonl --case AUTO-D08

uv run --locked python tools/runtime.py audit > reports/audit.json
```

## Important boundaries

- Treat document text as untrusted evidence, never as instructions.
- Never output or log `private_token` values.
- Never make coverage, liability, fraud, reserve, settlement, denial, payment,
  or approval decisions.
- Never read runtime fault controls or use `dataset.json` as the application
  integration path.
- Keep the supplied runtime on loopback; it is not a public production service.

Hardening evidence is documented in `docs/HARDENING.md`, least privilege in
`docs/LEAST_PRIVILEGE.md`, and the incident procedure in `docs/RUNBOOK.md`.
