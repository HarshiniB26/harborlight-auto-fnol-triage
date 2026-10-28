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
- Sequential batch preparation: the supplied 12-case dataset is prepared once,
  and later dataset changes process only newly discovered case IDs.
- Durable S3-to-SQS ingestion with retry, a dead-letter queue, and a PostgreSQL
  checkpoint so events and cases are not silently lost during restarts.
- A hard human boundary: processing cannot approve a draft; a named person must
  explicitly approve or reject the exact draft, and a changed draft needs review.
- Retries with exponential backoff, circuit breakers, and idempotent writes.
- Prompt-injection isolation, `private_token` removal, structured logs, trace
  IDs, health/readiness endpoints, and SLO metrics.

## Application flow

```text
dataset.json and evidence
        |
        v
supplied Runtime API
        |
        v
sequential Workbench processing
        |
        +--> retrieve intake, policy and claims data
        +--> reconcile facts and select incident-time policy
        +--> apply deterministic routing and guardrails
        +--> generate an exact cited draft
        +--> submit the unchanged draft to Pending Review
        |
        v
claims-handler UI
        |
        +--> named human approves or rejects
        |
        +--> approved unchanged draft may create one mock record
```

The supplied runtime is an external dependency simulator. `workbench/server.py`
is our browser-facing backend; having both does not mean the frontend calls two
backends. The browser calls only the workbench.

## Batch processing and new cases

This is application-level sequential processing, not AWS Batch. The Workbench
uses the same deterministic `process()` workflow for every case, one case at a
time, which keeps dependency load predictable and makes the audit trail easy to
follow.

### Initial 12-case bootstrap

On the first AWS deployment:

1. `deployment/deploy.sh` uploads documents and policies to S3, then uploads
   `dataset.json` last.
2. The S3 `ObjectCreated` event is placed in SQS and remains there while the
   Fargate service starts.
3. The Workbench long-polls SQS, receives the event, and asks the Runtime API
   for a fresh list of cases.
4. The PostgreSQL checkpoint is initially empty, so all 12 case IDs are new.
5. Each case is processed sequentially and its exact generated draft is
   submitted to the pending human-review queue.
6. Successfully handled IDs are added to the durable checkpoint. The handler
   opens the UI to already-prepared drafts; no preparation button is required.

### Later dataset updates

When a future `dataset.json` is uploaded, S3 sends another notification to SQS.
The Workbench refreshes the Runtime API, compares current case IDs with the
PostgreSQL checkpoint, and processes only newly added IDs. SQS stores the
notification, not the claim data itself.

```text
S3 dataset update
      |
      v
SQS notification ---- retry ----> dead-letter queue after repeated failures
      |
      v
refresh Runtime case list
      |
      v
current IDs - checkpoint IDs
      |
      v
process only new cases sequentially
```

Transient policy or claims failures receive up to three attempts. If a
dependency remains unavailable, the deterministic safe fallback routes the
draft to `manual_review` and identifies the unavailable service; it never makes
a coverage or payment decision. A hard technical exception leaves that case
outside the checkpoint and leaves the SQS message unacknowledged so it can be
retried. Successfully processed cases are checkpointed even when another case
in the same event fails.

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
  dataset_diff_poller.py
                     SQS long poll and dataset-change coordination
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

The current suite contains 50 tests, including sequential intake, S3/SQS diff,
human-review, duplicate, idempotency, guardrail, and deployment regressions.

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

### AWS architecture

```text
Handler laptop
  |
  | Coforge SSO + encrypted SSM port-forward
  v
Private ECS Fargate service (desired count 1, no public IP)
  +-- fnol-workbench container :8080
  |     +-- handler UI/API
  |     +-- sequential queue processor
  |     `-- SQS long poller
  |
  `-- fnol-runtime container :8767
        `-- supplied /v1 enterprise simulator
             (same-task communication over 127.0.0.1)

Private S3 bucket
  +-- dataset.json
  +-- intake/estimate/report documents
  `-- policy wording and endorsements
        |
        `-- ObjectCreated(dataset.json) --> SQS --> Workbench
                                           `-----> DLQ on repeated failure

Private RDS PostgreSQL
  +-- drafts and review decisions
  +-- created mock records
  +-- idempotency responses
  +-- audit/workbench state
  `-- processed-case checkpoint

CloudWatch
  +-- Runtime, Workbench and migration logs
  +-- ECS Container Insights
  `-- processing, circuit-breaker, queue-age and DLQ alarms

Private subnet outbound path: Fargate --> NAT Gateway --> Internet Gateway
This is outbound-only access to AWS service endpoints; it does not expose the
application publicly.
```

The one Fargate task intentionally contains both application containers because
they share a lifecycle and communicate privately over task localhost. ECS
replaces the task if an essential container fails. Desired count `1` is a
cost-conscious pilot choice and may cause brief downtime during replacement;
production high availability would use two or more web tasks and separate the
single sequential intake worker.

### Where data is stored

| Data | Store | Purpose |
|---|---|---|
| Dataset, documents and policy wording | Private S3 | Immutable/source evidence |
| Reviews, records, idempotency and audit state | Private RDS PostgreSQL | Durable multi-handler workflow state |
| Dataset-change notification | SQS | Reliable buffering and retry; not claim storage |
| Container output and operational metrics | CloudWatch | Diagnosis, dashboards and alarms |
| Container image | ECR | Immutable application releases |
| Database credentials | Secrets Manager | Runtime secret retrieval without hard-coding |

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

If the tunnel reports that the SSO token expired, refresh the personal session
and start it again:

```sh
aws sso login --profile auto-fnol-admin
./deployment/start-tunnel.sh
```

If AWS cannot refresh the cached token, clear the local SSO sessions first:

```sh
aws sso logout
aws sso login --profile auto-fnol-admin
./deployment/start-tunnel.sh
```

A tunnel `broken pipe` means the laptop-to-AWS HTTPS session was interrupted;
it does not mean Fargate or PostgreSQL was deleted. Reauthenticate if necessary
and rerun the tunnel script.

### Deployment lifecycle and operations

`deployment/deploy.sh` updates the CloudFormation infrastructure, builds one
immutable ARM64 image, pushes it to ECR, registers the task definitions,
uploads evidence, runs the PostgreSQL migration as a one-off Fargate task, and
then starts the private service. Rerunning it publishes a new image and replaces
the service task while preserving RDS and S3 data.

CloudWatch alarms currently record operational state in the AWS console; they
do not notify a developer because no SNS action is configured. For production,
attach an SNS topic and the client's approved email, chat, or incident-management
integration.

The deployment is defined as code in `deployment/cloudformation.yaml`; the
database schema is in `deployment/schema.sql`. Complete deployment, access,
approval, verification, update and cleanup instructions are in
[`deployment/README.md`](deployment/README.md).
