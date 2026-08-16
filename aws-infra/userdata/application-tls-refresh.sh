#!/bin/bash
# application-tls-refresh.sh
#
# Idempotent TLS SAN refresh for Nginx self-signed demo certs.
# Fixes first-boot race: cert may be generated with temporary public IPv4 before
# ApplicationElasticIp is associated.
#
# Behavior:
#   - Read current public IPv4 from IMDSv2 (bounded retries)
#   - If no public IP: do NOT destroy an existing cert; exit 0
#   - If cert already contains the public IP in SAN: no regeneration
#   - Else regenerate cert (localhost + 127.0.0.1 + public IP) and reload nginx
#
# Safe logs only (no private keys, no full PEM dumps).
set -euo pipefail

CERT="${TLS_CERT_PATH:-/etc/nginx/tls/application.crt}"
KEY="${TLS_KEY_PATH:-/etc/nginx/tls/application.key}"
MAX_ATTEMPTS="${TLS_REFRESH_MAX_ATTEMPTS:-10}"
SLEEP_SECS="${TLS_REFRESH_SLEEP_SECS:-30}"

log() { echo "tls-refresh: $*"; }

get_public_ip() {
  local token ip
  token=$(curl -sS -m 3 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
  if [ -z "${token}" ]; then
    return 1
  fi
  ip=$(curl -sS -m 3 -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/public-ipv4" 2>/dev/null || true)
  if [ -n "${ip}" ] && [[ "${ip}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "${ip}"
    return 0
  fi
  return 1
}

cert_contains_ip() {
  local ip="$1"
  if [ ! -f "${CERT}" ]; then
    return 1
  fi
  # Prefer openssl SAN text; fall back to subject CN.
  if openssl x509 -in "${CERT}" -noout -ext subjectAltName 2>/dev/null | grep -qE "IP Address:${ip}([^0-9]|$)"; then
    return 0
  fi
  if openssl x509 -in "${CERT}" -noout -subject 2>/dev/null | grep -qE "CN[[:space:]]*=[[:space:]]*${ip}([^0-9]|$)"; then
    return 0
  fi
  return 1
}

install_cert() {
  local ip="$1"
  local san cn
  san="subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${ip}"
  cn="${ip}"
  mkdir -p "$(dirname "${CERT}")"
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "${KEY}" \
    -out "${CERT}" \
    -subj "/CN=${cn}" -addext "${san}"
  chmod 640 "${KEY}"
  chmod 644 "${CERT}"
  chown root:nginx "${KEY}" "${CERT}" 2>/dev/null || chown root:root "${KEY}" "${CERT}"
}

attempt=1
while [ "${attempt}" -le "${MAX_ATTEMPTS}" ]; do
  public_ip=""
  if public_ip=$(get_public_ip); then
    log "public_ip_present=true attempt=${attempt}"
  else
    log "public_ip_present=false attempt=${attempt}"
    if [ -f "${CERT}" ] && [ -f "${KEY}" ]; then
      log "tls_refresh_needed=false reason=no_public_ip_keep_existing_cert"
      exit 0
    fi
    # No cert and no IP yet — wait for association.
    if [ "${attempt}" -lt "${MAX_ATTEMPTS}" ]; then
      sleep "${SLEEP_SECS}"
      attempt=$((attempt + 1))
      continue
    fi
    log "tls_refresh_needed=false reason=no_public_ip_and_no_cert_giving_up"
    exit 0
  fi

  if cert_contains_ip "${public_ip}"; then
    log "tls_refresh_needed=false public_ip_present=true reason=san_already_matches"
    exit 0
  fi

  log "tls_refresh_needed=true public_ip_present=true reason=san_mismatch_or_missing_cert"
  install_cert "${public_ip}"
  if command -v nginx >/dev/null 2>&1; then
    nginx -t
    systemctl reload nginx 2>/dev/null || systemctl try-reload-or-restart nginx 2>/dev/null || true
  fi
  log "tls_refresh_needed=false public_ip_present=true reason=regenerated_ok"
  exit 0
done

exit 0
