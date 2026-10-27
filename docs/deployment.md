# Deployment

Do not run these scripts unless you intend to create billable AWS resources.
Nothing in the test suite calls AWS.

## Automated and manual steps

| Step | How | Who |
|---|---|---|
| 1. Provision the stack | `scripts/deploy-infrastructure.sh` | Automated after you export credentials and the client CIDR |
| 2. Upload `dataset.json` | `scripts/upload-dataset.sh` | Automated |
| 3. Build the application | `scripts/deploy-app.sh` builds the runtime image on the instance | Automated |
| 4. Apply PostgreSQL migrations | Container entrypoint runs `python -m tools.migrate` | Automated on container start |
| 5. Deploy the container and Nginx site | `scripts/deploy-app.sh` via Session Manager | Automated |
| 6. Issue the TLS certificate | Certbot on the instance | Manual, after DNS exists |
| 7. Smoke test | Health, one draft, CLI approval, one record | Manual |

## 1. Provision

Set the handler CIDR before deploying. The template default is a documentation
address and will not admit your browser.

```sh
export AWS_REGION=us-east-1
export STACK_NAME=fnol
export FNOL_ENVIRONMENT=dev
export FNOL_ALLOWED_CIDR=203.0.113.10/32
scripts/deploy-infrastructure.sh
```

The script passes `CAPABILITY_NAMED_IAM` because the instance role has a name.
It does not embed application secrets.

Validate the template before the first deploy:

```sh
cfn-lint infra/cloudformation/fnol.yaml
aws cloudformation validate-template \
  --template-body file://infra/cloudformation/fnol.yaml
```

`validate-template` needs AWS credentials. `cfn-lint` does not. This repository
was checked with `cfn-lint` only; no stack was deployed from here.

## 2. Upload the dataset

```sh
scripts/upload-dataset.sh
```

The script reads the bucket and key from stack outputs, rejects a missing or
invalid JSON file, uploads with `application/json` and SSE-S3, and checks the
object with `head-object`. It does not set a public ACL. Use an identity that
can `s3:PutObject` on the bucket. The instance role cannot upload.

## 3 to 5. Build, migrate, and deploy

For a non-production stack, authentication stays off unless you set
`FNOL_AUTH_REQUIRED=true`. Production defaults that flag to true and the script
stops if the auth secret has no users.

Put the user document before a production deploy. Example shape, stored only
in Secrets Manager:

```json
{"users":[{"username":"handler","password":"<long-random>","role":"handler"},{"username":"reviewer","password":"<long-random>","role":"reviewer"}]}
```

```sh
scripts/deploy-app.sh
```

The script packs the repository, uploads `releases/fnol-app.tar.gz` with your
credentials, and runs the install through Session Manager. On the instance it
builds the `runtime` image, starts Compose with `restart: unless-stopped`, and
curls `/health` and `/ready` on loopback. The entrypoint applies migrations
and creates the `fnol_app` role before the servers listen.

Open a shell without SSH:

```sh
aws ssm start-session --target "$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" \
  --output text)"
```

## 6. HTTPS

This step is manual. The stack does not attach an ACM certificate to Nginx.

1. Create a DNS name that points at the Elastic IP. Pass `FNOL_DOMAIN` and
   `FNOL_HOSTED_ZONE_ID` on a stack update if Route 53 should manage the record.
2. On the instance:

```sh
sudo certbot --nginx -d fnol.example.com
```

3. Confirm the certificate files exist, then install the redirecting site from
   `infra/nginx/fnol-https.conf` after replacing the example name.
4. Install renewal:

```sh
sudo systemctl enable --now certbot-renew.timer || sudo systemctl enable --now snap.certbot.renew.timer
```

Until this step succeeds, the application is HTTP only. Do not describe it as
HTTPS-ready.

## 7. Smoke test

From a client in `AllowedCidr`:

```sh
curl -fsS "http://<public-ip>/health"
curl -fsS "http://<public-ip>/ready"
```

In the browser, prepare one draft and submit it for review. On the instance:

```sh
cd /opt/fnol/src
docker compose --env-file /opt/fnol/app.env -f docker-compose.prod.yml exec fnol \
  python tools/runtime.py pending
docker compose --env-file /opt/fnol/app.env -f docker-compose.prod.yml exec fnol \
  python tools/runtime.py approve REVIEW_ID --reviewer reviewer
```

When auth is required, `--reviewer` must be a configured reviewer username.
Return to the browser and create the record. A second create for the same loss
must not add a row.

## Local PostgreSQL

The default Compose service is still the devcontainer. The database profile is
optional:

```sh
docker compose --profile postgres up -d postgres
export FNOL_DATABASE_URL=postgresql://fnol:fnol_local_test@127.0.0.1:5432/fnol
python -m tools.migrate
```

Unset the URL to return to SQLite.

## Recovery

**EC2 restart.** The Compose restart policy starts the container. Nginx starts
with the instance. If the instance is replaced, run `scripts/deploy-app.sh`
again. The Elastic IP can be reassociated by updating the stack. Dataset and
database state are outside the instance.

**Container crash.** Docker restarts it. The entrypoint runs migrations again;
applied versions are skipped. `/ready` returns 503 while the runtime is down,
and `/health` can still return 200 for the workbench process.

**RDS interruption.** Restore an automated snapshot or the latest point in
time from the RDS console. Do not create records until `/ready` is 200. The
idempotency table prevents a blind retry from creating a second record when
the first commit survived.

**Failed migration.** The failed file is not inserted into `schema_migrations`.
Fix the SQL or the database, then restart the container. Do not edit an
already applied file; add a new one. Restoring the pre-migration snapshot is
the rollback if the file changed data.

**Invalid dataset upload.** The refresh endpoint returns an error and keeps
the previous document. Fix the object and refresh again, or restart after
uploading a valid object. If the process is starting for the first time and
the object is invalid, the container exits and Docker retries until the object
is valid or the configuration points at a local file.

## Operator checklist

- Narrow `AllowedCidr` to the office or VPN range.
- Populate the auth secret before enabling `FNOL_AUTH_REQUIRED`.
- Keep the dataset object and the image's markdown files from the same revision.
- Approve reviews only from Session Manager.
- Finish Certbot before sending handlers an `https://` link.
