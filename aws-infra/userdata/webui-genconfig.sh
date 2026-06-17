#!/bin/bash
# Regenerate /opt/webui/src/apigw.js from the data pipeline stack.
# Runs as systemd ExecStartPre on EVERY service start, so the config self-corrects
# after a reboot or an API redeploy. DATA_STACK / DATA_API_STAGE come from the
# environment (/etc/webui.env via the unit). Format MATCHES the build.py 'webui' task.
#
# SECURITY NOTE (DEMO ONLY): apigw.js embeds the API Gateway base URL AND the API
# key in plain text, which is served to the browser. This is acceptable for a
# demo/portfolio (the API key here is a usage-plan throttling/quota control, not an
# authN secret), but it IS publicly visible to anyone who can reach the page.
# For production, do NOT ship the key to the browser -- put auth in front of the
# API (Cognito / Lambda Authorizer) or proxy calls through a backend. Tracked as P2.
set -euo pipefail

: "${DATA_STACK:=video-analyzer-stack}"
: "${DATA_API_STAGE:=development}"

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: aws CLI not found; cannot generate apigw.js." >&2
  exit 1
fi

# region from IMDSv2
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/placement/region")

REST_API_ID=$(aws cloudformation describe-stack-resource \
  --region "${REGION}" --stack-name "${DATA_STACK}" \
  --logical-resource-id VidAnalyzerRestApi \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

API_KEY_ID=$(aws cloudformation describe-stack-resource \
  --region "${REGION}" --stack-name "${DATA_STACK}" \
  --logical-resource-id VidAnalyzerApiKey \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

API_KEY_VALUE=$(aws apigateway get-api-key \
  --region "${REGION}" --api-key "${API_KEY_ID}" --include-value \
  --query 'value' --output text)

API_BASE_URL="https://${REST_API_ID}.execute-api.${REGION}.amazonaws.com/${DATA_API_STAGE}"

mkdir -p /opt/webui/src
cat > /opt/webui/src/apigw.js <<EOF
var apiBaseUrl="${API_BASE_URL}";
var apiKey="${API_KEY_VALUE}";
EOF

echo "Wrote /opt/webui/src/apigw.js -> ${API_BASE_URL}"
