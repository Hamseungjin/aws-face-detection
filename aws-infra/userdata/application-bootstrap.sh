#!/bin/bash
# ApplicationInstance first-boot / re-run installer (Phase A parallel host).
#
# Co-locates on ONE EC2:
#   Nginx :443 (public TLS)  →  FastAPI 127.0.0.1:8080  +  KPI 127.0.0.1:8000
#   SQLite on dedicated EBS mounted at /data/kiosk
#
# Invoked by thin-loader UserData with env:
#   ARTIFACT_BUCKET, ARTIFACT_PREFIX
#   WEBUI_AUTH_SSM_PREFIX
#   DATA_VOLUME_DEVICE (CFN attachment hint, default /dev/xvdf)
#   DATA_VOLUME_ID (optional vol-... for by-id match; omit to avoid CFN circular deps)
#   WEBUI_PORT (default 8080), KPI_PORT (default 8000)
#
# Idempotent: safe to re-run (will NOT re-format a filesystem that already exists).
set -xeuo pipefail
exec > /var/log/application-bootstrap.log 2>&1

: "${ARTIFACT_BUCKET:?ARTIFACT_BUCKET is required}"
: "${ARTIFACT_PREFIX:?ARTIFACT_PREFIX is required}"

: "${DATA_VOLUME_DEVICE:=/dev/xvdf}"
: "${DATA_VOLUME_ID:=}"
: "${WEBUI_PORT:=8080}"
: "${KPI_PORT:=8000}"
: "${WEBUI_AUTH_SSM_PREFIX:=/video-analyzer/webui}"
: "${KIOSK_DB_PATH:=/data/kiosk/kiosk.db}"
: "${FACE_SIMILARITY_THRESHOLD:=90}"
: "${REKOGNITION_FACE_COLLECTION_ID:=kiosk-face-collection}"

S3_APP="s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}application"
S3_WEBUI="s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}webui"
S3_KPI="s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}kpi"

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: aws CLI not found on PATH. Aborting." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 0. Packages
# ---------------------------------------------------------------------------
dnf install -y python3.12 python3.12-pip || dnf install -y python3 python3-pip
PYBIN="$(command -v python3.12 || command -v python3)"
command -v openssl >/dev/null 2>&1 || dnf install -y openssl
command -v nginx >/dev/null 2>&1 || dnf install -y nginx
command -v xfs_admin >/dev/null 2>&1 || dnf install -y xfsprogs
command -v lsblk >/dev/null 2>&1 || dnf install -y util-linux

# ---------------------------------------------------------------------------
# 1. Region + public IP (IMDSv2)
# ---------------------------------------------------------------------------
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/placement/region")
INSTANCE_ID=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/instance-id")

# Public IPv4 may change when the EIP associates after first boot; wait briefly.
PUBIP=""
for _ in $(seq 1 30); do
  PUBIP=$(curl -sS -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "http://169.254.169.254/latest/meta-data/public-ipv4" 2>/dev/null || true)
  if [ -n "${PUBIP}" ]; then
    break
  fi
  sleep 2
done
echo "region=${REGION} instance_id=${INSTANCE_ID} public_ip=${PUBIP:-none}"

