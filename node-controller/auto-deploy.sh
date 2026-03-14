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
VENV_DIR="${INSTALL_DIR}/.venv"
ENV_FILE="/etc/phantom-node-controller.env"
SERVICE_FILE="/etc/systemd/system/phantom-node-controller.service"
SERVICE_NAME="phantom-node-controller.service"
NODE_PYTHON_BIN="/usr/bin/python3"

PANEL_URL="${PHANTOM_PANEL_URL:-}"
SHARED_TOKEN="${PHANTOM_SHARED_TOKEN:-}"
NODE_TRANSPORT="${PHANTOM_NODE_TRANSPORT:-http}"
PANEL_GRPC_TARGET="${PHANTOM_PANEL_GRPC_TARGET:-}"
PANEL_GRPC_PORT="${PHANTOM_PANEL_GRPC_PORT:-50061}"
AGENT_ID="${PHANTOM_AGENT_ID:-}"
NODE_NAME="${FPTN_NODE_NAME:-}"
NODE_HOST="${FPTN_NODE_HOST:-}"
NODE_PORT="${FPTN_NODE_PORT:-}"
NODE_REGION="${FPTN_NODE_REGION:-}"
NODE_TIER="${FPTN_NODE_TIER:-}"
CERT_PATH="${FPTN_CERT_PATH:-/etc/fptn/server.crt}"
FPTN_CONFIG_DIR="${FPTN_CONFIG_DIR:-/etc/fptn}"
METRICS_URL="${LOCAL_FPTN_METRICS_URL:-}"
NET_INTERFACE="${PHANTOM_NET_INTERFACE:-}"
HEARTBEAT_INTERVAL="${PHANTOM_HEARTBEAT_INTERVAL:-30}"
FPTN_SYNC_INTERVAL="${PHANTOM_FPTN_SYNC_INTERVAL:-5}"
REQUEST_TIMEOUT="${PHANTOM_REQUEST_TIMEOUT:-5}"
REPLACE_EXISTING="false"
REPLACE_AGENT_ID="${PHANTOM_REPLACE_AGENT_ID:-}"
ALLOW_DEREGISTER_FAILURE="false"

