#!/bin/bash
# First-boot installer for the KPI dashboard instance. Invoked by the thin-loader
# UserData with: KPI_PORT, DATA_STACK, ARTIFACT_BUCKET, ARTIFACT_PREFIX
set -xeuo pipefail
exec > /var/log/kpi-bootstrap.log 2>&1

: "${KPI_PORT:=8000}"
: "${DATA_STACK:=video-analyzer-stack}"
: "${ARTIFACT_BUCKET:?ARTIFACT_BUCKET is required}"
: "${ARTIFACT_PREFIX:?ARTIFACT_PREFIX is required}"

S3_BASE="s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}kpi"

# 0. defensive aws CLI check
if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: aws CLI not found on PATH. Cannot fetch artifacts. Aborting." >&2
  exit 1
fi

# 1. Python: prefer python3.12 (matches the Lambda runtime); fall back to the
#    distro python3 so the demo still runs if python3.12 is unavailable.
dnf install -y python3.12 python3.12-pip || dnf install -y python3 python3-pip
PYBIN="$(command -v python3.12 || command -v python3)"
echo "Using Python interpreter: ${PYBIN}"

# 2. region for boto3 (PR 4); boto3 also auto-detects on EC2, but be explicit
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/placement/region")

# 3. non-root system user
id -u kpi >/dev/null 2>&1 || useradd -r -s /sbin/nologin kpi

# 4. fetch + unpack into /opt/kpi (wipe first so removed files don't linger)
rm -rf /opt/kpi
mkdir -p /opt/kpi
aws s3 cp "${S3_BASE}/kpi-dashboard.tgz" /tmp/kpi-dashboard.tgz
tar -xzf /tmp/kpi-dashboard.tgz -C /opt/kpi
rm -f /tmp/kpi-dashboard.tgz

# 5. systemd unit
aws s3 cp "${S3_BASE}/kpi-dashboard.service" /etc/systemd/system/kpi-dashboard.service

# 6. virtualenv + dependencies
"${PYBIN}" -m venv /opt/kpi/venv
/opt/kpi/venv/bin/pip install --upgrade pip
/opt/kpi/venv/bin/pip install -r /opt/kpi/backend/requirements.txt

# 7. environment file consumed by the unit
cat > /etc/kpi-dashboard.env <<EOF
KPI_PORT=${KPI_PORT}
DATA_STACK=${DATA_STACK}
AWS_DEFAULT_REGION=${REGION}
EOF

# 8. ownership + start (enabled -> survives reboot)
chown -R kpi:kpi /opt/kpi
systemctl daemon-reload
systemctl enable --now kpi-dashboard.service
echo "kpi bootstrap complete (port ${KPI_PORT})"
