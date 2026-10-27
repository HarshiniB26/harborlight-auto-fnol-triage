#!/bin/bash
# Pack the application, upload the release object, and start it on the instance
# through Session Manager. No SSH port is required.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK="${STACK_NAME:-fnol}"
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${FNOL_ENVIRONMENT:-dev}"

output_value() {
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

BUCKET="$(output_value DatasetBucketName)"
KEY="$(output_value DatasetObjectKey)"
INSTANCE="$(output_value InstanceId)"
DB_SECRET="$(output_value DatabaseSecretArn)"
APP_SECRET="$(output_value AppDbSecretArn)"
AUTH_SECRET="$(output_value AuthSecretArn)"
LOG_GROUP="$(output_value LogGroupName)"

if [ "$ENVIRONMENT" = "prod" ]; then
  AUTH_REQUIRED="${FNOL_AUTH_REQUIRED:-true}"
else
  AUTH_REQUIRED="${FNOL_AUTH_REQUIRED:-false}"
fi
BEDROCK="${FNOL_BEDROCK_ENABLED:-false}"

if [ "$AUTH_REQUIRED" = "true" ]; then
  if ! aws secretsmanager get-secret-value \
      --region "$REGION" \
      --secret-id "$AUTH_SECRET" \
      --query SecretString \
      --output text \
      | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); doc=json.loads(raw) if raw and raw != "None" else {}; users=doc.get("users") if isinstance(doc, dict) else None; raise SystemExit(0 if isinstance(users, list) and users else 1)'; then
    echo "The auth secret has no users. Put a users document in ${AUTH_SECRET} before deploying with authentication required." >&2
    exit 1
  fi
fi

ARCHIVE="$(mktemp)"
trap 'rm -f "$ARCHIVE"' EXIT
tar -C "$ROOT" -czf "$ARCHIVE" \
  --exclude .git \
  --exclude .venv \
  --exclude .state \
  --exclude deliverables \
  --exclude __pycache__ \
  --exclude .cursor \
  --exclude reports \
  --exclude submissions \
  .

aws s3 cp "$ARCHIVE" "s3://${BUCKET}/releases/fnol-app.tar.gz" \
  --region "$REGION" \
  --content-type application/gzip \
  --sse AES256

REMOTE="$(cat <<EOF
set -euo pipefail
aws s3 cp s3://${BUCKET}/releases/fnol-app.tar.gz /tmp/fnol-app.tar.gz --region ${REGION}
mkdir -p /opt/fnol/src /opt/fnol/web /var/log/fnol /var/www/certbot
rm -rf /opt/fnol/src
mkdir -p /opt/fnol/src
tar -xzf /tmp/fnol-app.tar.gz -C /opt/fnol/src
cp -a /opt/fnol/src/workbench/web/. /opt/fnol/web/
cp /opt/fnol/src/infra/nginx/fnol.conf /etc/nginx/conf.d/fnol.conf
rm -f /etc/nginx/conf.d/fnol-bootstrap.conf /etc/nginx/conf.d/default.conf
nginx -t
systemctl reload nginx
cat >/opt/fnol/app.env <<'ENVEOF'
AWS_REGION=${REGION}
FNOL_DATASET_BUCKET=${BUCKET}
FNOL_DATASET_KEY=${KEY}
FNOL_DATABASE_SECRET_ARN=${DB_SECRET}
FNOL_APP_SECRET_ARN=${APP_SECRET}
FNOL_AUTH_SECRET_ARN=${AUTH_SECRET}
FNOL_AUTH_REQUIRED=${AUTH_REQUIRED}
FNOL_BEDROCK_ENABLED=${BEDROCK}
FNOL_LOG_GROUP=${LOG_GROUP}
ENVEOF
chmod 600 /opt/fnol/app.env
cd /opt/fnol/src
docker compose --env-file /opt/fnol/app.env -f docker-compose.prod.yml up -d --build
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS http://127.0.0.1:8080/health && curl -fsS http://127.0.0.1:8080/ready; then
    exit 0
  fi
  sleep 5
done
echo "Application health checks did not pass." >&2
exit 1
EOF
)"

PARAMS="$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.stdin.read()]}))' <<<"$REMOTE")"
COMMAND_ID="$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --comment "Deploy FNOL application" \
  --parameters "$PARAMS" \
  --query Command.CommandId \
  --output text)"

echo "SSM command ${COMMAND_ID} started on ${INSTANCE}."
STATUS="Pending"
for _ in $(seq 1 60); do
  STATUS="$(aws ssm get-command-invocation \
    --region "$REGION" \
    --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE" \
    --query Status \
    --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
  sleep 10
done

aws ssm get-command-invocation \
  --region "$REGION" \
  --command-id "$COMMAND_ID" \
  --instance-id "$INSTANCE" \
  --query '{Status:Status,Stdout:StandardOutputContent,Stderr:StandardErrorContent}' \
  --output text

if [ "$STATUS" != "Success" ]; then
  echo "Deployment command finished with status ${STATUS}." >&2
  exit 1
fi
echo "Application deployed. HTTPS still requires the Certbot step in docs/deployment.md."
