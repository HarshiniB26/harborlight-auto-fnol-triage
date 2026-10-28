#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 REVIEW_ID REVIEWER_NAME" >&2
  exit 2
fi

STACK_NAME="${STACK_NAME:-auto-fnol-pilot}"
AWS_REGION="${AWS_REGION:-us-east-1}"
REVIEW_ID="$1"
REVIEWER="$2"

if [[ ! "$REVIEW_ID" =~ ^rev_[A-Za-z0-9]+$ ]]; then
  echo "Invalid review ID." >&2
  exit 2
fi
if [[ ! "$REVIEWER" =~ ^[A-Za-z0-9._@-]{1,64}$ ]]; then
  echo "Reviewer names may contain letters, numbers, dot, underscore, @ and hyphen." >&2
  exit 2
fi

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

aws ecs execute-command \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --task "$TASK_ARN" \
  --container fnol-runtime \
  --interactive \
  --command "python -m tools.aws_runtime approve '$REVIEW_ID' --reviewer '$REVIEWER'"
