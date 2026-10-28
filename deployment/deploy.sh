#!/usr/bin/env bash
set -euo pipefail

# Builds the image, creates/updates AWS infrastructure, uploads immutable
# supplied evidence, initializes mutable RDS state, and starts both containers.

STACK_NAME="${STACK_NAME:-auto-fnol-pilot}"
PROJECT_NAME="${PROJECT_NAME:-auto-fnol}"
AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)}"

for command in aws docker python; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

echo "1/6 Creating or updating the private AWS infrastructure..."
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file deployment/cloudformation.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName="$PROJECT_NAME" \
  --tags Project="$PROJECT_NAME"

output() {
  aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

INSTANCE_ID="$(output InstanceId)"
BUCKET="$(output EvidenceBucketName)"
REPOSITORY_URI="$(output RepositoryUri)"
MASTER_SECRET_ARN="$(output DatabaseSecretArn)"
APP_SECRET_ARN="$(output ApplicationDatabaseSecretArn)"
LOG_GROUP="$(output LogGroupName)"
DATASET_DIFF_QUEUE_URL="$(output DatasetDiffQueueUrl)"
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
REGISTRY="${REPOSITORY_URI%/*}"

echo "2/6 Building the immutable application image..."
docker build --pull --tag "$IMAGE_URI" .

echo "3/6 Pushing the image to Amazon ECR..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker push "$IMAGE_URI"

aws s3 cp data/dev/dataset.json "s3://${BUCKET}/dataset.json" \
  --region "$AWS_REGION" --sse AES256 --content-type application/json
aws s3 sync data/dev/documents "s3://${BUCKET}/documents/" \
  --region "$AWS_REGION" --sse AES256
aws s3 sync data/dev/policies "s3://${BUCKET}/policies/" \
  --region "$AWS_REGION" --sse AES256

echo "4/6 Waiting for the private EC2 instance and SSM agent..."
aws ec2 wait instance-status-ok --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
for _ in $(seq 1 60); do
  STATUS="$(aws ssm describe-instance-information \
    --region "$AWS_REGION" \
    --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
    --query 'InstanceInformationList[0].PingStatus' \
    --output text 2>/dev/null || true)"
  if [[ "$STATUS" == "Online" ]]; then
    break
  fi
  sleep 10
done
if [[ "${STATUS:-}" != "Online" ]]; then
  echo "The EC2 instance did not become available in Systems Manager." >&2
  exit 1
fi

echo "5/6 Initializing mutable PostgreSQL state and starting private services..."
PARAMETERS_FILE="$(mktemp)"
trap 'rm -f "$PARAMETERS_FILE"' EXIT
export AWS_REGION INSTANCE_ID BUCKET REPOSITORY_URI MASTER_SECRET_ARN APP_SECRET_ARN LOG_GROUP IMAGE_URI REGISTRY DATASET_DIFF_QUEUE_URL
python - "$PARAMETERS_FILE" <<'PY'
import json
import os
import sys

region = os.environ["AWS_REGION"]
image = os.environ["IMAGE_URI"]
registry = os.environ["REGISTRY"]
bucket = os.environ["BUCKET"]
master_secret = os.environ["MASTER_SECRET_ARN"]
app_secret = os.environ["APP_SECRET_ARN"]
log_group = os.environ["LOG_GROUP"]
dataset_diff_queue_url = os.environ["DATASET_DIFF_QUEUE_URL"]

environment = (
    f"-e AWS_REGION={region} -e DB_SECRET_ARN={app_secret} "
    f"-e DB_NAME=fnol -e EVIDENCE_BUCKET={bucket}"
)
workbench_environment = (
    f"{environment} -e DATASET_DIFF_QUEUE_URL={dataset_diff_queue_url}"
)
migration_environment = (
    f"-e AWS_REGION={region} -e DB_SECRET_ARN={master_secret} "
    f"-e APP_DB_SECRET_ARN={app_secret} -e DB_NAME=fnol"
)
logging = (
    f"--log-driver=awslogs --log-opt awslogs-region={region} "
    f"--log-opt awslogs-group={log_group}"
)
commands = [
    "set -euo pipefail",
    f"aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {registry}",
    f"docker pull {image}",
    f"docker run --rm --network host {migration_environment} {image} python tools/migrate_to_aws.py --skip-s3",
    "docker rm -f fnol-workbench fnol-runtime 2>/dev/null || true",
    (
        f"docker run -d --name fnol-runtime --restart unless-stopped --network host "
        f"--no-healthcheck {logging} --log-opt awslogs-stream=runtime {environment} "
        f"-e ENABLE_SIMULATED_FAULTS=true {image} "
        "python tools/aws_runtime.py serve --host 127.0.0.1 --port 8767"
    ),
    "sleep 3",
    (
        f"docker run -d --name fnol-workbench --restart unless-stopped --network host "
        f"{logging} --log-opt awslogs-stream=workbench {workbench_environment} "
        f"-e STORAGE_BACKEND=aws -e RUNTIME_API_URL=http://127.0.0.1:8767 {image}"
    ),
    "sleep 3",
    "curl --fail http://127.0.0.1:8767/health",
    "curl --fail http://127.0.0.1:8080/ready",
]
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"commands": commands}, handle)
PY

COMMAND_ID="$(aws ssm send-command \
  --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --comment "Deploy $IMAGE_URI" \
  --parameters "file://$PARAMETERS_FILE" \
  --query 'Command.CommandId' \
  --output text)"
aws ssm wait command-executed \
  --region "$AWS_REGION" \
  --command-id "$COMMAND_ID" \
  --instance-id "$INSTANCE_ID"
aws ssm get-command-invocation \
  --region "$AWS_REGION" \
  --command-id "$COMMAND_ID" \
  --instance-id "$INSTANCE_ID" \
  --query '{Status:Status,Output:StandardOutputContent,Errors:StandardErrorContent}'

echo "6/6 Deployment completed. Start the private browser tunnel with:"
echo "aws ssm start-session --region $AWS_REGION --target $INSTANCE_ID --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}'"
echo "Then open http://127.0.0.1:8080"

