#!/usr/bin/env bash
set -euo pipefail

# Creates/updates infrastructure, publishes an immutable ARM64 image, uploads
# evidence, migrates PostgreSQL, and starts the private ECS Fargate service.

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

deploy_stack() {
  local desired_count="$1"
  local image_tag="$2"
  aws cloudformation deploy \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --template-file deployment/cloudformation.yaml \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
      ProjectName="$PROJECT_NAME" \
      ImageTag="$image_tag" \
      ServiceDesiredCount="$desired_count" \
    --tags Project="$PROJECT_NAME" Team=team1
}

output() {
  aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

echo "1/8 Creating or updating private AWS infrastructure..."
# A zero desired count lets the repository and task definitions exist before
# the first image is pushed, and safely stops the old service during migration.
deploy_stack 0 bootstrap

BUCKET="$(output EvidenceBucketName)"
REPOSITORY_URI="$(output RepositoryUri)"
CLUSTER="$(output ClusterName)"
SERVICE="$(output ServiceName)"
PRIVATE_SUBNET="$(output PrivateAppSubnetId)"
TASK_SECURITY_GROUP="$(output ApplicationSecurityGroupId)"
MIGRATION_TASK_DEFINITION="$(output MigrationTaskDefinitionArn)"
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
REGISTRY="${REPOSITORY_URI%/*}"

echo "2/8 Building the immutable ARM64 application image..."
docker build --pull --platform linux/arm64 --tag "$IMAGE_URI" .

echo "3/8 Pushing the image to Amazon ECR..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker push "$IMAGE_URI"

echo "4/8 Registering task definitions for image ${IMAGE_TAG}..."
deploy_stack 0 "$IMAGE_TAG"
MIGRATION_TASK_DEFINITION="$(output MigrationTaskDefinitionArn)"

echo "5/8 Uploading immutable evidence and the dataset manifest..."
# Upload referenced files first. Uploading dataset.json last publishes the S3
# event only after all evidence is available to the runtime.
aws s3 sync data/dev/documents "s3://${BUCKET}/documents/" \
  --region "$AWS_REGION" --sse AES256
aws s3 sync data/dev/policies "s3://${BUCKET}/policies/" \
  --region "$AWS_REGION" --sse AES256
aws s3 cp data/dev/dataset.json "s3://${BUCKET}/dataset.json" \
  --region "$AWS_REGION" --sse AES256 --content-type application/json

echo "6/8 Running the one-off PostgreSQL migration task..."
NETWORK_CONFIGURATION="awsvpcConfiguration={subnets=[$PRIVATE_SUBNET],securityGroups=[$TASK_SECURITY_GROUP],assignPublicIp=DISABLED}"
MIGRATION_TASK_ARN="$(aws ecs run-task \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --task-definition "$MIGRATION_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK_CONFIGURATION" \
  --query 'tasks[0].taskArn' \
  --output text)"

if [[ -z "$MIGRATION_TASK_ARN" || "$MIGRATION_TASK_ARN" == "None" ]]; then
  echo "ECS did not start the migration task." >&2
  exit 1
fi

aws ecs wait tasks-stopped \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --tasks "$MIGRATION_TASK_ARN"

MIGRATION_EXIT_CODE="$(aws ecs describe-tasks \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --tasks "$MIGRATION_TASK_ARN" \
  --query "tasks[0].containers[?name=='fnol-migration'].exitCode | [0]" \
  --output text)"

if [[ "$MIGRATION_EXIT_CODE" != "0" ]]; then
  aws ecs describe-tasks \
    --region "$AWS_REGION" \
    --cluster "$CLUSTER" \
    --tasks "$MIGRATION_TASK_ARN" \
    --query 'tasks[0].{StoppedReason:stoppedReason,Containers:containers[*].{Name:name,Reason:reason,ExitCode:exitCode}}'
  echo "PostgreSQL migration failed with exit code ${MIGRATION_EXIT_CODE}." >&2
  exit 1
fi

echo "7/8 Starting the private Fargate service..."
deploy_stack 1 "$IMAGE_TAG"
aws ecs wait services-stable \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --services "$SERVICE"

echo "8/8 Deployment completed successfully."
echo "Run ./deployment/start-tunnel.sh and open http://127.0.0.1:18080"
