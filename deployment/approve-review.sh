#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 INSTANCE_ID REVIEW_ID REVIEWER_NAME" >&2
  exit 2
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
INSTANCE_ID="$1"
REVIEW_ID="$2"
REVIEWER="$3"

if [[ ! "$INSTANCE_ID" =~ ^i-[a-f0-9]+$ ]]; then
  echo "Invalid EC2 instance ID." >&2
  exit 2
fi
if [[ ! "$REVIEW_ID" =~ ^rev_[A-Za-z0-9]+$ ]]; then
  echo "Invalid review ID." >&2
  exit 2
fi
if [[ ! "$REVIEWER" =~ ^[A-Za-z0-9._@-]{1,64}$ ]]; then
  echo "Reviewer names may contain letters, numbers, dot, underscore, @ and hyphen." >&2
  exit 2
fi

COMMAND_ID="$(aws ssm send-command \
  --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands="docker exec fnol-runtime python tools/aws_runtime.py approve '$REVIEW_ID' --reviewer '$REVIEWER'" \
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

