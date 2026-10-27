# AWS architecture

The deployment keeps the current two-process application and places it on a
small EC2 instance. Local development is unchanged: SQLite, the workbench JSON
file, and `data/dev/dataset.json`.

```text
Handlers
   |
   | TCP 443, and TCP 80 until a certificate exists
   v
Route 53 (only when a hosted zone and domain are provided)
   |
   v
Internet Gateway
   |
   v
Public subnet: EC2 t3.small
   Nginx serves workbench/web and proxies /api, /health, /ready, /metrics
   Docker container, restart unless-stopped
      workbench  127.0.0.1:8080
      runtime    127.0.0.1:8767   (not published)
   |
   +--> S3 GetObject dataset.json          (S3 is outside the VPC)
   +--> Secrets Manager GetSecretValue
   +--> CloudWatch logs
   |
   v
Private subnets, two Availability Zones
   RDS PostgreSQL, not publicly accessible
      reviews, records, idempotency keys, tool attempts, audit
      workbench draft document
```

There is no load balancer, NAT gateway, ECS service, API Gateway, or
ElastiCache. Session Manager replaces SSH. Bedrock is not granted unless the
stack parameter is turned on.

## Why the shape differs from a generic web tier

The repository is a Python workbench plus a supplied runtime, not a compiled
React bundle. Nginx still serves the static page directly. The browser never
calls the runtime. The runtime remains the only reader of `dataset.json` and
the only writer of reviews, records, idempotency keys, attempt counters, and
the audit log.

Markdown evidence stays in the image because the workbench opens those files
by source id. Startup compares them with the dataset `sources` map and refuses
to serve a mismatch.

## Dataset freshness

The runtime loads the dataset once and keeps it in memory. Uploading a new
object does not change a running process. Operators either restart the
container or, from the instance, call:

```sh
curl -fsS -X POST http://127.0.0.1:8767/internal/dataset/refresh
```

Nginx does not proxy that path. A failed refresh keeps the previous valid
document. New records store the S3 version id, or the local SHA-256 when the
file is local, in `records.dataset_version` and in the `record_created` audit
detail. The record HTTP body does not grow a new field.

## Database

PostgreSQL is selected only when `FNOL_DATABASE_URL` or
`FNOL_DATABASE_SECRET_ARN` is set. Migrations live in
`infra/db/migrations` and run before the processes start. The application role
`fnol_app` receives DML on the FNOL tables. The master secret is used for
migrations. SQLite still creates its own tables for local runs.

Approval remains `python tools/runtime.py approve`. The conditional update
changes a review only while it is pending, so a repeated approval does not
replace the first reviewer. Record insertion and the idempotency row are one
transaction.

## Estimated monthly cost

These are on-demand us-east-1 estimates for the defaults, before tax and data
transfer:

| Piece | Estimate |
|---|---|
| EC2 `t3.small` | about $15 |
| EBS 20 GiB gp3 | about $2 |
| RDS `db.t4g.micro`, 20 GiB gp3, Single-AZ | about $12 to $15 |
| Secrets Manager, two secrets | under $1 |
| S3, CloudWatch logs, and alarms at this volume | about $1 to $3 |
| Elastic IP while attached to a running instance | $0 |

A typical month is about **$30 to $40**. Multi-AZ, a larger instance, Bedrock
inference, and NAT would raise that. The template does not create those by
default.

## What is not automatic

HTTPS is not complete when the stack finishes. Certbot has to issue a
certificate after DNS points at the instance. Until then the instance serves
HTTP on port 80 to the configured CIDR, or only the bootstrap page if the
application has not been deployed.
