#!/bin/bash
# Upload dataset.json with the caller's AWS credentials. The EC2 role cannot
# upload; it can only GetObject the dataset and the release archive.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK="${STACK_NAME:-fnol}"
REGION="${AWS_REGION:-us-east-1}"
FILE="${1:-$ROOT/data/dev/dataset.json}"

if [ ! -f "$FILE" ]; then
  echo "Dataset file not found: $FILE" >&2
  exit 1
fi

python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$FILE"

output_value() {
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

BUCKET="$(output_value DatasetBucketName)"
KEY="$(output_value DatasetObjectKey)"
if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "Stack ${STACK} did not output a dataset bucket." >&2
  exit 1
fi

aws s3 cp "$FILE" "s3://${BUCKET}/${KEY}" \
  --region "$REGION" \
  --content-type application/json \
  --sse AES256

aws s3api head-object \
  --region "$REGION" \
  --bucket "$BUCKET" \
  --key "$KEY" \
  --query '{Key:Key,ContentType:ContentType,VersionId:VersionId,ServerSideEncryption:ServerSideEncryption}' \
  --output table

echo "Uploaded s3://${BUCKET}/${KEY}. The object is not public."
echo "A running application keeps its current copy until restart or POST /internal/dataset/refresh on 127.0.0.1:8767."
