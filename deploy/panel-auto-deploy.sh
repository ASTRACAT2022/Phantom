#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_DIR="/opt/phantom-control-plane"
SERVICE_NAME="phantom-control-plane"
SERVICE_USER="phantom"
STATE_DIR="/var/lib/phantom-control-plane"
ENV_FILE="/etc/phantom-control-plane.env"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
BACKUP_SERVICE_FILE="/etc/systemd/system/phantom-backup.service"
BACKUP_TIMER_FILE="/etc/systemd/system/phantom-backup.timer"

APP_NAME="Phantom Control Plane"
DATABASE_URL=""
PANEL_HOST="0.0.0.0"
PANEL_PORT="8000"
DATABASE_PATH="${STATE_DIR}/panel.db"
FPTN_CONFIG_DIR="${STATE_DIR}/fptn-config"
FPTN_SERVICE_NAME="ASTRACAT.Network"
FPTN_PROMETHEUS_METRICS_URL=""
NODE_CONTROLLER_SHARED_TOKEN=""
BILLING_API_TOKEN=""
ADMIN_USERNAME="admin"
ADMIN_PASSWORD=""
ADMIN_SESSION_SECRET=""
SESSION_COOKIE_SECURE="false"
PANEL_PUBLIC_BASE_URL=""
FORWARDED_ALLOW_IPS="127.0.0.1"
NODE_AGENT_GRPC_ENABLED="false"
NODE_AGENT_GRPC_HOST="0.0.0.0"
NODE_AGENT_GRPC_PORT="50061"
PHANTOM_SEED_DEMO="false"
PANEL_TIMEZONE="Europe/Moscow"
PHANTOM_BACKUP_DIR="/var/backups/phantom-control-plane"
PHANTOM_BACKUP_RETENTION_DAYS="14"
ENABLE_BACKUP_TIMER="true"

usage() {
  cat <<'EOF'
Usage:
  sudo bash deploy/panel-auto-deploy.sh [options]

Options:
  --panel-host HOST
  --panel-port PORT
  --app-name NAME
  --database-url URL
  --database-path PATH
  --fptn-config-dir PATH
  --fptn-service-name NAME
  --metrics-url URL
  --node-token TOKEN
  --billing-token TOKEN
  --admin-username USER
  --admin-password PASS
  --session-cookie-secure true|false
  --public-base-url URL
  --forwarded-allow-ips IPS
  --behind-proxy
  --enable-node-grpc
  --grpc-host HOST
  --grpc-port PORT|random
  --seed-demo true|false
  --timezone TZ
  --backup-dir PATH
  --backup-retention-days DAYS
  --disable-backup-timer
  --help
EOF
}

quote_env() {
  printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"]/\\&/g')"
}

random_token() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

random_port() {
  python3 - <<'PY'
import random
print(random.randint(20000, 60000))
PY
}