# ---------------------------------------------------------------------------
# 2. Locate data volume block device (Nitro-safe)
# ---------------------------------------------------------------------------
# CloudFormation attaches Device: /dev/xvdf (DATA_VOLUME_DEVICE). On Nitro the
# real path is often /dev/nvme1n1. We intentionally do NOT !Ref the volume from
# UserData (volume AZ depends on the instance → circular dependency).
#
# Discovery order:
#   1) by-id using optional DATA_VOLUME_ID (vol-xxx, dashes stripped)
#   2) DATA_VOLUME_DEVICE and common aliases
#   3) first non-root disk that appears after root is mounted
find_data_device() {
  local candidate vol_nodash name serial root_src root_disk
  root_src=$(findmnt -n -o SOURCE / 2>/dev/null || true)
  root_disk=""
  if [ -n "${root_src}" ]; then
    root_disk=$(lsblk -no PKNAME "${root_src}" 2>/dev/null || true)
    if [ -z "${root_disk}" ]; then
      root_disk=$(basename "${root_src}")
    fi
  fi

  if [ -n "${DATA_VOLUME_ID}" ]; then
    vol_nodash="${DATA_VOLUME_ID#vol-}"
    vol_nodash="${vol_nodash//-/}"
    for candidate in \
      "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol${vol_nodash}" \
      "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol${vol_nodash}-ns-1"
    do
      if [ -e "${candidate}" ]; then
        readlink -f "${candidate}"
        return 0
      fi
    done
    while read -r name serial; do
      [ -z "${name}" ] && continue
      case "${serial}" in
        *"${vol_nodash}"*)
          echo "/dev/${name}"
          return 0
          ;;
      esac
    done < <(lsblk -dn -o NAME,SERIAL 2>/dev/null || true)
  fi

  for candidate in \
    "${DATA_VOLUME_DEVICE}" \
    "/dev/xvdf" \
    "/dev/sdf" \
    "/dev/nvme1n1"
  do
    if [ -e "${candidate}" ]; then
      readlink -f "${candidate}"
      return 0
    fi
  done

  # Last resort: any disk node that is not the root disk and has no mountpoint.
  while read -r name; do
    [ -z "${name}" ] && continue
    if [ -n "${root_disk}" ] && [ "${name}" = "${root_disk}" ]; then
      continue
    fi
    if [ -z "$(lsblk -ndo MOUNTPOINT "/dev/${name}" 2>/dev/null || true)" ]; then
      # Prefer whole disks (no partition suffix quirks for NVMe: nvme0n1 ok)
      echo "/dev/${name}"
      return 0
    fi
  done < <(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}')
  return 1
}

echo "Waiting for data volume (device hint ${DATA_VOLUME_DEVICE}, id=${DATA_VOLUME_ID:-unknown})..."
DATA_DEV=""
for _ in $(seq 1 90); do
  if DATA_DEV=$(find_data_device); then
    echo "Found data device: ${DATA_DEV}"
    break
  fi
  sleep 2
done
if [ -z "${DATA_DEV}" ]; then
  echo "ERROR: data volume block device not found (attachment may still be pending)." >&2
  lsblk -o NAME,SIZE,TYPE,SERIAL,MOUNTPOINT || true
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Filesystem: format ONLY if unformatted (never wipe existing data)
# ---------------------------------------------------------------------------
if blkid "${DATA_DEV}" >/dev/null 2>&1; then
  echo "Filesystem already present on ${DATA_DEV}; will not format."
  blkid "${DATA_DEV}" || true
else
  echo "No filesystem on ${DATA_DEV}; creating XFS (one-time)."
  mkfs.xfs -f "${DATA_DEV}"
fi

DATA_UUID=$(blkid -s UUID -o value "${DATA_DEV}")
if [ -z "${DATA_UUID}" ]; then
  echo "ERROR: could not read UUID from ${DATA_DEV}" >&2
  exit 1
fi
echo "data volume UUID=${DATA_UUID}"

# ---------------------------------------------------------------------------
# 4. Mount /data/kiosk and persist via UUID in fstab
# ---------------------------------------------------------------------------
mkdir -p /data/kiosk
FSTAB_LINE="UUID=${DATA_UUID} /data/kiosk xfs defaults,nofail 0 2"
if grep -qE '[[:space:]]/data/kiosk[[:space:]]' /etc/fstab 2>/dev/null; then
  # Replace any existing /data/kiosk line so UUID stays correct after re-attach.
  tmp_fstab=$(mktemp)
  grep -vE '[[:space:]]/data/kiosk[[:space:]]' /etc/fstab > "${tmp_fstab}" || true
  echo "${FSTAB_LINE}" >> "${tmp_fstab}"
  cat "${tmp_fstab}" > /etc/fstab
  rm -f "${tmp_fstab}"
else
  echo "${FSTAB_LINE}" >> /etc/fstab
fi

if ! mountpoint -q /data/kiosk; then
  mount /data/kiosk
fi
if ! mountpoint -q /data/kiosk; then
  echo "ERROR: /data/kiosk is not mounted (fail closed)." >&2
  exit 1
fi
# Prove we are not on the root filesystem.
ROOT_SRC=$(findmnt -n -o SOURCE /)
DATA_SRC=$(findmnt -n -o SOURCE /data/kiosk)
if [ "${ROOT_SRC}" = "${DATA_SRC}" ]; then
  echo "ERROR: /data/kiosk resolves to root device ${ROOT_SRC}; refusing to continue." >&2
  exit 1
