# Auto FNOL — system architecture

Private pilot architecture for the Auto FNOL workbench in **us-east-1**. The
application is never exposed on the public internet: operators reach the UI
through an encrypted **SSM port-forward**. Immutable case/tool evidence stays
in **S3**; mutable reviews, records, and audit live in **private RDS**.

Interactive diagram and tables live in the Cursor canvas
`fnol-system-architecture.canvas.tsx` (open beside chat from the canvases
directory for this workspace).

Canonical deploy path:

| Artifact | Path |
|---|---|
| Infrastructure template | `deployment/cloudformation.yaml` |
| Deploy orchestrator | `deployment/deploy.sh` |
| Mutable schema | `deployment/schema.sql` |
| Operator notes | `deployment/README.md` |

```text
Handler laptop
  └─ SSM encrypted port-forward → private EC2 (no inbound ports)
       ├─ Workbench UI + /api          (:8080)
       ├─ Internal /v1 runtime         (:8767, localhost only)
       ├─ S3 gateway endpoint → private S3
       │    ├─ dataset.json
       │    └─ documents/ and policies/
       ├─ TLS 5432 → private RDS PostgreSQL
       │    └─ reviews, records, idempotency, counters, audit, UI state
       └─ NAT egress → ECR, SSM, CloudWatch, Secrets Manager
```

---

## 1. Design principles

1. **Zero inbound network surface** — EC2 security group ingress is empty; no
   ALB, no bastion, no SSH. Reachability is the SSM control plane + IAM.
2. **Evidence vs state** — Supplied fixtures (`dataset.json`, documents,
   policies) are immutable objects in S3. PostgreSQL holds only mutable
   operational state produced by the pilot.
3. **Human approval boundary** — Processing never auto-approves. A named person
   must approve/reject an exact draft hash in the UI (or admin CLI fallback).
4. **Least privilege** — Instance role is read-mostly; DB uses a dedicated
   `fnol_app` role with DML on schema `fnol` only.
5. **Cost-conscious pilot** — Single AZ app host (`t3.small`), single-AZ RDS
   (`db.t4g.micro`), one NAT Gateway; Multi-AZ / ALB deferred.

---

## 2. Services and reasoning

| Service | What it does | Why this service |
|---|---|---|
| **VPC** `10.44.0.0/16` | Isolation boundary with DNS support | Own CIDR so SG/route rules are unambiguous; no shared-default VPC assumptions. |
| **Public subnet** `10.44.0.0/24` | Hosts NAT (+ IGW attachment) | Only place that needs a public IP; app and DB stay off it. |
| **Private app subnet** `10.44.10.0/24` | Hosts EC2; default route → NAT | Outbound for pulls/APIs without accepting inbound from internet. |
| **Private DB subnets** `10.44.20/24`, `10.44.21/24` | RDS subnet group across 2 AZs | RDS requires ≥2 subnets; route table has **no NAT** → DB cannot initiate internet egress. |
| **Internet Gateway** | Public subnet ↔ internet | Required for NAT EIP; not a path to the app. |
| **NAT Gateway + EIP** | App egress | ECR login/pull, SSM, Secrets Manager, CloudWatch without giving EC2 a public IP. |
| **S3 Gateway VPC Endpoint** | Private route to S3 | Evidence traffic stays on AWS network; saves NAT data processing cost. |
| **EC2** (Amazon Linux 2023) | Docker host for workbench + runtime | Smallest operational unit for a private pilot; both containers share host networking for localhost `/v1`. |
| **SSM Session Manager** | Port-forward `8080→8080` and Run Command deploy | Replaces bastion/SSH; sessions are IAM-authorized and audited. |
| **ECR** | Immutable container images | Scan-on-push; immutable tags (`git-sha-timestamp`) so deploys are reproducible. |
| **S3 evidence bucket** | `dataset.json`, `documents/`, `policies/` | Versioned, SSE-S3, public access blocked, TLS-only bucket policy. |
| **RDS PostgreSQL** | Mutable FNOL schema | Encrypted storage; not publicly accessible; 7-day backups; snapshot on delete. |
| **Secrets Manager** | `fnol_admin` (master) + `fnol_app` (runtime) | Credentials never in image/userdata; rotation-friendly; migrate vs runtime separation. |
| **CloudWatch Logs** | `/auto-fnol/application` streams | Container stdout via `awslogs`; 30-day retention. |

### On-host processes