is_local_metrics_url() {
  local value="$1"
  [[ "${value}" == http://127.0.0.1:* || "${value}" == http://localhost:* || "${value}" == https://127.0.0.1:* || "${value}" == https://localhost:* ]]
}

read_env_var() {
  local key="$1"
  local env_file="${2:-${ENV_FILE}}"
  if [[ ! -f "${env_file}" ]]; then
    return 0
  fi
  sed -n "s/^${key}=\"\\(.*\\)\"$/\\1/p" "${env_file}" | head -n 1
}

detect_local_fptn_metrics_candidates() {
  local compose_file="/opt/fptn-server/docker-compose.yml"
  local proxy_port=""
  local port=""
  local secret=""
  local server_ips=""
  if [[ ! -f "${compose_file}" ]]; then
    return 0
  fi

  proxy_port="$(sed -n 's/^[[:space:]]*-[[:space:]]*"127\.0\.0\.1:\([0-9]\+\):80\/tcp"[[:space:]]*$/\1/p' "${compose_file}" | head -n 1)"
  port="$(sed -n 's/^[[:space:]]*-[[:space:]]*"\([0-9]\+\):443\/tcp"[[:space:]]*$/\1/p' "${compose_file}" | head -n 1)"
  secret="$(sed -n 's/^[[:space:]]*PROMETHEUS_SECRET_ACCESS_KEY:[[:space:]]*"\([^"]\+\)"[[:space:]]*$/\1/p' "${compose_file}" | head -n 1)"
  server_ips="$(sed -n 's/^[[:space:]]*SERVER_EXTERNAL_IPS:[[:space:]]*"\([^"]\+\)"[[:space:]]*$/\1/p' "${compose_file}" | head -n 1)"
  if [[ -z "${secret}" ]]; then
    return 0
  fi
  if [[ -n "${proxy_port}" ]]; then
    printf 'http://127.0.0.1:%s/api/v1/metrics/%s\n' "${proxy_port}" "${secret}"
    printf 'http://localhost:%s/api/v1/metrics/%s\n' "${proxy_port}" "${secret}"
  fi
  if [[ -n "${port}" ]]; then
    printf 'https://127.0.0.1:%s/api/v1/metrics/%s\n' "${port}" "${secret}"
    printf 'https://localhost:%s/api/v1/metrics/%s\n' "${port}" "${secret}"
    if [[ -n "${server_ips}" ]]; then
      tr ',' '\n' <<< "${server_ips}" | sed 's/^ *//;s/ *$//' | while read -r host; do
        if [[ -n "${host}" ]]; then
          printf 'https://%s:%s/api/v1/metrics/%s\n' "${host}" "${port}" "${secret}"
        fi
      done
    fi
  fi
}

metrics_url_returns_fptn_data() {
  local url="$1"
  local curl_args=(-fsS --max-time 3)
  if [[ "${url}" == https://* ]]; then
    curl_args+=(-k)
  fi
  local body=""
  if ! body="$(curl "${curl_args[@]}" "${url}" 2>/dev/null || true)"; then
    return 1
  fi
  if [[ -z "${body}" ]]; then
    return 1
  fi
  grep -qE 'fptn_active_sessions|fptn_user_(incoming|outgoing)_traffic_bytes' <<< "${body}"
}

choose_metrics_url() {
  local explicit_url="$1"
  local existing_url="$2"
  local candidate=""
  if [[ -n "${explicit_url}" ]]; then
    printf '%s\n' "${explicit_url}"
    return 0
  fi
  while read -r candidate; do
    if [[ -n "${candidate}" ]] && metrics_url_returns_fptn_data "${candidate}"; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done < <(detect_local_fptn_metrics_candidates)
  if [[ -n "${existing_url}" ]]; then
    printf '%s\n' "${existing_url}"
    return 0
  fi
  return 0
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root: sudo bash deploy/panel-auto-deploy.sh" >&2
    exit 1
  fi
}

ensure_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --panel-host)
        PANEL_HOST="$2"
        shift 2
        ;;
      --panel-port)
        PANEL_PORT="$2"
        shift 2
        ;;
      --app-name)
        APP_NAME="$2"
        shift 2
        ;;
      --database-url)
        DATABASE_URL="$2"
        shift 2
        ;;
      --database-path)
        DATABASE_PATH="$2"
        shift 2
        ;;
      --fptn-config-dir)
        FPTN_CONFIG_DIR="$2"
        shift 2
        ;;
      --fptn-service-name)
        FPTN_SERVICE_NAME="$2"
        shift 2
        ;;
      --metrics-url)
        FPTN_PROMETHEUS_METRICS_URL="$2"
        shift 2
        ;;
      --node-token)
        NODE_CONTROLLER_SHARED_TOKEN="$2"
        shift 2
        ;;
      --billing-token)
        BILLING_API_TOKEN="$2"
        shift 2
        ;;
      --admin-username)
        ADMIN_USERNAME="$2"
        shift 2
        ;;
      --admin-password)
        ADMIN_PASSWORD="$2"
        shift 2
        ;;
      --session-cookie-secure)
        SESSION_COOKIE_SECURE="$2"
        shift 2
        ;;
      --public-base-url)
        PANEL_PUBLIC_BASE_URL="$2"
        shift 2
        ;;
      --forwarded-allow-ips)
        FORWARDED_ALLOW_IPS="$2"
        shift 2
        ;;
      --behind-proxy)
        if [[ "${PANEL_HOST}" == "0.0.0.0" ]]; then
          PANEL_HOST="127.0.0.1"
        fi
        SESSION_COOKIE_SECURE="true"
        shift
        ;;
      --enable-node-grpc)
        NODE_AGENT_GRPC_ENABLED="true"
        shift
        ;;
      --grpc-host)
        NODE_AGENT_GRPC_HOST="$2"
        shift 2
        ;;
      --grpc-port)
        NODE_AGENT_GRPC_PORT="$2"
        shift 2
        ;;
      --seed-demo)
        PHANTOM_SEED_DEMO="$2"
        shift 2
        ;;
      --timezone)
        PANEL_TIMEZONE="$2"
        shift 2
        ;;
      --backup-dir)
        PHANTOM_BACKUP_DIR="$2"
        shift 2
        ;;
      --backup-retention-days)
        PHANTOM_BACKUP_RETENTION_DAYS="$2"
        shift 2
        ;;
      --disable-backup-timer)
        ENABLE_BACKUP_TIMER="false"
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done
}