usage() {
  cat <<'EOF'
Usage:
  sudo bash auto-deploy.sh --panel-url http://panel:8000 --shared-token TOKEN [options]

Required:
  --panel-url URL            Admin panel URL
  --shared-token TOKEN       Must match NODE_CONTROLLER_SHARED_TOKEN on the panel

Optional:
  --transport http|grpc      Default: http
  --grpc-target HOST:PORT    Explicit panel gRPC target
  --grpc-port PORT           Panel gRPC port, default: 50061
  --agent-id ID              Stable node agent id, default: hostname
  --node-name NAME           Node display name, default: hostname
  --node-host HOST           Public IP/hostname, default: auto-detected local IP
  --node-port PORT           FPTN public port, optional: panel default -> 8443 fallback
  --region REGION            Node region label, optional: panel default -> Unknown fallback
  --tier public|premium|censored, optional: panel default -> public fallback
  --cert-path PATH           Path to FPTN server certificate, default: /etc/fptn/server.crt
  --config-dir PATH          FPTN config dir for self-check, default: /etc/fptn
  --metrics-url URL          Local FPTN metrics endpoint
  --interface IFACE          Network interface, default: auto-detect from route
  --heartbeat-interval SEC   Default: 30
  --fptn-sync-interval SEC   Default: 5
  --request-timeout SEC      Default: 5
  --replace-existing         Remove old node record from the panel before re-registering
  --replace-agent-id ID      Agent id to remove first, default: current --agent-id / hostname
  --help                     Show this help

Example:
  sudo bash auto-deploy.sh \
    --panel-url https://panel.example.com \
    --shared-token super-secret \
    --transport grpc \
    --grpc-target panel.example.com:51173 \
    --node-name "Edge AMS-01" \
    --node-host 203.0.113.10 \
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

grpc_target_reachable() {
  local target="$1"
  if [[ -z "${target}" ]]; then
    return 1
  fi

  /usr/bin/python3 - "${target}" <<'PY'
import socket
import sys

target = sys.argv[1].strip()
if not target or ":" not in target:
    raise SystemExit(1)

host, port = target.rsplit(":", 1)
try:
    port_num = int(port)
except ValueError:
    raise SystemExit(1)

try:
    with socket.create_connection((host, port_num), timeout=2):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
}

deregister_existing_node() {
  local target_agent_id="$1"
  local attempt=1
  local max_attempts=10

  while (( attempt <= max_attempts )); do
    if "${NODE_PYTHON_BIN}" "${INSTALL_DIR}/agent.py" --deregister-agent-id "${target_agent_id}"; then
      echo "Removed previous node registration for agent_id=${target_agent_id}."
      return 0
    fi
    if (( attempt < max_attempts )); then
      echo "Panel not ready for deregister yet; retrying (${attempt}/${max_attempts})..." >&2
      sleep 2
    fi
    ((attempt++))
  done

  if [[ "${ALLOW_DEREGISTER_FAILURE}" == "true" ]]; then
    echo "Could not remove old node registration for agent_id=${target_agent_id}; continuing anyway." >&2
    return 0
  fi

  echo "Failed to remove old node registration for agent_id=${target_agent_id}." >&2
  exit 1
}

run_agent_once_with_retries() {
  local attempt=1
  local max_attempts=12

  while (( attempt <= max_attempts )); do
    if "${NODE_PYTHON_BIN}" "${INSTALL_DIR}/agent.py" --once >/tmp/phantom-node-once.json 2>/tmp/phantom-node-once.err; then
      echo "Validated node heartbeat against panel."
      return 0
    fi
    if (( attempt < max_attempts )); then
      echo "Heartbeat validation failed; retrying (${attempt}/${max_attempts})..." >&2
      sleep 2
    fi
    ((attempt++))
  done

  echo "Node heartbeat validation failed." >&2
  cat /tmp/phantom-node-once.err >&2 || true
  exit 1
}

run_self_check_or_fail() {
  local output_file="/tmp/phantom-node-self-check.json"
  if "${NODE_PYTHON_BIN}" "${INSTALL_DIR}/agent.py" --self-check >"${output_file}" 2>/tmp/phantom-node-self-check.err; then
    echo "Validated local FPTN stack and node-controller self-check."
    return 0
  fi

  echo "Node self-check failed." >&2
  cat /tmp/phantom-node-self-check.err >&2 || true
  cat "${output_file}" >&2 || true
  exit 1
}

ensure_grpc_runtime() {
  if [[ "${NODE_TRANSPORT}" != "grpc" ]]; then
    NODE_PYTHON_BIN="/usr/bin/python3"
    return
  fi

  if /usr/bin/python3 -c 'import grpc' >/dev/null 2>&1; then
    NODE_PYTHON_BIN="/usr/bin/python3"
    return
  fi

  if ! /usr/bin/python3 -m venv --help >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y python3-venv
    else
      echo "python3-venv is required to install grpcio for gRPC transport." >&2
      exit 1
    fi
  fi

  /usr/bin/python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
  "${VENV_DIR}/bin/pip" install "grpcio>=1.74,<1.76" >/dev/null
  NODE_PYTHON_BIN="${VENV_DIR}/bin/python"
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
    --transport)
      NODE_TRANSPORT="$2"
      shift 2
      ;;
    --grpc-target)
      PANEL_GRPC_TARGET="$2"
      shift 2
      ;;
    --grpc-port)
      PANEL_GRPC_PORT="$2"
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
    --config-dir)
      FPTN_CONFIG_DIR="$2"
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
    --fptn-sync-interval)
      FPTN_SYNC_INTERVAL="$2"
      shift 2
      ;;
    --request-timeout)
      REQUEST_TIMEOUT="$2"
      shift 2
      ;;
    --replace-existing)
      REPLACE_EXISTING="true"
      shift
      ;;
    --replace-agent-id)
      REPLACE_AGENT_ID="$2"
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

if [[ "${NODE_TRANSPORT}" != "http" && "${NODE_TRANSPORT}" != "grpc" ]]; then
  echo "--transport must be one of: http, grpc"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not installed."
  exit 1
fi

install -d "${INSTALL_DIR}"
install -m 755 "${SCRIPT_DIR}/agent.py" "${INSTALL_DIR}/agent.py"
install -m 644 "${SCRIPT_DIR}/phantom-node-controller.service" "${SERVICE_FILE}"
ensure_grpc_runtime