| Container | Bind | Responsibility |
|---|---|---|
| `fnol-workbench` | `0.0.0.0:8080` (reached via SSM tunnel) | UI, `/api`, human review decision, record creation after hash checks; `STORAGE_BACKEND=aws`, `RUNTIME_API_URL=http://127.0.0.1:8767`. |
| `fnol-runtime` | `127.0.0.1:8767` | Internal `/v1` case/tool/record API over fixtures + RDS; not forwarded through SSM. |

---

## 3. Security groups

| Security group | Inbound | Outbound | Reasoning |
|---|---|---|---|
| **ApplicationSecurityGroup** | **None** (`SecurityGroupIngress: []`) | All (`0.0.0.0/0`) | Network cannot connect to the instance. Operators use SSM, which does not require SG ingress. Egress covers NAT paths to AWS APIs and RDS in-VPC. |
| **DatabaseSecurityGroup** | TCP **5432** from **ApplicationSecurityGroup** only | (default / not widened in template) | Postgres is reachable solely from the app host’s SG — no CIDR ranges, no other roles. |

### Complementary network controls

- EC2: **no public IPv4**; private subnet only.
- RDS: `PubliclyAccessible: false`; DB route table has no `0.0.0.0/0`.
- S3: Block Public Access (all four flags); bucket policy **Deny** if
  `aws:SecureTransport` is false.
- EC2 metadata: **IMDSv2 required** (`HttpTokens: required`).
- EBS root volume: encrypted gp3.

---

## 4. IAM roles and policies

### Instance role (`InstanceRole`)

Trust: `ec2.amazonaws.com` → attached via `InstanceProfile`.

| Attachment | Permissions | Reasoning |
|---|---|---|
| **AmazonSSMManagedInstanceCore** | SSM agent, sessions, Run Command | Deploy and tunnel without SSH keys. |
| **AmazonEC2ContainerRegistryReadOnly** | Pull images | Instance may pull, not push or mutate repos. |
| **FnolApplicationAccess** (inline) | `s3:ListBucket` on evidence bucket; `s3:GetObject` on objects | Runtime/workbench **read** evidence only. |
| same | `secretsmanager:GetSecretValue` on master + app secret ARNs | Migrate uses master; long-running containers use app secret. |
| same | `logs:CreateLogStream`, `DescribeLogStreams`, `PutLogEvents` on the app log group ARN | Telemetry scoped to one log group. |

**Explicitly out of scope for the instance role:** IAM administration, unrestricted
S3 write/delete, Secrets create/rotate admin, RDS modify/delete, VPC changes,
account-wide CloudWatch.

Human operators authenticating to SSM use **their own IAM principals** (separate
from the instance role). In this pilot, the typed reviewer name is an audit
label; access control is IAM + who can start a session.

### PostgreSQL roles

| Role | Used by | Privileges |
|---|---|---|
| `fnol_admin` (master secret) | One-shot `tools/migrate_to_aws.py` | Owns schema creation; creates/updates `fnol_app`. |
| `fnol_app` (application secret) | `fnol-runtime` / `fnol-workbench` | `USAGE` on schema `fnol`; `SELECT/INSERT/UPDATE/DELETE` on tables; `USAGE/SELECT` on sequences. |

---

## 5. CloudFormation resource map

Template: [`deployment/cloudformation.yaml`](../../deployment/cloudformation.yaml).

Parameters: `ProjectName` (default `auto-fnol`), `InstanceType` (`t3.small`),
`DatabaseInstanceClass` (`db.t4g.micro`), `LatestAmiId` (SSM AL2023 parameter).

| Logical ID | AWS type | Notes |
|---|---|---|
| `Vpc` | `AWS::EC2::VPC` | `10.44.0.0/16` |
| `InternetGateway` + attachment | IGW | Public path for NAT only |
| `PublicSubnetA` | Subnet | `MapPublicIpOnLaunch: true` |
| `PrivateAppSubnetA` | Subnet | App host |
| `PrivateDbSubnetA/B` | Subnets | Two AZs for RDS |
| `PublicRouteTable` + IGW route | Routing | Public tier |
| `NatEip` + `NatGateway` | NAT | In public subnet |
| `PrivateAppRouteTable` + NAT route | Routing | App egress |
| `PrivateDbRouteTable` | Routing | **No default route** |
| `EvidenceBucket` | S3 | `DeletionPolicy: Retain`, versioning, SSE-S3 |
| `EvidenceBucketPolicy` | Bucket policy | Deny insecure transport |
| `S3GatewayEndpoint` | VPC endpoint | On private app route table |
| `ContainerRepository` | ECR | Immutable tags, scan on push, Retain |
| `DatabaseSecret` | Secrets Manager | Master `fnol_admin` |
| `ApplicationDatabaseSecret` | Secrets Manager | App `fnol_app` |
| `DatabaseSubnetGroup` | RDS subnet group | Both DB subnets |
| `ApplicationSecurityGroup` | SG | Empty ingress |
| `DatabaseSecurityGroup` | SG | 5432 from app SG |
| `Database` | RDS instance | Encrypted Postgres, Snapshot on replace/delete |
| `Secret*Attachment` | Secret target | Wires endpoint into secrets |
| `ApplicationLogGroup` | Logs | 30-day retention |
| `InstanceRole` / `InstanceProfile` | IAM | Least-privilege + SSM/ECR managed policies |
| `ApplicationInstance` | EC2 | Userdata installs Docker; depends on NAT + secret attachments |