fi
echo "Mounted /data/kiosk from ${DATA_SRC}"

# ---------------------------------------------------------------------------
# 5. System users
# ---------------------------------------------------------------------------
id -u webui >/dev/null 2>&1 || useradd -r -s /sbin/nologin webui
id -u kpi >/dev/null 2>&1 || useradd -r -s /sbin/nologin kpi

chown webui:webui /data/kiosk
chmod 750 /data/kiosk

# ---------------------------------------------------------------------------
# 6. Fetch application artifacts (reuse publishapps webui + kpi tarballs)
# ---------------------------------------------------------------------------
rm -rf /opt/webui
mkdir -p /opt/webui
aws s3 cp "${S3_WEBUI}/web-ui.tgz" /tmp/web-ui.tgz
tar -xzf /tmp/web-ui.tgz -C /opt/webui
rm -f /tmp/web-ui.tgz

rm -rf /opt/kpi
mkdir -p /opt/kpi
aws s3 cp "${S3_KPI}/kpi-dashboard.tgz" /tmp/kpi-dashboard.tgz
tar -xzf /tmp/kpi-dashboard.tgz -C /opt/kpi
rm -f /tmp/kpi-dashboard.tgz

# Units + nginx config from application artifact prefix
mkdir -p /opt/app
aws s3 cp "${S3_APP}/kiosk-fastapi.service" /etc/systemd/system/kiosk-fastapi.service
aws s3 cp "${S3_APP}/kpi-dashboard.service" /etc/systemd/system/kpi-dashboard.service
aws s3 cp "${S3_APP}/nginx-application.conf" /etc/nginx/conf.d/application.conf
# Keep a local copy of bootstrap for re-runs via SSM
aws s3 cp "${S3_APP}/bootstrap.sh" /opt/app/bootstrap.sh || true
chmod +x /opt/app/bootstrap.sh 2>/dev/null || true

# ---------------------------------------------------------------------------
# 7. Separate Python venvs
# ---------------------------------------------------------------------------
"${PYBIN}" -m venv /opt/webui/venv
/opt/webui/venv/bin/pip install --upgrade pip
/opt/webui/venv/bin/pip install -r /opt/webui/backend/requirements.txt

"${PYBIN}" -m venv /opt/kpi/venv
/opt/kpi/venv/bin/pip install --upgrade pip
/opt/kpi/venv/bin/pip install -r /opt/kpi/backend/requirements.txt

# ---------------------------------------------------------------------------
# 8. Auth secrets from SSM (never from CFN UserData plaintext)
# ---------------------------------------------------------------------------
AUTH_USER=$(aws ssm get-parameter --region "${REGION}" \
  --name "${WEBUI_AUTH_SSM_PREFIX}/auth-username" --query Parameter.Value --output text)
AUTH_HASH=$(aws ssm get-parameter --region "${REGION}" \
  --name "${WEBUI_AUTH_SSM_PREFIX}/password-hash" --with-decryption --query Parameter.Value --output text)
SESSION_SECRET=$(aws ssm get-parameter --region "${REGION}" \
  --name "${WEBUI_AUTH_SSM_PREFIX}/session-secret" --with-decryption --query Parameter.Value --output text)

# ---------------------------------------------------------------------------
# 9. Environment files (KIOSK_DB_PATH is mandatory — no root-disk fallback)
# ---------------------------------------------------------------------------
cat > /etc/webui.env <<EOF
WEBUI_PORT=${WEBUI_PORT}
AWS_DEFAULT_REGION=${REGION}
AWS_REGION=${REGION}
FACE_SIMILARITY_THRESHOLD=${FACE_SIMILARITY_THRESHOLD}
REKOGNITION_FACE_COLLECTION_ID=${REKOGNITION_FACE_COLLECTION_ID}
KIOSK_DB_PATH=${KIOSK_DB_PATH}
WEBUI_AUTH_USERNAME=${AUTH_USER}
WEBUI_AUTH_PASSWORD_HASH=${AUTH_HASH}
WEBUI_SESSION_SECRET=${SESSION_SECRET}
WEBUI_SESSION_HTTPS_ONLY=true
EOF
chmod 600 /etc/webui.env
chown webui:webui /etc/webui.env

