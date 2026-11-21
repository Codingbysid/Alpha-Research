#!/usr/bin/env bash
set -euo pipefail

# Configuration
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ECR_REPO="${ECR_REPO:-osint-alpha-runner}"
export LAMBDA_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-osint-short-q5-daily}"
export SCHEDULE_NAME="${SCHEDULE_NAME:-osint-short-q5-weekday-1550et}"

echo "=== OSINT Alpha Pipeline: AWS Lambda Deployment ==="

if ! command -v aws &>/dev/null; then
    echo "ERROR: aws CLI is not installed or not in PATH."
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo "ERROR: docker is not installed or not in PATH."
    exit 1
fi

# Detect AWS Account ID
if ! aws sts get-caller-identity &>/dev/null; then
    echo "ERROR: AWS credentials not found or expired."
    echo "Please configure credentials via:"
    echo "  export AWS_ACCESS_KEY_ID='your-key'"
    echo "  export AWS_SECRET_ACCESS_KEY='your-secret'"
    echo "  export AWS_DEFAULT_REGION='us-east-1'"
    echo "Or run 'aws configure'."
    exit 1
fi

export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export S3_BUCKET="${S3_BUCKET:-osint-alpha-pipeline-${AWS_ACCOUNT_ID}}"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:latest"

echo "AWS Account ID: ${AWS_ACCOUNT_ID}"
echo "Region        : ${AWS_REGION}"
echo "S3 Bucket     : ${S3_BUCKET}"
echo "ECR Image     : ${ECR_URI}"

# Step 1: Ensure S3 Bucket exists and sync data
echo "--> [1/5] Ensuring S3 Bucket s3://${S3_BUCKET} exists and syncing state..."
aws s3 mb "s3://${S3_BUCKET}" --region "${AWS_REGION}" 2>/dev/null || true
aws s3 sync data/ "s3://${S3_BUCKET}/osint-alpha/data/" --exclude "raw_archives/*"

# Step 2: Ensure ECR Repository exists & push image
echo "--> [2/5] Authenticating with ECR & pushing container image..."
aws ecr create-repository --repository-name "${ECR_REPO}" --region "${AWS_REGION}" 2>/dev/null || true
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "Building Docker image..."
docker build -f Dockerfile.lambda -t "${ECR_URI}" .

echo "Pushing Docker image to ECR..."
docker push "${ECR_URI}"

# Step 3: Ensure IAM Execution Role exists
ROLE_NAME="osint_lambda_s3_role"
echo "--> [3/5] Checking IAM role ${ROLE_NAME}..."
if ! aws iam get-role --role-name "${ROLE_NAME}" &>/dev/null; then
    echo "Creating IAM execution role ${ROLE_NAME}..."
    TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    aws iam create-role --role-name "${ROLE_NAME}" --assume-role-policy-document "${TRUST_POLICY}"
    aws iam attach-role-policy --role-name "${ROLE_NAME}" --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    
    S3_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::${S3_BUCKET}\",\"arn:aws:s3:::${S3_BUCKET}/*\"]}]}"
    aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name "OsintS3SyncPolicy" --policy-document "${S3_POLICY}"
    echo "Waiting 10s for IAM role propagation..."
    sleep 10
fi
ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Step 4: Create or Update Lambda Function
echo "--> [4/5] Deploying Lambda container function ${LAMBDA_FUNCTION_NAME}..."
ENV_JSON="{\"Variables\":{\"S3_BUCKET\":\"${S3_BUCKET}\",\"NASA_FIRMS_MAP_KEY\":\"${NASA_FIRMS_MAP_KEY:-}\",\"EIA_API_KEY\":\"${EIA_API_KEY:-}\",\"APCA_API_KEY_ID\":\"${APCA_API_KEY_ID:-}\",\"APCA_API_SECRET_KEY\":\"${APCA_API_SECRET_KEY:-}\",\"APCA_API_BASE_URL\":\"${APCA_API_BASE_URL:-https://paper-api.alpaca.markets}\",\"BASE_EQUITY\":\"${BASE_EQUITY:-100000}\"}}"

if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" &>/dev/null; then
    echo "Updating existing Lambda function code..."
    aws lambda update-function-code \
      --function-name "${LAMBDA_FUNCTION_NAME}" \
      --image-uri "${ECR_URI}"
    echo "Updating Lambda environment variables..."
    aws lambda wait function-updated --function-name "${LAMBDA_FUNCTION_NAME}" 2>/dev/null || sleep 5
    aws lambda update-function-configuration \
      --function-name "${LAMBDA_FUNCTION_NAME}" \
      --environment "${ENV_JSON}"
else
    echo "Creating new Lambda function..."
    aws lambda create-function \
      --function-name "${LAMBDA_FUNCTION_NAME}" \
      --package-type Image \
      --code ImageUri="${ECR_URI}" \
      --role "${ROLE_ARN}" \
      --architectures arm64 \
      --memory-size 2048 \
      --timeout 300 \
      --ephemeral-storage Size=1024 \
      --environment "${ENV_JSON}"
fi

# Step 5: Configure EventBridge Scheduler (Mon-Fri 15:50 ET)
echo "--> [5/5] Ensuring EventBridge Schedule ${SCHEDULE_NAME} exists..."
SCHED_ROLE_NAME="osint_eventbridge_scheduler_role"
if ! aws iam get-role --role-name "${SCHED_ROLE_NAME}" &>/dev/null; then
    SCHED_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    aws iam create-role --role-name "${SCHED_ROLE_NAME}" --assume-role-policy-document "${SCHED_TRUST}"
    LAMBDA_INVOKE_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_FUNCTION_NAME}\"}]}"
    aws iam put-role-policy --role-name "${SCHED_ROLE_NAME}" --policy-name "InvokeLambdaPolicy" --policy-document "${LAMBDA_INVOKE_POLICY}"
    sleep 5
fi
SCHED_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${SCHED_ROLE_NAME}"

aws scheduler create-schedule \
  --name "${SCHEDULE_NAME}" \
  --schedule-expression "cron(50 15 ? * MON-FRI *)" \
  --schedule-expression-timezone "America/New_York" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_FUNCTION_NAME}\",\"RoleArn\":\"${SCHED_ROLE_ARN}\"}" 2>/dev/null || \
aws scheduler update-schedule \
  --name "${SCHEDULE_NAME}" \
  --schedule-expression "cron(50 15 ? * MON-FRI *)" \
  --schedule-expression-timezone "America/New_York" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_FUNCTION_NAME}\",\"RoleArn\":\"${SCHED_ROLE_ARN}\"}"

echo "=== Deployment Successfully Completed! ==="
echo "Lambda Function : ${LAMBDA_FUNCTION_NAME}"
echo "Schedule        : Mon-Fri 15:50 America/New_York"
echo "To trigger manual test run:"
echo "  aws lambda invoke --function-name ${LAMBDA_FUNCTION_NAME} --payload '{\"skip_ingest\": true}' --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json"