create_service_user() {
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
}

install_project_files() {
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${STATE_DIR}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${FPTN_CONFIG_DIR}"
  install -d "${PHANTOM_BACKUP_DIR}"

  rm -rf \
    "${APP_DIR}/app" \
    "${APP_DIR}/templates" \
    "${APP_DIR}/static" \
    "${APP_DIR}/node-controller" \
    "${APP_DIR}/deploy"

  cp -R "${PROJECT_ROOT}/app" "${APP_DIR}/app"
  cp -R "${PROJECT_ROOT}/templates" "${APP_DIR}/templates"
  cp -R "${PROJECT_ROOT}/static" "${APP_DIR}/static"
  cp -R "${PROJECT_ROOT}/node-controller" "${APP_DIR}/node-controller"
  cp -R "${PROJECT_ROOT}/deploy" "${APP_DIR}/deploy"
  install -m 0644 "${PROJECT_ROOT}/requirements.txt" "${APP_DIR}/requirements.txt"
  install -m 0644 "${PROJECT_ROOT}/README.md" "${APP_DIR}/README.md"
  install -m 0644 "${PROJECT_ROOT}/MANUAL.md" "${APP_DIR}/MANUAL.md"

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}" "${STATE_DIR}"
}

install_python_env() {
  python3 -m venv "${APP_DIR}/.venv"
  "${APP_DIR}/.venv/bin/pip" install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.venv"
}

set_env_var() {
  local key="$1"
  local value="$2"

  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i.bak "s|^${key}=.*|${key}=$(quote_env "${value}")|" "${ENV_FILE}"
    rm -f "${ENV_FILE}.bak"
  else
    printf '%s=%s\n' "${key}" "$(quote_env "${value}")" >> "${ENV_FILE}"
  fi
}