# Ensure DB parent is the mounted volume (path must stay under /data/kiosk).
case "${KIOSK_DB_PATH}" in
  /data/kiosk/*) ;;
  *)
    echo "ERROR: KIOSK_DB_PATH must be under /data/kiosk (got ${KIOSK_DB_PATH})" >&2
    exit 1
    ;;
esac
# Touch ownership on mount; SQLite file is created by the app on first start.
chown webui:webui /data/kiosk
chmod 750 /data/kiosk

cat > /etc/kpi-dashboard.env <<EOF
KPI_PORT=${KPI_PORT}
AWS_DEFAULT_REGION=${REGION}
AWS_REGION=${REGION}
EOF
chmod 640 /etc/kpi-dashboard.env
chown root:kpi /etc/kpi-dashboard.env

chown -R webui:webui /opt/webui
chown -R kpi:kpi /opt/kpi

# ---------------------------------------------------------------------------
# 10. TLS cert for Nginx (self-signed demo). SAN uses public IP when known.
#     Browser will warn; getUserMedia still requires HTTPS origin.
#     Helper: /opt/app/regenerate-tls.sh after EIP association if SAN mismatches.
# ---------------------------------------------------------------------------
mkdir -p /etc/nginx/tls
install_tls_cert() {
  local ip="${1:-}"
  local san="subjectAltName=DNS:localhost,IP:127.0.0.1"
  local cn="localhost"
  if [ -n "${ip}" ]; then
    san="${san},IP:${ip}"
    cn="${ip}"
  fi
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout /etc/nginx/tls/application.key \
    -out /etc/nginx/tls/application.crt \
    -subj "/CN=${cn}" -addext "${san}"
  chmod 640 /etc/nginx/tls/application.key
  chmod 644 /etc/nginx/tls/application.crt
  chown root:nginx /etc/nginx/tls/application.key /etc/nginx/tls/application.crt 2>/dev/null \
    || chown root:root /etc/nginx/tls/application.key /etc/nginx/tls/application.crt
}

if [ ! -f /etc/nginx/tls/application.crt ] || [ ! -f /etc/nginx/tls/application.key ]; then
  install_tls_cert "${PUBIP}"
else
  echo "TLS cert already present; leaving in place (use /opt/app/regenerate-tls.sh to refresh SAN)."
fi

# Install bounded TLS SAN refresh helper (EIP association race fix).
# Preferred path: published artifact; fallback: embedded regenerate wrapper.
if aws s3 cp "${S3_APP}/application-tls-refresh.sh" /opt/app/application-tls-refresh.sh 2>/dev/null; then
  chmod 755 /opt/app/application-tls-refresh.sh
else
  # Fallback if publishapps application did not include the script yet.
  cat > /opt/app/application-tls-refresh.sh <<'TLSREF'
#!/bin/bash
set -euo pipefail
CERT=/etc/nginx/tls/application.crt
KEY=/etc/nginx/tls/application.key
MAX_ATTEMPTS="${TLS_REFRESH_MAX_ATTEMPTS:-10}"
SLEEP_SECS="${TLS_REFRESH_SLEEP_SECS:-30}"
log() { echo "tls-refresh: $*"; }
get_public_ip() {
  local token ip
  token=$(curl -sS -m 3 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
  [ -z "${token}" ] && return 1
  ip=$(curl -sS -m 3 -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/public-ipv4" 2>/dev/null || true)
  [ -n "${ip}" ] && [[ "${ip}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && echo "${ip}" && return 0
  return 1
}
cert_contains_ip() {
  local ip="$1"
  [ -f "${CERT}" ] || return 1
  openssl x509 -in "${CERT}" -noout -ext subjectAltName 2>/dev/null | grep -qE "IP Address:${ip}([^0-9]|$)" && return 0
  openssl x509 -in "${CERT}" -noout -subject 2>/dev/null | grep -qE "CN[[:space:]]*=[[:space:]]*${ip}([^0-9]|$)" && return 0
  return 1
}
install_cert() {
  local ip="$1"
  mkdir -p /etc/nginx/tls
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "${KEY}" -out "${CERT}" \
    -subj "/CN=${ip}" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${ip}"
  chmod 640 "${KEY}"; chmod 644 "${CERT}"
  chown root:nginx "${KEY}" "${CERT}" 2>/dev/null || chown root:root "${KEY}" "${CERT}"
}
for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
  if ! public_ip=$(get_public_ip); then
    log "public_ip_present=false attempt=${attempt}"
    [ -f "${CERT}" ] && log "tls_refresh_needed=false reason=no_public_ip_keep_existing_cert" && exit 0
    [ "${attempt}" -lt "${MAX_ATTEMPTS}" ] && sleep "${SLEEP_SECS}" && continue
    log "tls_refresh_needed=false reason=giving_up"; exit 0
  fi
  log "public_ip_present=true attempt=${attempt}"
  if cert_contains_ip "${public_ip}"; then
    log "tls_refresh_needed=false reason=san_already_matches"; exit 0
  fi
  log "tls_refresh_needed=true reason=san_mismatch_or_missing_cert"
  install_cert "${public_ip}"
  command -v nginx >/dev/null && nginx -t && (systemctl reload nginx || true)
  log "tls_refresh_needed=false reason=regenerated_ok"; exit 0
done
exit 0
TLSREF
  chmod 755 /opt/app/application-tls-refresh.sh
fi
# Compat alias for operators / prior docs
ln -sfn /opt/app/application-tls-refresh.sh /opt/app/regenerate-tls.sh

if aws s3 cp "${S3_APP}/application-tls-refresh.service" /etc/systemd/system/application-tls-refresh.service 2>/dev/null; then
  :
else
  cat > /etc/systemd/system/application-tls-refresh.service <<'UNIT'
[Unit]
Description=Refresh Nginx self-signed TLS SAN for current public IPv4 (EIP race fix)
After=network-online.target nginx.service
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/app/application-tls-refresh.sh
Nice=10
[Install]
WantedBy=multi-user.target
UNIT
fi

# Disable default nginx welcome site if present (AL2023 may use conf.d only).
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true
if [ -f /etc/nginx/nginx.conf ]; then
  # Ensure conf.d is included (default on AL2023).
  grep -q 'conf.d/\*.conf' /etc/nginx/nginx.conf || \
    echo 'include /etc/nginx/conf.d/*.conf;' >> /etc/nginx/nginx.conf
fi

nginx -t

# ---------------------------------------------------------------------------
# 11. Enable + start services (order: backends then nginx, then TLS refresh)
# ---------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable kiosk-fastapi.service kpi-dashboard.service nginx.service application-tls-refresh.service
systemctl restart kiosk-fastapi.service
systemctl restart kpi-dashboard.service
# Give uvicorn a moment before nginx health probes
sleep 2
systemctl restart nginx.service
# Bounded SAN refresh (retries if EIP not yet associated). Non-fatal if it waits.
systemctl start application-tls-refresh.service || true

# ---------------------------------------------------------------------------
# 12. Local health checks (loopback only — does not touch AWS data plane)
# ---------------------------------------------------------------------------
fail=0
if ! mountpoint -q /data/kiosk; then
  echo "HEALTH FAIL: /data/kiosk not mounted" >&2
  fail=1
fi
if ! curl -sf --max-time 5 "http://127.0.0.1:${WEBUI_PORT}/healthz" >/dev/null; then
  echo "HEALTH FAIL: FastAPI /healthz on 127.0.0.1:${WEBUI_PORT}" >&2
  systemctl status kiosk-fastapi.service --no-pager || true
  fail=1
fi
if ! curl -sf --max-time 5 "http://127.0.0.1:${KPI_PORT}/healthz" >/dev/null; then
  echo "HEALTH FAIL: KPI /healthz on 127.0.0.1:${KPI_PORT}" >&2
  systemctl status kpi-dashboard.service --no-pager || true
  fail=1
fi
if ! curl -skf --max-time 5 "https://127.0.0.1/healthz" >/dev/null; then
  echo "HEALTH FAIL: Nginx HTTPS /healthz" >&2
  systemctl status nginx.service --no-pager || true
  fail=1
fi

if [ "${fail}" -ne 0 ]; then
  echo "application bootstrap finished WITH health failures (see above)" >&2
  exit 1
fi

echo "application bootstrap complete"
echo "  FastAPI  127.0.0.1:${WEBUI_PORT}"
echo "  KPI      127.0.0.1:${KPI_PORT}"
echo "  Nginx    :443 (self-signed)"
echo "  SQLite   ${KIOSK_DB_PATH} on device ${DATA_DEV}"
echo "  Public   https://<ApplicationElasticIp>/  (not /proxy/8080)"
