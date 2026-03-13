#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${PHANTOM_GITHUB_REPO:-ASTRACAT2022/Phantom}"
REPO_REF="${PHANTOM_GITHUB_REF:-main}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_REF}/node-controller"

PANEL_URL="${PHANTOM_PANEL_URL:-}"
SHARED_TOKEN="${PHANTOM_SHARED_TOKEN:-}"
NODE_TRANSPORT="${PHANTOM_NODE_TRANSPORT:-http}"
PANEL_GRPC_TARGET="${PHANTOM_PANEL_GRPC_TARGET:-}"
PANEL_GRPC_PORT="${PHANTOM_PANEL_GRPC_PORT:-50061}"
AGENT_ID="${PHANTOM_AGENT_ID:-}"
NODE_NAME="${FPTN_NODE_NAME:-}"
NODE_HOST="${FPTN_NODE_HOST:-}"
NODE_PORT="${FPTN_NODE_PORT:-8443}"
NODE_REGION="${FPTN_NODE_REGION:-}"
NODE_TIER="${FPTN_NODE_TIER:-}"
PROXY_DOMAIN="${FPTN_DEFAULT_PROXY_DOMAIN:-vk.ru}"
FPTN_IMAGE="${FPTN_SERVER_IMAGE:-fptnvpn/fptn-vpn-server:latest}"
FPTN_DIR="${FPTN_SERVER_DIR:-/opt/fptn-server}"
FPTN_CONFIG_DIR="${FPTN_CONFIG_DIR:-/opt/fptn-server-data}"
LOCAL_METRICS_URL="${LOCAL_FPTN_METRICS_URL:-}"
OPEN_UFW="false"
REPLACE_EXISTING="false"
REPLACE_AGENT_ID="${PHANTOM_REPLACE_AGENT_ID:-}"
FPTN_ONLY="false"
SKIP_DOCKER_INSTALL="false"
TMP_DIR=""

usage() {
  cat <<EOF
Phantom full FPTN node installer

This script prepares Docker, deploys FPTN on the selected port, generates a certificate,
and then installs Phantom node-controller so the node appears in the admin panel.

Usage:
  curl -fsSL ${RAW_BASE}/install-fptn-node.sh | sudo bash -s -- --panel-url http://PANEL_IP:8000 --shared-token TOKEN --node-host NODE_IP [options]

Required:
  --node-host HOST           Public IP / hostname of this node

Required unless --fptn-only:
  --panel-url URL            Admin panel URL
  --shared-token TOKEN       Must match NODE_CONTROLLER_SHARED_TOKEN on the panel

Optional:
  --transport http|grpc      node-controller transport, default: http
  --grpc-target HOST:PORT    Explicit panel gRPC target
  --grpc-port PORT           Panel gRPC port, default: 50061
  --agent-id ID              Stable node agent id
  --node-name NAME           Node display name, default: hostname
  --node-port PORT           Public FPTN port, default: 8443
  --region REGION            Node region label
  --tier public|premium|censored
  --proxy-domain DOMAIN      FPTN DEFAULT_PROXY_DOMAIN, default: vk.ru
  --metrics-url URL          Forwarded to node-controller as LOCAL_FPTN_METRICS_URL
  --fptn-image IMAGE         Docker image, default: fptnvpn/fptn-vpn-server:latest
  --fptn-dir PATH            Docker compose dir, default: /opt/fptn-server
  --fptn-config-dir PATH     FPTN config dir, default: /opt/fptn-server-data
  --open-ufw                 Open node port in UFW when active
  --replace-existing         Remove old node registration before re-registering
  --replace-agent-id ID      Explicit old agent id to remove
  --fptn-only                Deploy only FPTN, skip node-controller install
  --skip-docker-install      Do not apt install Docker / openssl
  --help                     Show this help
EOF
}

