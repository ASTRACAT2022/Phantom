#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This auto-deploy script currently supports Linux only."
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is required but systemctl was not found."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/phantom-node-controller"
ENV_FILE="/etc/phantom-node-controller.env"
SERVICE_FILE="/etc/systemd/system/phantom-node-controller.service"
SERVICE_NAME="phantom-node-controller.service"

PANEL_URL="${PHANTOM_PANEL_URL:-}"
SHARED_TOKEN="${PHANTOM_SHARED_TOKEN:-}"
AGENT_ID="${PHANTOM_AGENT_ID:-}"
NODE_NAME="${FPTN_NODE_NAME:-}"
NODE_HOST="${FPTN_NODE_HOST:-}"
NODE_PORT="${FPTN_NODE_PORT:-}"
NODE_REGION="${FPTN_NODE_REGION:-}"
NODE_TIER="${FPTN_NODE_TIER:-}"
CERT_PATH="${FPTN_CERT_PATH:-/etc/fptn/server.crt}"
METRICS_URL="${LOCAL_FPTN_METRICS_URL:-}"
NET_INTERFACE="${PHANTOM_NET_INTERFACE:-}"
HEARTBEAT_INTERVAL="${PHANTOM_HEARTBEAT_INTERVAL:-30}"
REQUEST_TIMEOUT="${PHANTOM_REQUEST_TIMEOUT:-5}"

usage() {
  cat <<'EOF'
Usage:
  sudo bash auto-deploy.sh --panel-url http://panel:8000 --shared-token TOKEN [options]

Required:
  --panel-url URL            Admin panel URL
  --shared-token TOKEN       Must match NODE_CONTROLLER_SHARED_TOKEN on the panel

Optional:
  --agent-id ID              Stable node agent id, default: hostname
  --node-name NAME           Node display name, default: hostname
  --node-host HOST           Public IP/hostname, default: auto-detected local IP
  --node-port PORT           FPTN public port, optional: panel default -> 8443 fallback
  --region REGION            Node region label, optional: panel default -> Unknown fallback
  --tier public|premium|censored, optional: panel default -> public fallback
  --cert-path PATH           Path to FPTN server certificate, default: /etc/fptn/server.crt
  --metrics-url URL          Local FPTN metrics endpoint
  --interface IFACE          Network interface, default: auto-detect from route
  --heartbeat-interval SEC   Default: 30
  --request-timeout SEC      Default: 5
  --help                     Show this help

Example:
  sudo bash auto-deploy.sh \
    --panel-url https://panel.example.com \
    --shared-token super-secret \
    --node-name "Edge AMS-01" \
    --node-host 1.2.3.4 \
    --region Amsterdam \
    --tier public
EOF
}

write_env_var() {
  local key="$1"
  local value="${2:-}"
  printf '%s=%q\n' "${key}" "${value}"
}

detect_default_interface() {
  ip route show default 2>/dev/null | awk '/default/ {print $5; exit}'
}

detect_host_ip() {
  local iface="$1"
  if [[ -n "${iface}" ]] && command -v ip >/dev/null 2>&1; then
    ip -4 addr show dev "${iface}" 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -n1
    return 0
  fi
  hostname -I 2>/dev/null | awk '{print $1}'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --panel-url)
      PANEL_URL="$2"
      shift 2
      ;;
    --shared-token)
      SHARED_TOKEN="$2"
      shift 2
      ;;
    --agent-id)
      AGENT_ID="$2"
      shift 2
      ;;
    --node-name)
      NODE_NAME="$2"
      shift 2
      ;;
    --node-host)
      NODE_HOST="$2"
      shift 2
      ;;
    --node-port)
      NODE_PORT="$2"
      shift 2
      ;;
    --region)
      NODE_REGION="$2"
      shift 2
      ;;
    --tier)
      NODE_TIER="$2"
      shift 2
      ;;
    --cert-path)
      CERT_PATH="$2"
      shift 2
      ;;
    --metrics-url)
      METRICS_URL="$2"
      shift 2
      ;;
    --interface)
      NET_INTERFACE="$2"
      shift 2
      ;;
    --heartbeat-interval)
      HEARTBEAT_INTERVAL="$2"
      shift 2
      ;;
    --request-timeout)
      REQUEST_TIMEOUT="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      echo
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${PANEL_URL}" ]]; then
  echo "--panel-url is required."
  exit 1
fi

if [[ -z "${SHARED_TOKEN}" ]]; then
  echo "--shared-token is required."
  exit 1
fi

if [[ -z "${NET_INTERFACE}" ]]; then
  NET_INTERFACE="$(detect_default_interface || true)"
fi

HOSTNAME_FALLBACK="$(hostname)"
if [[ -z "${AGENT_ID}" ]]; then
  AGENT_ID="${HOSTNAME_FALLBACK}"
fi

if [[ -z "${NODE_NAME}" ]]; then
  NODE_NAME="${HOSTNAME_FALLBACK}"
fi

if [[ -z "${NODE_HOST}" ]]; then
  NODE_HOST="$(detect_host_ip "${NET_INTERFACE:-}" || true)"
fi

if [[ -z "${NODE_HOST}" ]]; then
  echo "Could not auto-detect node host IP. Pass --node-host explicitly."
  exit 1
fi

if [[ -n "${NODE_TIER}" && "${NODE_TIER}" != "public" && "${NODE_TIER}" != "premium" && "${NODE_TIER}" != "censored" ]]; then
  echo "--tier must be one of: public, premium, censored"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not installed."
  exit 1
fi

install -d "${INSTALL_DIR}"
install -m 755 "${SCRIPT_DIR}/agent.py" "${INSTALL_DIR}/agent.py"
install -m 644 "${SCRIPT_DIR}/phantom-node-controller.service" "${SERVICE_FILE}"

{
  write_env_var "PHANTOM_PANEL_URL" "${PANEL_URL}"
  write_env_var "PHANTOM_SHARED_TOKEN" "${SHARED_TOKEN}"
  write_env_var "PHANTOM_AGENT_ID" "${AGENT_ID}"
  write_env_var "FPTN_NODE_NAME" "${NODE_NAME}"
  write_env_var "FPTN_NODE_HOST" "${NODE_HOST}"
  write_env_var "FPTN_NODE_PORT" "${NODE_PORT}"
  write_env_var "FPTN_NODE_REGION" "${NODE_REGION}"
  write_env_var "FPTN_NODE_TIER" "${NODE_TIER}"
  write_env_var "FPTN_CERT_PATH" "${CERT_PATH}"
  write_env_var "LOCAL_FPTN_METRICS_URL" "${METRICS_URL}"
  write_env_var "PHANTOM_NET_INTERFACE" "${NET_INTERFACE}"
  write_env_var "PHANTOM_HEARTBEAT_INTERVAL" "${HEARTBEAT_INTERVAL}"
  write_env_var "PHANTOM_REQUEST_TIMEOUT" "${REQUEST_TIMEOUT}"
} > "${ENV_FILE}"

chmod 600 "${ENV_FILE}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2

echo
echo "Phantom node-controller deployed."
echo "Service: ${SERVICE_NAME}"
echo "Panel:   ${PANEL_URL}"
echo "Node:    ${NODE_NAME} (${NODE_HOST}:${NODE_PORT:-panel-default})"
echo "Tier:    ${NODE_TIER:-panel-default}"
echo "Region:  ${NODE_REGION:-panel-default}"
echo "Env:     ${ENV_FILE}"
echo
systemctl --no-pager --full status "${SERVICE_NAME}" || true
echo
echo "Logs:"
echo "  journalctl -u ${SERVICE_NAME} -f"
