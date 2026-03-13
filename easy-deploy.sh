#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="${ROOT_DIR}/deploy/panel-auto-deploy.sh"

PANEL_PORT="8000"
PANEL_HOST="0.0.0.0"
PUBLIC_IP=""
NODE_TOKEN=""
BILLING_TOKEN=""
METRICS_URL=""
TIMEZONE="Europe/Moscow"
SEED_DEMO="false"
DATABASE_URL=""
ADMIN_USERNAME="admin"
ADMIN_PASSWORD=""
SESSION_COOKIE_SECURE="false"
ENABLE_NODE_GRPC="false"
NODE_GRPC_HOST="0.0.0.0"
NODE_GRPC_PORT=""

usage() {
  cat <<'EOF'
Easy deploy for Phantom Control Plane

Usage:
  sudo bash easy-deploy.sh [options]

Options:
  --port PORT
  --host HOST
  --public-ip IP
  --node-token TOKEN
  --billing-token TOKEN
  --admin-username USER
  --admin-password PASS
  --session-cookie-secure true|false
  --enable-node-grpc
  --grpc-host HOST
  --grpc-port PORT|random
  --metrics-url URL
  --database-url URL
  --timezone TZ
  --seed-demo true|false
  --help

Examples:
  sudo bash easy-deploy.sh
  sudo bash easy-deploy.sh --port 8080
  sudo bash easy-deploy.sh --port 8080 --node-token my-node-token --billing-token my-billing-token
EOF
}

detect_public_ip() {
  local detected

  detected="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "${detected}" ]]; then
    printf '%s\n' "${detected}"
    return
  fi

  detected="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
  if [[ -n "${detected}" ]]; then
    printf '%s\n' "${detected}"
    return
  fi

  printf 'SERVER_IP\n'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port)
        PANEL_PORT="$2"
        shift 2
        ;;
      --host)
        PANEL_HOST="$2"
        shift 2
        ;;
      --public-ip)
        PUBLIC_IP="$2"
        shift 2
        ;;
      --node-token)
        NODE_TOKEN="$2"
        shift 2
        ;;
      --billing-token)
        BILLING_TOKEN="$2"
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
      --enable-node-grpc)
        ENABLE_NODE_GRPC="true"
        shift
        ;;
      --grpc-host)
        NODE_GRPC_HOST="$2"
        shift 2
        ;;
      --grpc-port)
        NODE_GRPC_PORT="$2"
        shift 2
        ;;
      --metrics-url)
        METRICS_URL="$2"
        shift 2
        ;;
      --database-url)
        DATABASE_URL="$2"
        shift 2
        ;;
      --timezone)
        TIMEZONE="$2"
        shift 2
        ;;
      --seed-demo)
        SEED_DEMO="$2"
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

ensure_ready() {
  if [[ ! -f "${DEPLOY_SCRIPT}" ]]; then
    echo "Deploy script not found: ${DEPLOY_SCRIPT}" >&2
    exit 1
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash easy-deploy.sh" >&2
    exit 1
  fi
}

main() {
  parse_args "$@"
  ensure_ready

  if [[ -z "${PUBLIC_IP}" ]]; then
    PUBLIC_IP="$(detect_public_ip)"
  fi

  echo "Starting Phantom easy deploy..."
  echo "Panel host: ${PANEL_HOST}"
  echo "Panel port: ${PANEL_PORT}"
  echo "Public IP hint: ${PUBLIC_IP}"

  CMD=(
    bash "${DEPLOY_SCRIPT}"
    --panel-host "${PANEL_HOST}"
    --panel-port "${PANEL_PORT}"
    --timezone "${TIMEZONE}"
    --seed-demo "${SEED_DEMO}"
  )

  if [[ -n "${NODE_TOKEN}" ]]; then
    CMD+=(--node-token "${NODE_TOKEN}")
  fi
  if [[ -n "${BILLING_TOKEN}" ]]; then
    CMD+=(--billing-token "${BILLING_TOKEN}")
  fi
  if [[ -n "${ADMIN_USERNAME}" ]]; then
    CMD+=(--admin-username "${ADMIN_USERNAME}")
  fi
  if [[ -n "${ADMIN_PASSWORD}" ]]; then
    CMD+=(--admin-password "${ADMIN_PASSWORD}")
  fi
  if [[ -n "${SESSION_COOKIE_SECURE}" ]]; then
    CMD+=(--session-cookie-secure "${SESSION_COOKIE_SECURE}")
  fi
  if [[ -n "${METRICS_URL}" ]]; then
    CMD+=(--metrics-url "${METRICS_URL}")
  fi
  if [[ -n "${DATABASE_URL}" ]]; then
    CMD+=(--database-url "${DATABASE_URL}")
  fi
  if [[ "${ENABLE_NODE_GRPC}" == "true" ]]; then
    CMD+=(--enable-node-grpc --grpc-host "${NODE_GRPC_HOST}")
    if [[ -n "${NODE_GRPC_PORT}" ]]; then
      CMD+=(--grpc-port "${NODE_GRPC_PORT}")
    fi
  fi

  "${CMD[@]}"

  cat <<EOF

Quick links:
  Panel: http://${PUBLIC_IP}:${PANEL_PORT}
  Swagger: http://${PUBLIC_IP}:${PANEL_PORT}/docs

Next:
  systemctl status phantom-control-plane.service
  journalctl -u phantom-control-plane.service -f

EOF
}

main "$@"
