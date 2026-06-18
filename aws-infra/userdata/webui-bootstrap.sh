#!/bin/bash
# First-boot installer for the webui instance (FastAPI/uvicorn: login + browser capture).
# Invoked by the thin-loader UserData with:
#   WEBUI_PORT, DATA_STACK, DATA_API_STAGE, ARTIFACT_BUCKET, ARTIFACT_PREFIX,
#   KINESIS_STREAM, WEBUI_AUTH_SSM_PREFIX
set -xeuo pipefail
exec > /var/log/webui-bootstrap.log 2>&1

: "${WEBUI_PORT:=8080}"
: "${DATA_STACK:=video-analyzer-stack}"
: "${DATA_API_STAGE:=development}"
: "${ARTIFACT_BUCKET:?ARTIFACT_BUCKET is required}"
: "${ARTIFACT_PREFIX:?ARTIFACT_PREFIX is required}"
: "${KINESIS_STREAM:=FrameStream}"
: "${WEBUI_AUTH_SSM_PREFIX:=/video-analyzer/webui}"

S3_BASE="s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}webui"

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: aws CLI not found on PATH. Aborting." >&2
  exit 1
fi

# 1. Python 3.12 preferred (matches Lambda runtime); fall back to distro python3.
dnf install -y python3.12 python3.12-pip || dnf install -y python3 python3-pip
PYBIN="$(command -v python3.12 || command -v python3)"
# openssl for the self-signed TLS cert (getUserMedia requires a secure context).
command -v openssl >/dev/null 2>&1 || dnf install -y openssl

# 2. region + public IP from IMDSv2 (public IP -> cert SAN so https://<ip> matches).
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/placement/region")
PUBIP=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/public-ipv4" || true)

# 3. non-root system user
id -u webui >/dev/null 2>&1 || useradd -r -s /sbin/nologin webui

# 4. fetch + unpack the app (frontend + backend) into /opt/webui (wipe first).
rm -rf /opt/webui
mkdir -p /opt/webui
aws s3 cp "${S3_BASE}/web-ui.tgz" /tmp/web-ui.tgz
tar -xzf /tmp/web-ui.tgz -C /opt/webui
rm -f /tmp/web-ui.tgz

# 5. systemd unit
aws s3 cp "${S3_BASE}/webui.service" /etc/systemd/system/webui.service

# 6. virtualenv + dependencies
"${PYBIN}" -m venv /opt/webui/venv
/opt/webui/venv/bin/pip install --upgrade pip
/opt/webui/venv/bin/pip install -r /opt/webui/backend/requirements.txt

# 7. self-signed TLS cert (1 year), SAN includes localhost + the instance public IP.
mkdir -p /etc/webui
SAN="subjectAltName=DNS:localhost,IP:127.0.0.1"
[ -n "${PUBIP}" ] && SAN="${SAN},IP:${PUBIP}"
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout /etc/webui/tls.key -out /etc/webui/tls.crt \
  -subj "/CN=${PUBIP:-localhost}" -addext "${SAN}"

# 8. login secrets from SSM Parameter Store (SecureString) -- never in git/UserData.
AUTH_USER=$(aws ssm get-parameter --region "${REGION}" \
  --name "${WEBUI_AUTH_SSM_PREFIX}/auth-username" --query Parameter.Value --output text)
AUTH_HASH=$(aws ssm get-parameter --region "${REGION}" \
  --name "${WEBUI_AUTH_SSM_PREFIX}/password-hash" --with-decryption --query Parameter.Value --output text)
SESSION_SECRET=$(aws ssm get-parameter --region "${REGION}" \
  --name "${WEBUI_AUTH_SSM_PREFIX}/session-secret" --with-decryption --query Parameter.Value --output text)

# 9. environment file consumed by the unit + backend (config.py reads os.environ).
#    AUTH_HASH contains '$' chars; it is written via ${VAR} expansion (single pass,
#    NOT re-expanded), so the pbkdf2 string is preserved verbatim.
cat > /etc/webui.env <<EOF
WEBUI_PORT=${WEBUI_PORT}
AWS_DEFAULT_REGION=${REGION}
KINESIS_STREAM=${KINESIS_STREAM}
DATA_STACK=${DATA_STACK}
DATA_API_STAGE=${DATA_API_STAGE}
WEBUI_AUTH_USERNAME=${AUTH_USER}
WEBUI_AUTH_PASSWORD_HASH=${AUTH_HASH}
WEBUI_SESSION_SECRET=${SESSION_SECRET}
WEBUI_SESSION_HTTPS_ONLY=true
EOF
chmod 600 /etc/webui.env

# 10. ownership: the webui user must read the app, cert key, and env file.
chown -R webui:webui /opt/webui
chown webui:webui /etc/webui/tls.key /etc/webui/tls.crt /etc/webui.env
chmod 640 /etc/webui/tls.key

systemctl daemon-reload
systemctl enable --now webui.service
echo "webui bootstrap complete (HTTPS port ${WEBUI_PORT})"