cleanup() {
  if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
    rm -rf "${TMP_DIR}"
  fi
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: curl ... | sudo bash -s -- ..." >&2
    exit 1
  fi
}

ensure_linux() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "This installer currently supports Linux only." >&2
    exit 1
  fi
}

ensure_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

detect_host_ip() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

install_dependencies() {
  if [[ "${SKIP_DOCKER_INSTALL}" == "true" ]]; then
    return
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Automatic dependency install currently supports apt-based Linux only." >&2
    echo "Install Docker, docker compose, openssl and curl manually or rerun with --skip-docker-install." >&2
    exit 1
  fi

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  if ! apt-get install -y ca-certificates curl openssl docker.io docker-compose-plugin; then
    apt-get install -y ca-certificates curl openssl docker.io docker-compose
  fi
  systemctl enable docker
  systemctl restart docker
}

detect_compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return
  fi
  echo "docker compose is not available." >&2
  exit 1
}

write_compose_file() {
  install -d "${FPTN_DIR}"
  install -d "${FPTN_CONFIG_DIR}"

  cat > "${FPTN_DIR}/docker-compose.yml" <<EOF
services:
  fptn-server:
    image: ${FPTN_IMAGE}
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
      - NET_RAW
      - SYS_ADMIN
    sysctls:
      - net.ipv4.ip_forward=1
      - net.ipv6.conf.all.forwarding=1
      - net.ipv4.conf.all.rp_filter=0
      - net.ipv4.conf.default.rp_filter=0
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - "${NODE_PORT}:443/tcp"
    volumes:
      - ${FPTN_CONFIG_DIR}:/etc/fptn
    environment:
      ENABLE_DETECT_PROBING: "true"
      DEFAULT_PROXY_DOMAIN: "${PROXY_DOMAIN}"
      ALLOWED_SNI_LIST: ""
      DISABLE_BITTORRENT: "true"
      USE_REMOTE_SERVER_AUTH: "false"
      SERVER_EXTERNAL_IPS: "${NODE_HOST}"
EOF
}

ensure_certs() {
  if [[ -f "${FPTN_CONFIG_DIR}/server.crt" && -f "${FPTN_CONFIG_DIR}/server.key" ]]; then
    return
  fi

  openssl req \
    -x509 \
    -nodes \
    -newkey rsa:2048 \
    -keyout "${FPTN_CONFIG_DIR}/server.key" \
    -out "${FPTN_CONFIG_DIR}/server.crt" \
    -days 3650 \
    -subj "/CN=${NODE_HOST}"
}

start_fptn() {
  "${COMPOSE_CMD[@]}" -f "${FPTN_DIR}/docker-compose.yml" pull
  "${COMPOSE_CMD[@]}" -f "${FPTN_DIR}/docker-compose.yml" up -d
}

open_firewall() {
  if [[ "${OPEN_UFW}" != "true" ]]; then
    return
  fi
  if ! command -v ufw >/dev/null 2>&1; then
    return
  fi
  if ! ufw status 2>/dev/null | grep -q "Status: active"; then
    return
  fi
  ufw allow "${NODE_PORT}/tcp"
}

