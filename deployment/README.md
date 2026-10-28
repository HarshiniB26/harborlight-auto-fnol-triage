# AWS deployment

The pilot is private, US-region, and handler-accessible through an encrypted
Session Manager tunnel:

```text
Handler laptop
  └─ SSM encrypted port-forward
       └─ private ECS Fargate task (desired count 1; no public IP)
            ├─ Workbench UI/API :8080
            └─ Runtime /v1 :8767 over task localhost

S3 dataset.json update → SQS → sequential Workbench intake
S3 documents/policies ────────→ Runtime and Workbench evidence reads
Fargate task ── TLS 5432 ─────→ private RDS PostgreSQL
Fargate logs/metrics ─────────→ CloudWatch
Private app subnet ── NAT ────→ ECR, SQS, Secrets Manager, CloudWatch, SSM
```

SQS stores notifications, not cases. The first `dataset.json` event sees an
empty PostgreSQL checkpoint, so all 12 cases are prepared sequentially and
submitted as pending reviews. Later events refresh the runtime's S3 dataset and
prepare only IDs absent from the durable handled-case checkpoint. Successful
events are deleted; technical failures retry and eventually reach the DLQ.

## Prerequisites

- Docker Desktop running
- AWS CLI v2 and Session Manager plugin
- an authenticated profile with permission for CloudFormation, VPC, ECS,
  Fargate, RDS, ECR, S3, SQS, IAM, Secrets Manager, CloudWatch and SSM

```sh
aws sso login --profile auto-fnol-admin
aws sts get-caller-identity --profile auto-fnol-admin
```

## Deploy

From the repository root:

```sh
export AWS_PROFILE=auto-fnol-admin
export AWS_REGION=us-east-1
export STACK_NAME=auto-fnol-team1
export PROJECT_NAME=auto-fnol-team1

aws cloudformation validate-template \
  --region "$AWS_REGION" \
  --template-body file://deployment/cloudformation.yaml

./deployment/deploy.sh
```

The script:

1. updates infrastructure with the service stopped;
2. builds and pushes one immutable ARM64 image;
3. registers the Runtime, Workbench and migration task definitions;
4. uploads documents and policies, then `dataset.json` last;
5. runs the idempotent PostgreSQL migration as a one-off Fargate task;
6. starts one Fargate service task and waits for stability.

The service task contains two containers because they share a lifecycle and
communicate over `127.0.0.1`. ECS replaces the task if an essential container
fails. The migration is separate because it should run once per deployment,
not on every application restart.

## Open the private UI

```sh
./deployment/start-tunnel.sh
```

Keep that terminal open and browse to `http://127.0.0.1:18080`. No load
balancer, public application URL, public task IP, or inbound security-group
rule is created.

## Handler workflow

The automatic queue job submits drafts before the handler opens the UI. Under
**Pending Review**, the handler reads one exact draft and its citations, enters
their name, and approves or rejects it. Only an approved, unchanged draft shows
**Create claim record**. Record creation remains idempotent and duplicate-safe.

The administrative approval fallback is:

```sh
./deployment/approve-review.sh REVIEW_ID reviewer-name
```

## Verify

Through the tunnel:

```sh
curl http://127.0.0.1:18080/health
curl http://127.0.0.1:18080/ready
curl http://127.0.0.1:18080/api/cases
```

In AWS, verify that the stack is complete, the ECS service has one running
task, RDS is not publicly accessible, S3 Block Public Access is enabled, the
SQS queue is empty after intake, the DLQ is empty, and the CloudWatch log group
contains `runtime`, `workbench`, and `migration` streams.

## Update and cleanup

Rerun `deployment/deploy.sh` after code changes. It publishes a new immutable
tag, runs the safe migration, and replaces the service task; RDS and S3 remain
outside the task.

The NAT Gateway, Fargate task and RDS instance accrue charges while running.
To remove the stack:

```sh
aws cloudformation delete-stack \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME"
```

The template retains the S3 bucket and ECR repository and takes a final RDS
snapshot. Remove retained data only after confirming it is no longer needed.
