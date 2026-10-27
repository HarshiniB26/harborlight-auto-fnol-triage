#!/bin/bash
# Provision the FNOL CloudFormation stack. This script does not upload the
# dataset, deploy the application, or print secret values.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK="${STACK_NAME:-fnol}"
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${FNOL_ENVIRONMENT:-dev}"
PROJECT="${FNOL_PROJECT:-fnol}"
CIDR="${FNOL_ALLOWED_CIDR:-203.0.113.10/32}"

overrides=(
  "ProjectName=${PROJECT}"
  "Environment=${ENVIRONMENT}"
  "AllowedCidr=${CIDR}"
)

if [ -n "${FNOL_INSTANCE_TYPE:-}" ]; then overrides+=("InstanceType=${FNOL_INSTANCE_TYPE}"); fi
if [ -n "${FNOL_DB_INSTANCE_CLASS:-}" ]; then overrides+=("DBInstanceClass=${FNOL_DB_INSTANCE_CLASS}"); fi
if [ -n "${FNOL_DB_STORAGE:-}" ]; then overrides+=("DBAllocatedStorage=${FNOL_DB_STORAGE}"); fi
if [ -n "${FNOL_DB_BACKUP_DAYS:-}" ]; then overrides+=("DBBackupRetention=${FNOL_DB_BACKUP_DAYS}"); fi
if [ -n "${FNOL_DOMAIN:-}" ]; then overrides+=("DomainName=${FNOL_DOMAIN}"); fi
if [ -n "${FNOL_HOSTED_ZONE_ID:-}" ]; then overrides+=("HostedZoneId=${FNOL_HOSTED_ZONE_ID}"); fi
if [ -n "${FNOL_ENABLE_BEDROCK:-}" ]; then overrides+=("EnableBedrock=${FNOL_ENABLE_BEDROCK}"); fi
if [ -n "${FNOL_BEDROCK_MODEL_ARN:-}" ]; then overrides+=("BedrockModelArn=${FNOL_BEDROCK_MODEL_ARN}"); fi
if [ -n "${FNOL_MULTI_AZ:-}" ]; then overrides+=("MultiAZ=${FNOL_MULTI_AZ}"); fi

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$ROOT/infra/cloudformation/fnol.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "${overrides[@]}" \
  --no-fail-on-empty-changeset

echo "Stack ${STACK} is ready in ${REGION}."
echo "Next: scripts/upload-dataset.sh, then populate the auth secret before a production application deploy."
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs" \
  --output table