write_env_file() {
  if [[ -z "${NODE_CONTROLLER_SHARED_TOKEN}" ]]; then
    NODE_CONTROLLER_SHARED_TOKEN="$(random_token)"
  fi
  if [[ -z "${BILLING_API_TOKEN}" ]]; then
    BILLING_API_TOKEN="$(random_token)"
  fi
  if [[ -z "${ADMIN_PASSWORD}" ]]; then
    ADMIN_PASSWORD="$(random_token)"
  fi
  if [[ -z "${ADMIN_SESSION_SECRET}" ]]; then
    ADMIN_SESSION_SECRET="$(random_token)"
  fi
  if [[ "${NODE_AGENT_GRPC_ENABLED}" == "true" && "${NODE_AGENT_GRPC_PORT}" == "random" ]]; then
    NODE_AGENT_GRPC_PORT="$(random_port)"
  fi

  if [[ ! -f "${ENV_FILE}" ]]; then
    install -m 0640 -o root -g "${SERVICE_USER}" /dev/null "${ENV_FILE}"
  fi
  local existing_metrics_url=""
  existing_metrics_url="$(read_env_var "FPTN_PROMETHEUS_METRICS_URL" "${ENV_FILE}")"
  FPTN_PROMETHEUS_METRICS_URL="$(choose_metrics_url "${FPTN_PROMETHEUS_METRICS_URL}" "${existing_metrics_url}")"

  set_env_var "APP_NAME" "${APP_NAME}"
  set_env_var "DATABASE_URL" "${DATABASE_URL}"
  set_env_var "DATABASE_PATH" "${DATABASE_PATH}"
  set_env_var "FPTN_CONFIG_DIR" "${FPTN_CONFIG_DIR}"
  set_env_var "FPTN_SERVICE_NAME" "${FPTN_SERVICE_NAME}"
  set_env_var "FPTN_PROMETHEUS_METRICS_URL" "${FPTN_PROMETHEUS_METRICS_URL}"
  if is_local_metrics_url "${FPTN_PROMETHEUS_METRICS_URL}" && [[ "${FPTN_PROMETHEUS_METRICS_URL}" == https://* ]]; then
    set_env_var "FPTN_PROMETHEUS_INSECURE_TLS" "true"
  elif [[ "${FPTN_PROMETHEUS_METRICS_URL}" == http://* ]]; then
    set_env_var "FPTN_PROMETHEUS_INSECURE_TLS" "false"
  fi
  set_env_var "NODE_CONTROLLER_SHARED_TOKEN" "${NODE_CONTROLLER_SHARED_TOKEN}"
  set_env_var "BILLING_API_TOKEN" "${BILLING_API_TOKEN}"
  set_env_var "ADMIN_USERNAME" "${ADMIN_USERNAME}"
  set_env_var "ADMIN_PASSWORD" "${ADMIN_PASSWORD}"
  set_env_var "ADMIN_SESSION_SECRET" "${ADMIN_SESSION_SECRET}"
  set_env_var "SESSION_COOKIE_SECURE" "${SESSION_COOKIE_SECURE}"
  set_env_var "PANEL_PUBLIC_BASE_URL" "${PANEL_PUBLIC_BASE_URL}"
  set_env_var "FORWARDED_ALLOW_IPS" "${FORWARDED_ALLOW_IPS}"
  set_env_var "NODE_AGENT_GRPC_ENABLED" "${NODE_AGENT_GRPC_ENABLED}"
  set_env_var "NODE_AGENT_GRPC_HOST" "${NODE_AGENT_GRPC_HOST}"
  set_env_var "NODE_AGENT_GRPC_PORT" "${NODE_AGENT_GRPC_PORT}"
  set_env_var "PHANTOM_SEED_DEMO" "${PHANTOM_SEED_DEMO}"
  set_env_var "PANEL_TIMEZONE" "${PANEL_TIMEZONE}"
  set_env_var "PANEL_HOST" "${PANEL_HOST}"
  set_env_var "PANEL_PORT" "${PANEL_PORT}"
  set_env_var "PHANTOM_BACKUP_DIR" "${PHANTOM_BACKUP_DIR}"
  set_env_var "PHANTOM_BACKUP_RETENTION_DAYS" "${PHANTOM_BACKUP_RETENTION_DAYS}"
}

install_systemd_unit() {
  install -m 0644 "${PROJECT_ROOT}/deploy/phantom-control-plane.service" "${SYSTEMD_UNIT}"
  install -m 0644 "${PROJECT_ROOT}/deploy/phantom-backup.service" "${BACKUP_SERVICE_FILE}"
  install -m 0644 "${PROJECT_ROOT}/deploy/phantom-backup.timer" "${BACKUP_TIMER_FILE}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  systemctl restart "${SERVICE_NAME}.service"
  if [[ "${ENABLE_BACKUP_TIMER}" == "true" ]]; then
    systemctl enable phantom-backup.timer
    systemctl restart phantom-backup.timer
  fi
}

print_summary() {
  cat <<EOF

Phantom Control Plane deployed.

Service:
  systemctl status ${SERVICE_NAME}.service
  journalctl -u ${SERVICE_NAME}.service -f
  systemctl status phantom-backup.timer

Panel:
  http://${PANEL_HOST}:${PANEL_PORT}
  http://${PANEL_HOST}:${PANEL_PORT}/docs

Public base URL:
  ${PANEL_PUBLIC_BASE_URL:-not-set}

Env file:
  ${ENV_FILE}

Tokens:
  NODE_CONTROLLER_SHARED_TOKEN=${NODE_CONTROLLER_SHARED_TOKEN}
  BILLING_API_TOKEN=${BILLING_API_TOKEN}

Admin:
  username=${ADMIN_USERNAME}
  password=${ADMIN_PASSWORD}

Backups:
  dir=${PHANTOM_BACKUP_DIR}
  retention_days=${PHANTOM_BACKUP_RETENTION_DAYS}
  run_now=sudo bash /opt/phantom-control-plane/deploy/backup.sh
  restore=sudo bash /opt/phantom-control-plane/deploy/restore.sh --archive ${PHANTOM_BACKUP_DIR}/phantom-backup-YYYYMMDD-HHMMSS.tar.gz

Metrics:
  url=${FPTN_PROMETHEUS_METRICS_URL}

Proxy:
  forwarded_allow_ips=${FORWARDED_ALLOW_IPS}

Node gRPC:
  enabled=${NODE_AGENT_GRPC_ENABLED}
  target=${PANEL_HOST}:${NODE_AGENT_GRPC_PORT}

EOF
}

main() {
  parse_args "$@"
  ensure_root
  ensure_command python3
  ensure_command curl
  ensure_command systemctl
  ensure_command install
  create_service_user
  install_project_files
  install_python_env
  write_env_file
  install_systemd_unit
  systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  print_summary
}

main "$@"