if [[ "${NODE_TRANSPORT}" == "grpc" && -z "${PANEL_GRPC_TARGET}" ]]; then
  PANEL_GRPC_TARGET="$(
    /usr/bin/python3 - "${PANEL_URL}" "${PANEL_GRPC_PORT}" <<'PY'
import sys
from urllib.parse import urlsplit

panel_url = sys.argv[1]
grpc_port = sys.argv[2]
host = urlsplit(panel_url).hostname or panel_url
print(f"{host}:{grpc_port}")
PY
  )"
fi

if [[ "${NODE_TRANSPORT}" == "grpc" ]] && ! grpc_target_reachable "${PANEL_GRPC_TARGET}"; then
  echo "gRPC target ${PANEL_GRPC_TARGET:-<empty>} is unreachable; falling back to HTTP transport." >&2
  NODE_TRANSPORT="http"
  PANEL_GRPC_TARGET=""
fi

{
  write_env_var "PHANTOM_PANEL_URL" "${PANEL_URL}"
  write_env_var "PHANTOM_SHARED_TOKEN" "${SHARED_TOKEN}"
  write_env_var "PHANTOM_NODE_TRANSPORT" "${NODE_TRANSPORT}"
  write_env_var "PHANTOM_PANEL_GRPC_TARGET" "${PANEL_GRPC_TARGET}"
  write_env_var "PHANTOM_PANEL_GRPC_PORT" "${PANEL_GRPC_PORT}"
  write_env_var "PHANTOM_AGENT_ID" "${AGENT_ID}"
  write_env_var "FPTN_NODE_NAME" "${NODE_NAME}"
  write_env_var "FPTN_NODE_HOST" "${NODE_HOST}"
  write_env_var "FPTN_NODE_PORT" "${NODE_PORT}"
  write_env_var "FPTN_NODE_REGION" "${NODE_REGION}"
  write_env_var "FPTN_NODE_TIER" "${NODE_TIER}"
  write_env_var "FPTN_CERT_PATH" "${CERT_PATH}"
  write_env_var "FPTN_CONFIG_DIR" "${FPTN_CONFIG_DIR}"
  write_env_var "LOCAL_FPTN_METRICS_URL" "${METRICS_URL}"
  write_env_var "PHANTOM_NET_INTERFACE" "${NET_INTERFACE}"
  write_env_var "PHANTOM_HEARTBEAT_INTERVAL" "${HEARTBEAT_INTERVAL}"
  write_env_var "PHANTOM_FPTN_SYNC_INTERVAL" "${FPTN_SYNC_INTERVAL}"
  write_env_var "PHANTOM_REQUEST_TIMEOUT" "${REQUEST_TIMEOUT}"
} > "${ENV_FILE}"

chmod 600 "${ENV_FILE}"

if [[ "${REPLACE_EXISTING}" == "true" ]]; then
  if [[ -z "${REPLACE_AGENT_ID}" ]]; then
    REPLACE_AGENT_ID="${AGENT_ID}"
  fi
  if [[ "${REPLACE_AGENT_ID}" == "${AGENT_ID}" ]]; then
    ALLOW_DEREGISTER_FAILURE="true"
  fi
  deregister_existing_node "${REPLACE_AGENT_ID}"
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2
run_agent_once_with_retries
run_self_check_or_fail

echo
echo "Phantom node-controller deployed."
echo "Service: ${SERVICE_NAME}"
echo "Panel:   ${PANEL_URL}"
echo "Transport: ${NODE_TRANSPORT}"
if [[ "${NODE_TRANSPORT}" == "grpc" ]]; then
  echo "gRPC:    ${PANEL_GRPC_TARGET}"
fi
echo "Node:    ${NODE_NAME} (${NODE_HOST}:${NODE_PORT:-panel-default})"
echo "Tier:    ${NODE_TIER:-panel-default}"
echo "Region:  ${NODE_REGION:-panel-default}"
echo "Env:     ${ENV_FILE}"
echo
systemctl --no-pager --full status "${SERVICE_NAME}" || true
echo
echo "Logs:"
echo "  journalctl -u ${SERVICE_NAME} -f"
