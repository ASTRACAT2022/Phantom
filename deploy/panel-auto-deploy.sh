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

APP_NAME="Phantom Control Plane"
PANEL_HOST="0.0.0.0"
PANEL_PORT="8000"
DATABASE_PATH="${STATE_DIR}/panel.db"
FPTN_CONFIG_DIR="${STATE_DIR}/fptn-config"
FPTN_SERVICE_NAME="PHANTOM.NET"
FPTN_PROMETHEUS_METRICS_URL=""
NODE_CONTROLLER_SHARED_TOKEN=""
BILLING_API_TOKEN=""
PHANTOM_SEED_DEMO="false"
PANEL_TIMEZONE="Europe/Moscow"

usage() {
  cat <<'EOF'
Usage:
  sudo bash deploy/panel-auto-deploy.sh [options]

Options:
  --panel-host HOST
  --panel-port PORT
  --app-name NAME
  --database-path PATH
  --fptn-config-dir PATH
  --fptn-service-name NAME
  --metrics-url URL
  --node-token TOKEN
  --billing-token TOKEN
  --seed-demo true|false
  --timezone TZ
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
      --seed-demo)
        PHANTOM_SEED_DEMO="$2"
        shift 2
        ;;
      --timezone)
        PANEL_TIMEZONE="$2"
        shift 2
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

  if [[ ! -f "${ENV_FILE}" ]]; then
    install -m 0640 -o root -g "${SERVICE_USER}" /dev/null "${ENV_FILE}"
  fi

  set_env_var "APP_NAME" "${APP_NAME}"
  set_env_var "DATABASE_PATH" "${DATABASE_PATH}"
  set_env_var "FPTN_CONFIG_DIR" "${FPTN_CONFIG_DIR}"
  set_env_var "FPTN_SERVICE_NAME" "${FPTN_SERVICE_NAME}"
  set_env_var "FPTN_PROMETHEUS_METRICS_URL" "${FPTN_PROMETHEUS_METRICS_URL}"
  set_env_var "NODE_CONTROLLER_SHARED_TOKEN" "${NODE_CONTROLLER_SHARED_TOKEN}"
  set_env_var "BILLING_API_TOKEN" "${BILLING_API_TOKEN}"
  set_env_var "PHANTOM_SEED_DEMO" "${PHANTOM_SEED_DEMO}"
  set_env_var "PANEL_TIMEZONE" "${PANEL_TIMEZONE}"
  set_env_var "PANEL_HOST" "${PANEL_HOST}"
  set_env_var "PANEL_PORT" "${PANEL_PORT}"
}

install_systemd_unit() {
  install -m 0644 "${PROJECT_ROOT}/deploy/phantom-control-plane.service" "${SYSTEMD_UNIT}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  systemctl restart "${SERVICE_NAME}.service"
}

print_summary() {
  cat <<EOF

Phantom Control Plane deployed.

Service:
  systemctl status ${SERVICE_NAME}.service
  journalctl -u ${SERVICE_NAME}.service -f

Panel:
  http://${PANEL_HOST}:${PANEL_PORT}
  http://${PANEL_HOST}:${PANEL_PORT}/docs

Env file:
  ${ENV_FILE}

Tokens:
  NODE_CONTROLLER_SHARED_TOKEN=${NODE_CONTROLLER_SHARED_TOKEN}
  BILLING_API_TOKEN=${BILLING_API_TOKEN}

EOF
}

main() {
  parse_args "$@"
  ensure_root
  ensure_command python3
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
