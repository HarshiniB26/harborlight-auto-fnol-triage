# AWS deployment

This deployment creates the private pilot architecture in `us-east-1`:

```text
Handler laptop
  └─ SSM encrypted port-forward → private EC2 (no inbound ports)
       ├─ Workbench UI + /api
       ├─ Internal /v1 runtime
       ├─ S3 gateway endpoint → private S3
       │    ├─ dataset.json (unchanged supplied case/tool definitions)
       │    └─ documents/ and policies/ (immutable evidence)
       ├─ TLS 5432 → private RDS PostgreSQL
       │    └─ reviews, records, idempotency, counters, audit, UI state
       └─ NAT egress → ECR, SSM, CloudWatch, Secrets Manager
```

The EC2 instance has no public IP and no inbound security-group rules. The NAT
Gateway is in the only public subnet. RDS uses two isolated database subnets.
The application and internal `/v1` runtime share the private EC2 host and are
never exposed to the internet.

## Prerequisites

- AWS CLI authenticated to the target account
- AWS Session Manager plugin installed on each laptop that opens the tunnel
- Docker running locally
- permission to create CloudFormation, VPC, EC2, RDS, ECR, S3, IAM, Secrets
  Manager, CloudWatch and Systems Manager resources
- repository checked out on the branch/version to deploy

Confirm the account before creating anything:

```sh
aws sts get-caller-identity
aws configure get region
```

The script defaults to `us-east-1`, keeping the supplied data in the US.

## Deploy everything

From the repository root:

```sh
AWS_REGION=us-east-1 STACK_NAME=auto-fnol-pilot \
  ./deployment/deploy.sh
```

The script performs these real operations:

1. Deploys `deployment/cloudformation.yaml`.
2. Builds one immutable Docker image.
3. Pushes that image to the stack's private ECR repository.
4. Waits for the private EC2 instance to become available through SSM.
5. Uploads `data/dev/dataset.json` unchanged to the private S3 bucket.
6. Uploads the referenced documents and policy wording to that bucket.
7. Creates the mutable PostgreSQL schema from `deployment/schema.sql`.
8. Starts the S3-input/RDS-state `/v1` runtime and AWS-backed workbench.
9. Sends container output to CloudWatch Logs.
10. Checks the runtime health and workbench readiness endpoints.

The schema migration is idempotent and does not delete reviews, records or
audit history. Cases, tool fixtures and deliberate faults remain in the supplied
`dataset.json`; they are not duplicated into relational tables.

## Open the private application

The deployment prints this command with the real instance ID:

```sh
aws ssm start-session \
  --region us-east-1 \
  --target INSTANCE_ID \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
```

Keep that terminal open and browse to `http://127.0.0.1:8080`. The localhost
address is the near end of the encrypted tunnel; the application is running on
the private EC2 instance in AWS.

## Decide a review

After submitting a review, a person reads the exact draft and citations, enters
their name in the UI, and clicks **Approve exact draft** or **Reject draft**.
Processing never approves automatically. The reviewer name is stored with the
decision and draft hash. In this private pilot, SSM/IAM controls who can reach
the UI; the typed name is an audit label, not independent identity proof.

The operator script remains available as an administrative fallback:

```sh
AWS_REGION=us-east-1 ./deployment/approve-review.sh \
  INSTANCE_ID REVIEW_ID reviewer-name
```

Refresh the case in the UI and create the approved record.

## Verify the deployment

Through the open SSM tunnel:

```sh
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/api/cases
```

In AWS, verify:

- CloudFormation stack status is `CREATE_COMPLETE` or `UPDATE_COMPLETE`.
- EC2 has no public IPv4 address and is `Online` in Systems Manager.
- RDS says `Publicly accessible: No`.
- S3 Block Public Access is enabled.
- CloudWatch log group contains `runtime` and `workbench` streams.

## Update the application

Commit the code and rerun `deployment/deploy.sh`. It builds a new image tag,
pushes it to ECR, reruns the safe migration and replaces both containers. RDS
and S3 data remain outside the containers.

## Cost and cleanup

The NAT Gateway, EC2 instance and RDS instance accrue charges while running.
When the pilot is finished, inspect retained evidence/database snapshots before
deleting the stack:

```sh
aws cloudformation delete-stack \
  --region us-east-1 \
  --stack-name auto-fnol-pilot
```

The template intentionally retains the S3 bucket, ECR repository and final RDS
snapshot. Delete those manually only after confirming they are no longer needed.

