# Personal Auto FNOL Triage Copilot

A claims-handler workbench that prepares a cited First Notice of Loss
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
- A hard human boundary: processing cannot approve a draft; a named person must
  explicitly approve or reject the exact draft, and a changed draft needs review.
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
        +--> explicit named human decision in the private UI
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
uv sync --locked --group dev
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

The initial queue job prepares all current cases sequentially and submits each
exact draft to the pending-review queue. A handler opens a pending draft, reads
its facts and citations, enters their name, and clicks **Approve draft** or
**Reject**. The decision is bound to the draft hash; processing never approves
automatically.

The supplied operator CLI is still available as a local administrative fallback:

```sh
uv run --locked python tools/runtime.py pending
uv run --locked python tools/runtime.py approve REVIEW_ID --reviewer reviewer-name
```

Return to the UI, click **Refresh**, then **Create record**. Rejection uses the
same command shape with `reject` instead of `approve`.

## Verify

```sh
uv run --locked ruff check workbench tools tests
uv run --locked mypy
uv run --locked python -m pytest --cov
uv run --locked python tools/check_secrets.py
uv run --locked python tools/hardening_demo.py
```

The tests process all 12 development cases and compare the drafts with
`feedback/dev_gold.json` through the supplied evaluator.

The current suite contains 89 tests, including sequential intake, S3/SQS diff,
human-review, duplicate, idempotency, guardrail, and deployment regressions. The
CI quality gate requires Ruff and mypy to pass and enforces 100% statement and
branch coverage.

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
  or autonomous approval decisions.
- Never read runtime fault controls or use `dataset.json` as the application
  integration path.
- Keep the supplied runtime on loopback; it is not a public production service.

Hardening evidence is documented in `docs/HARDENING.md`, least privilege in
`docs/LEAST_PRIVILEGE.md`, and the incident procedure in `docs/RUNBOOK.md`.

## Deploy to AWS

AWS mode runs one private ECS Fargate service containing the Workbench and
internal runtime containers. The unchanged `dataset.json`, documents, and
policies stay in private S3; S3 change events are buffered by SQS and processed
sequentially. RDS PostgreSQL holds only mutable reviews, records, idempotency
keys, counters, audit events, generated workbench state, and the handled-case
checkpoint. Container logs go to CloudWatch. Local file-backed mode remains
available for development and testing.

```sh
aws sso login --profile auto-fnol-admin
aws sts get-caller-identity --profile auto-fnol-admin
AWS_PROFILE=auto-fnol-admin AWS_REGION=us-east-1 \
STACK_NAME=auto-fnol-team1 PROJECT_NAME=auto-fnol-team1 \
  ./deployment/deploy.sh
```

### Teammate access through Coforge SSO

The application is private, so every teammate uses their own Coforge AWS SSO
identity and opens an independent encrypted tunnel to the shared Fargate
service. Never copy another teammate's temporary credentials or SSO cache.

Configure the AWS CLI once with `aws configure sso` and use:

```text
SSO start URL: https://identitycenter.amazonaws.com/ssoins-7223ae9684bca5e9
SSO region: us-east-1
AWS account: 104005345381
Role: AWSAdministratorAccess
Default region: us-east-1
Output format: json
Profile name: auto-fnol-admin
```

The local profile name is only a label and may be different. For each new SSO
session, log in and start the tunnel with that profile:

```sh
aws sso login --profile auto-fnol-admin

export AWS_PROFILE=auto-fnol-admin
export AWS_REGION=us-east-1
export STACK_NAME=auto-fnol-team1

./deployment/start-tunnel.sh
```

Keep the tunnel terminal open and visit `http://127.0.0.1:18080`. Multiple
handlers can each run their own local tunnel to the same deployed application
and PostgreSQL state. Press `Ctrl+C` in the tunnel terminal when finished.

The deployment is defined as code in `deployment/cloudformation.yaml`; the
database schema is in `deployment/schema.sql`. Complete deployment, access,
approval, verification, update and cleanup instructions are in
[`deployment/README.md`](deployment/README.md).