install_node_controller() {
  local installer_path="$1"
  local cmd=(
    bash "${installer_path}"
    --panel-url "${PANEL_URL}"
    --shared-token "${SHARED_TOKEN}"
    --transport "${NODE_TRANSPORT}"
    --grpc-port "${PANEL_GRPC_PORT}"
    --node-name "${NODE_NAME}"
    --node-host "${NODE_HOST}"
    --node-port "${NODE_PORT}"
    --region "${NODE_REGION}"
    --cert-path "${FPTN_CONFIG_DIR}/server.crt"
  )

  if [[ -n "${PANEL_GRPC_TARGET}" ]]; then
    cmd+=(--grpc-target "${PANEL_GRPC_TARGET}")
  fi
  if [[ -n "${AGENT_ID}" ]]; then
    cmd+=(--agent-id "${AGENT_ID}")
  fi
  if [[ -n "${NODE_TIER}" ]]; then
    cmd+=(--tier "${NODE_TIER}")
  fi
  if [[ -n "${LOCAL_METRICS_URL}" ]]; then
    cmd+=(--metrics-url "${LOCAL_METRICS_URL}")
  fi
  if [[ "${REPLACE_EXISTING}" == "true" ]]; then
    cmd+=(--replace-existing)
  fi
  if [[ -n "${REPLACE_AGENT_ID}" ]]; then
    cmd+=(--replace-agent-id "${REPLACE_AGENT_ID}")
  fi

  "${cmd[@]}"
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
    --proxy-domain)
      PROXY_DOMAIN="$2"
      shift 2
      ;;
    --metrics-url)
      LOCAL_METRICS_URL="$2"
      shift 2
      ;;
    --fptn-image)
      FPTN_IMAGE="$2"
      shift 2
      ;;
    --fptn-dir)
      FPTN_DIR="$2"
      shift 2
      ;;
    --fptn-config-dir)
      FPTN_CONFIG_DIR="$2"
      shift 2
      ;;
    --open-ufw)
      OPEN_UFW="true"
      shift
      ;;
    --replace-existing)
      REPLACE_EXISTING="true"
      shift
      ;;
    --replace-agent-id)
      REPLACE_AGENT_ID="$2"
      shift 2
      ;;
    --fptn-only)
      FPTN_ONLY="true"
      shift
      ;;
    --skip-docker-install)
      SKIP_DOCKER_INSTALL="true"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

trap cleanup EXIT
ensure_root
ensure_linux
ensure_command bash
ensure_command curl

if [[ -z "${NODE_HOST}" ]]; then
  NODE_HOST="$(detect_host_ip || true)"
fi
if [[ -z "${NODE_HOST}" ]]; then
  echo "--node-host is required." >&2
  exit 1
fi

if [[ -z "${NODE_NAME}" ]]; then
  NODE_NAME="$(hostname)"
fi

if [[ "${NODE_TRANSPORT}" != "http" && "${NODE_TRANSPORT}" != "grpc" ]]; then
  echo "--transport must be one of: http, grpc" >&2
  exit 1
fi

if [[ -n "${NODE_TIER}" && "${NODE_TIER}" != "public" && "${NODE_TIER}" != "premium" && "${NODE_TIER}" != "censored" ]]; then
  echo "--tier must be one of: public, premium, censored" >&2
  exit 1
fi

if [[ "${FPTN_ONLY}" != "true" ]]; then
  if [[ -z "${PANEL_URL}" ]]; then
    echo "--panel-url is required unless --fptn-only is used." >&2
    exit 1
  fi
  if [[ -z "${SHARED_TOKEN}" ]]; then
    echo "--shared-token is required unless --fptn-only is used." >&2
    exit 1
  fi
fi

install_dependencies
ensure_command openssl
ensure_command docker
detect_compose_cmd
write_compose_file
ensure_certs
start_fptn
open_firewall

if [[ "${FPTN_ONLY}" != "true" ]]; then
  TMP_DIR="$(mktemp -d)"
  curl -fsSL "${RAW_BASE}/install-via-github.sh" -o "${TMP_DIR}/install-via-github.sh"
  chmod +x "${TMP_DIR}/install-via-github.sh"
  install_node_controller "${TMP_DIR}/install-via-github.sh"
fi

echo
echo "FPTN node bootstrap completed."
echo "FPTN compose: ${FPTN_DIR}/docker-compose.yml"
echo "FPTN config:  ${FPTN_CONFIG_DIR}"
echo "Public port:  ${NODE_HOST}:${NODE_PORT}"
echo
echo "Verify:"
echo "  docker ps"
echo "  ss -ltnp | grep ${NODE_PORT}"
echo "  curl -vk https://${NODE_HOST}:${NODE_PORT}/"