**Outputs consumed by `deploy.sh`:** `InstanceId`, `EvidenceBucketName`,
`RepositoryUri`, `DatabaseSecretArn`, `ApplicationDatabaseSecretArn`,
`DatabaseEndpoint`, `LogGroupName`, `Region`.

---

## 6. Deployment strategy and steps

### Preconditions

- AWS CLI authenticated to the target account; confirm with
  `aws sts get-caller-identity` and `aws configure get region`.
- Session Manager plugin on each laptop that opens the tunnel.
- Docker available locally for image build/push.
- IAM permission to create CloudFormation, VPC, EC2, RDS, ECR, S3, IAM,
  Secrets Manager, CloudWatch, and Systems Manager resources.
- Capability `CAPABILITY_NAMED_IAM` (instance role creation).

### One-shot deploy

```sh
AWS_REGION=us-east-1 STACK_NAME=auto-fnol-pilot \
  ./deployment/deploy.sh
```

What the script does:

| Step | Action |
|---|---|
| **1** | `aws cloudformation deploy` of `deployment/cloudformation.yaml` with `ProjectName`. |
| **2** | `docker build --pull` tagged `${git-sha}-${UTC timestamp}`. |
| **3** | ECR login + push; upload `data/dev/dataset.json`, sync `documents/` and `policies/` to the evidence bucket (SSE-S3). |
| **4** | Wait EC2 status OK; poll SSM until `PingStatus=Online`. |
| **5** | SSM `AWS-RunShellScript`: pull image → `python tools/migrate_to_aws.py --skip-s3` (master secret) → replace/start `fnol-runtime` and `fnol-workbench` (app secret) → health checks. |
| **6** | Print SSM port-forward command for `:8080`. |

Migration is **idempotent** and does not wipe reviews/records/audit. Cases and
deliberate faults remain in `dataset.json` (not duplicated into relational
case tables).

### Open the private UI

```sh
aws ssm start-session \
  --region us-east-1 \
  --target INSTANCE_ID \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
```

Browse `http://127.0.0.1:8080`. Verify:

```sh
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/api/cases
```

AWS-side checks: stack complete; EC2 no public IPv4 and SSM Online; RDS not
public; S3 Block Public Access on; CloudWatch streams `runtime` / `workbench`.

### Application updates

Commit code and re-run `./deployment/deploy.sh`. New immutable image tag; safe
migration; containers replaced. RDS and S3 data remain outside containers.

### Teardown

```sh
aws cloudformation delete-stack \
  --region us-east-1 \
  --stack-name auto-fnol-pilot
```

S3 bucket, ECR repository, and final RDS snapshot are **retained** — delete
manually only after confirming evidence is no longer needed. NAT, EC2, and RDS
accrue cost while the stack exists.

---

## 7. Data plane summary

| Store | Mutability | Contents |
|---|---|---|
| S3 evidence bucket | Immutable inputs (versioned) | `dataset.json`, documents, policy wording |
| RDS `fnol.*` | Mutable | `reviews`, `records`, `idempotency_keys`, `tool_calls`, `audit`, `workbench_state` |
| ECR | Immutable tags | Application image |
| Secrets Manager | Rotatable credentials | Master + app DB passwords |
| CloudWatch Logs | Append-only telemetry | Metadata/outcomes — not tool payloads or documents |

---

## Related docs

- [deployment/README.md](../../deployment/README.md) — operator deploy/runbook
- [docs/LEAST_PRIVILEGE.md](../LEAST_PRIVILEGE.md) — human/app boundary audit
- [docs/HARDENING.md](../HARDENING.md) — SLO, failure cases, security controls
- [docs/RUNBOOK.md](../RUNBOOK.md) — operational procedures
