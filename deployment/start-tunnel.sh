#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-auto-fnol-pilot}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LOCAL_PORT="${LOCAL_PORT:-18080}"

output() {
  aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

CLUSTER="$(output ClusterName)"
SERVICE="$(output ServiceName)"
TASK_ARN="$(aws ecs list-tasks \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --service-name "$SERVICE" \
  --desired-status RUNNING \
  --query 'taskArns[0]' \
  --output text)"

if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "No running Fargate task was found." >&2
  exit 1
fi

TASK_ID="${TASK_ARN##*/}"
RUNTIME_ID="$(aws ecs describe-tasks \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?name=='fnol-workbench'].runtimeId | [0]" \
  --output text)"

if [[ -z "$RUNTIME_ID" || "$RUNTIME_ID" == "None" ]]; then
  echo "The workbench container runtime ID is not available yet." >&2
  exit 1
fi

TARGET="ecs:${CLUSTER}_${TASK_ID}_${RUNTIME_ID}"
echo "Forwarding http://127.0.0.1:${LOCAL_PORT} to the private workbench."
echo "Keep this terminal open until the demo is finished."
aws ssm start-session \
  --region "$AWS_REGION" \
  --target "$TARGET" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
