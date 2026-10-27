# AWS migration plan

This document records the repository as it exists before the AWS changes, what
the migration moves, and which behavior stays in place. It is the source for
the CloudFormation template, the persistence adapters, and the deployment
scripts added alongside it.

## Current architecture

The application is a Python 3.12 workbench plus a supplied runtime. It is not a
React application and it does not use a web framework. Both processes use the
standard-library HTTP server. Local development has no AWS credentials and no
third-party packages beyond what the deployment extras install.

```text
Browser -> workbench (127.0.0.1:8080) -> runtime (127.0.0.1:8767)
                |                              |
                |                              +-- data/dev/dataset.json
                |                              +-- .state/mock.sqlite
                +-- data/dev/documents/*.md
                +-- data/dev/policies/*.md
                +-- .state/workbench.json
```

| Piece | Where it lives | Role |
|---|---|---|
| Handler UI | `workbench/web/` | One HTML page, `app.js`, and `styles.css`. No build step. |
| Workbench | `workbench/server.py`, `workbench/service.py` | Case queue, draft preparation, review submission, record filing. |
| Rules and citations | `workbench/rules.py`, `workbench/draft.py` | Deterministic classification, routing, and exact quotes. |
| Runtime | `tools/runtime.py` | Case, policy, and claims reads. Reviews, records, idempotency, tool-attempt counters, and the audit log. |
| Dataset | `data/dev/dataset.json` | Read only by the runtime and the evaluator. The workbench does not open it. |
| Evidence files | `data/dev/documents`, `data/dev/policies` | Markdown the workbench cites. The text matches `sources` in the dataset. |
| Runtime state | `.state/mock.sqlite` | Tables `reviews`, `records`, `keys`, `calls`, `audit`. Created on startup. |
| Workbench state | `.state/workbench.json` | Draft, warnings, review id, and idempotency key for each case. |
| Approval | `python tools/runtime.py approve` | Operator CLI. The workbench has no approve method. |
| Containers | `Dockerfile`, `docker-compose.yml` | Devcontainer that runs `sleep infinity`. No ports are published. |

There is no authentication, no LLM client, and no infrastructure definition.
`docs/API.md` describes the runtime contract, including the idempotency key,
the draft hash, and the logical-record duplicate rule. `AUTO-D09` and
`AUTO-D10` share one logical key.

## Behavior that must survive the move

- Drafts are deterministic. Routing stays in `rules.py`.
- A pending or approved review locks the draft until a record exists.
- Record creation requires an approved review whose hash and body match the saved draft.
- The idempotency key and the record row are written in one transaction.
- The same key and fingerprint replay the stored response. A different fingerprint conflicts.
- A second intake for an existing logical key does not create a second record.
- Tool faults still increment `calls` and return the simulated 503.
- `private_token` values and `SYNTHETIC-PRIVATE-` strings stay out of drafts, workbench state, and logs.
- Local `unittest` runs continue to use the JSON file and SQLite. They do not call AWS.

## What moves

| Data | Local store | AWS store | Reader |
|---|---|---|---|
| `dataset.json` | `data/dev/dataset.json` | Private S3 object | Runtime, cached in memory until restart or `POST /internal/dataset/refresh` |
| Reviews, records, idempotency keys, tool attempts, audit | SQLite | RDS PostgreSQL | Runtime |
| Dataset version on a new record | absent | `records.dataset_version` and the `record_created` audit detail | Runtime. The HTTP record body stays `id`, `case_id`, `draft`, `review_id`. |
| Workbench drafts | JSON file | `workbench_state` when a database URL or secret is configured | Workbench |

Markdown evidence stays in the image. On startup the runtime compares those
files with `sources` in the loaded dataset and refuses to serve if they differ.
S3 is authoritative for the dataset document. The image is authoritative for
the files the workbench opens by path. Deploy the same revision of both.

PostgreSQL schema changes are versioned SQL files in `infra/db/migrations`.
The SQLite path still creates its tables on startup so local tests stay
self-contained. Postgres does not run `CREATE TABLE` on every request.

## Authentication and Bedrock

The training app has no sign-in. Deployed sign-in is optional in code and
required only when `FNOL_AUTH_REQUIRED` is true. It is a small session cookie
checked by the workbench. It is not Cognito and it is not IAM.

Handlers can prepare drafts, submit reviews, and file an already approved
record. No application role can approve a review. Approval stays on the runtime
CLI over Session Manager. When auth is required, `--reviewer` must be a
configured reviewer.

Bedrock is off unless `FNOL_BEDROCK_ENABLED` is true. The client is not used by
`rules.py` or `draft.py`. A successful call can only add a non-authoritative
warning. Classification, routing, citations, and approval do not change.

## Deployment shape

One EC2 instance in a public subnet runs Nginx and one backend container.
Nginx serves `workbench/web` and proxies `/api/`, `/health`, `/ready`, and
`/metrics` to `127.0.0.1:8080`. The runtime listens on `127.0.0.1:8767` inside
the container and is not published. RDS is in two private subnets and accepts
5432 only from the instance security group. The instance reaches S3, Secrets
Manager, CloudWatch, and Systems Manager through the Internet Gateway. There
is no NAT, load balancer, or container service.

HTTPS uses Certbot on the instance after DNS exists. The template does not
attach an ACM certificate to Nginx, and the app is not described as HTTPS-ready
until that operator step finishes.

## Affected functionality

- Runtime startup and the dataset health field.
- Runtime SQL and the operator commands `pending`, `approve`, `reject`, `audit`, and `reset`.
- Workbench state reads and writes when a database is configured.
- The handler page, which gains a sign-in form used only when auth is required.
- Process warnings, only when Bedrock is enabled.
- New health and readiness behavior is unchanged when auth is off and the runtime is up.

`tools/evaluate.py`, the gold file, the output contract, and the rule tests are
unchanged.
