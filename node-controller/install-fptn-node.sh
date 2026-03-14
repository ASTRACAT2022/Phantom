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
DNS_IPV4_PRIMARY="${FPTN_DNS_IPV4_PRIMARY:-77.239.113.0}"
DNS_IPV4_SECONDARY="${FPTN_DNS_IPV4_SECONDARY:-108.165.164.201}"
FPTN_IMAGE="${FPTN_SERVER_IMAGE:-fptnvpn/fptn-vpn-server:latest}"
FPTN_PROXY_IMAGE="${FPTN_PROXY_IMAGE:-fptnvpn/fptn-proxy-server}"
FPTN_DIR="${FPTN_SERVER_DIR:-/opt/fptn-server}"
FPTN_CONFIG_DIR="${FPTN_CONFIG_DIR:-/opt/fptn-server-data}"
FPTN_PROXY_PORT="${FPTN_PROXY_PORT:-18080}"
ALLOWED_SNI_LIST="${FPTN_ALLOWED_SNI_LIST:-}"
PROMETHEUS_SECRET_ACCESS_KEY="${FPTN_PROMETHEUS_SECRET_ACCESS_KEY:-}"
REMOTE_SERVER_AUTH_HOST="${FPTN_REMOTE_SERVER_AUTH_HOST:-127.0.0.1}"
REMOTE_SERVER_AUTH_PORT="${FPTN_REMOTE_SERVER_AUTH_PORT:-8080}"
MAX_ACTIVE_SESSIONS_PER_USER="${FPTN_MAX_ACTIVE_SESSIONS_PER_USER:-3}"
LOCAL_METRICS_URL="${LOCAL_FPTN_METRICS_URL:-}"
OPEN_UFW="false"
REPLACE_EXISTING="false"
REPLACE_AGENT_ID="${PHANTOM_REPLACE_AGENT_ID:-}"
FPTN_ONLY="false"
SKIP_DOCKER_INSTALL="false"
SKIP_NET_TUNING="false"
TMP_DIR=""
PANEL_ENV_FILE="/etc/phantom-control-plane.env"
PANEL_SERVICE_NAME="phantom-control-plane.service"
# Intentionally sorts after local `99-*.conf` profiles so FPTN tuning wins.
PERF_SYSCTL_FILE="/etc/sysctl.d/99-z-phantom-fptn-performance.conf"
LEGACY_PERF_SYSCTL_FILE="/etc/sysctl.d/98-phantom-fptn-performance.conf"

usage() {
  cat <<EOF
Phantom full FPTN node installer

This script prepares Docker, deploys FPTN on the selected port, generates a certificate,
validates metrics and TCP readiness, and then installs Phantom node-controller so the node appears in the admin panel.

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
  --dns-ipv4-primary IP      Upstream DNS primary, default: 77.239.113.0
  --dns-ipv4-secondary IP    Upstream DNS secondary, default: 108.165.164.201
  --allowed-sni-list CSV     Default: proxy domain
  --prometheus-key KEY       Secret for FPTN metrics, default: auto-generated
  --max-sessions COUNT       Default: 3
  --metrics-url URL          Forwarded to node-controller as LOCAL_FPTN_METRICS_URL
  --fptn-image IMAGE         Docker image, default: fptnvpn/fptn-vpn-server:latest
  --fptn-proxy-image IMAGE   Docker image, default: fptnvpn/fptn-proxy-server
  --fptn-dir PATH            Docker compose dir, default: /opt/fptn-server
  --fptn-config-dir PATH     FPTN config dir, default: /opt/fptn-server-data
  --fptn-proxy-port PORT     Local proxy-server port, default: 18080
  --open-ufw                 Open node port in UFW when active
  --replace-existing         Remove old node registration before re-registering
  --replace-agent-id ID      Explicit old agent id to remove
  --fptn-only                Deploy only FPTN, skip node-controller install
  --skip-docker-install      Do not apt install Docker / openssl
  --skip-net-tuning          Do not apply Phantom high-priority network performance sysctl profile
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

random_token() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}

detect_host_ip() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

detect_panel_host() {
  /usr/bin/python3 - "${PANEL_URL}" <<'PY'
import sys
from urllib.parse import urlsplit

print(urlsplit(sys.argv[1]).hostname or "")
PY
}

is_local_panel() {
  local panel_host="$1"
  if [[ -z "${panel_host}" ]]; then
    return 1
  fi
  if [[ "${panel_host}" == "127.0.0.1" || "${panel_host}" == "localhost" || "${panel_host}" == "::1" ]]; then
    return 0
  fi
  if hostname -I 2>/dev/null | tr ' ' '\n' | grep -Fxq "${panel_host}"; then
    return 0
  fi
  if [[ "$(hostname -f 2>/dev/null || true)" == "${panel_host}" ]]; then
    return 0
  fi
  if [[ "$(hostname 2>/dev/null || true)" == "${panel_host}" ]]; then
    return 0
  fi
  return 1
}

quote_env() {
  printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"]/\\&/g')"
}

set_panel_env_var() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${PANEL_ENV_FILE}"; then
    sed -i.bak "s|^${key}=.*|${key}=$(quote_env "${value}")|" "${PANEL_ENV_FILE}"
    rm -f "${PANEL_ENV_FILE}.bak"
  else
    printf '%s=%s\n' "${key}" "$(quote_env "${value}")" >> "${PANEL_ENV_FILE}"
  fi
}

detect_panel_service_user() {
  local service_user=""
  service_user="$(systemctl show -p User --value "${PANEL_SERVICE_NAME}" 2>/dev/null || true)"
  if [[ -z "${service_user}" ]]; then
    service_user="phantom"
  fi
  printf '%s\n' "${service_user}"
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

detect_docker_server_api_version() {
  local api_version=""
  api_version="$(docker version --format '{{.Server.APIVersion}}' 2>/dev/null || true)"
  if [[ -n "${api_version}" ]]; then
    printf '%s\n' "${api_version}"
    return
  fi

  if [[ -S /var/run/docker.sock ]]; then
    api_version="$(
      curl -fsS --unix-socket /var/run/docker.sock http://localhost/version 2>/dev/null | \
        /usr/bin/python3 - <<'PY'
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

api_version = str(payload.get("ApiVersion", "")).strip()
if api_version:
    print(api_version)
PY
    )"
  fi

  printf '%s\n' "${api_version}"
}

configure_docker_api_compat() {
  if [[ -n "${DOCKER_API_VERSION:-}" ]]; then
    return
  fi

  local server_api=""
  server_api="$(detect_docker_server_api_version)"
  if [[ -z "${server_api}" ]]; then
    return
  fi

  export DOCKER_API_VERSION="${server_api}"
  echo "Using Docker API compatibility mode: ${DOCKER_API_VERSION}"
}

apply_network_tuning() {
  if [[ "${SKIP_NET_TUNING}" == "true" ]]; then
    return
  fi

  # Clean up the old lower-priority profile so reruns migrate existing nodes too.
  if [[ -f "${LEGACY_PERF_SYSCTL_FILE}" && "${LEGACY_PERF_SYSCTL_FILE}" != "${PERF_SYSCTL_FILE}" ]]; then
    rm -f "${LEGACY_PERF_SYSCTL_FILE}"
  fi

  cat > "${PERF_SYSCTL_FILE}" <<'EOF'
# Phantom / FPTN throughput-oriented tuning
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 250000
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_mtu_probing = 1
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.ipv4.tcp_rmem = 4096 87380 67108864
net.ipv4.tcp_wmem = 4096 65536 67108864
EOF

  modprobe tcp_bbr 2>/dev/null || true
  sysctl --system >/dev/null
}

write_compose_file() {
  install -d "${FPTN_DIR}"
  install -d "${FPTN_CONFIG_DIR}"

  if [[ -z "${ALLOWED_SNI_LIST}" ]]; then
    ALLOWED_SNI_LIST="${PROXY_DOMAIN}"
  fi
  if [[ -z "${PROMETHEUS_SECRET_ACCESS_KEY}" ]]; then
    PROMETHEUS_SECRET_ACCESS_KEY="$(random_token)"
  fi

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
    ulimits:
      nproc:
        soft: 524288
        hard: 524288
      nofile:
        soft: 524288
        hard: 524288
      memlock:
        soft: 524288
        hard: 524288
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - "${NODE_PORT}:443/tcp"
    volumes:
      - ${FPTN_CONFIG_DIR}:/etc/fptn
    environment:
      ENABLE_DETECT_PROBING: "true"
      DEFAULT_PROXY_DOMAIN: "${PROXY_DOMAIN}"
      ALLOWED_SNI_LIST: "${ALLOWED_SNI_LIST}"
      DISABLE_BITTORRENT: "true"
      PROMETHEUS_SECRET_ACCESS_KEY: "${PROMETHEUS_SECRET_ACCESS_KEY}"
      USE_REMOTE_SERVER_AUTH: "false"
      REMOTE_SERVER_AUTH_HOST: "${REMOTE_SERVER_AUTH_HOST}"
      REMOTE_SERVER_AUTH_PORT: "${REMOTE_SERVER_AUTH_PORT}"
      MAX_ACTIVE_SESSIONS_PER_USER: "${MAX_ACTIVE_SESSIONS_PER_USER}"
      SERVER_EXTERNAL_IPS: "${NODE_HOST}"
      DNS_IPV4_PRIMARY: "${DNS_IPV4_PRIMARY}"
      DNS_IPV4_SECONDARY: "${DNS_IPV4_SECONDARY}"
    healthcheck:
      test: ["CMD", "sh", "-c", "pgrep dnsmasq && pgrep fptn-server"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
  fptn-proxy-server:
    image: ${FPTN_PROXY_IMAGE}
    restart: unless-stopped
    depends_on:
      - fptn-server
    ports:
      - "127.0.0.1:${FPTN_PROXY_PORT}:80/tcp"
    environment:
      FPTN_HOST: "${NODE_HOST}"
      FPTN_PORT: "${NODE_PORT}"
    command: /usr/bin/fptn-proxy --target-host "${NODE_HOST}" --target-port "${NODE_PORT}" --listen-port 80
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

wait_for_local_tcp() {
  local port="$1"
  local label="$2"
  local attempt
  for attempt in $(seq 1 30); do
    if /usr/bin/python3 - "${port}" <<'PY'
import socket
import sys

port = int(sys.argv[1])
try:
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
    then
      echo "Validated ${label} on 127.0.0.1:${port}."
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for ${label} on 127.0.0.1:${port}." >&2
  return 1
}

wait_for_metrics_payload() {
  local url="$1"
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS "${url}" 2>/dev/null | grep -Eq 'fptn_active_sessions|fptn_user_'; then
      echo "Validated FPTN metrics endpoint ${url}."
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for FPTN metrics at ${url}." >&2
  return 1
}

wait_for_panel_health() {
  local url="$1"
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS "${url}/health" 2>/dev/null | grep -q '"status":"ok"'; then
      echo "Validated panel health at ${url}/health."
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for panel health at ${url}/health." >&2
  return 1
}

verify_fptn_stack() {
  local metrics_url="http://127.0.0.1:${FPTN_PROXY_PORT}/api/v1/metrics/${PROMETHEUS_SECRET_ACCESS_KEY}"
  wait_for_local_tcp "${NODE_PORT}" "public FPTN port"
  wait_for_local_tcp "${FPTN_PROXY_PORT}" "local metrics proxy"
  wait_for_metrics_payload "${metrics_url}"
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

sync_local_panel_with_fptn() {
  local panel_host=""
  local panel_service_user=""
  local local_metrics_url=""
  panel_host="$(detect_panel_host)"
  if [[ ! -f "${PANEL_ENV_FILE}" ]]; then
    return
  fi
  if ! is_local_panel "${panel_host}"; then
    return
  fi
  if ! systemctl list-unit-files | grep -q "^${PANEL_SERVICE_NAME}"; then
    return
  fi
  if [[ ! -x "/opt/phantom-control-plane/.venv/bin/python" ]]; then
    return
  fi

  panel_service_user="$(detect_panel_service_user)"
  if id -u "${panel_service_user}" >/dev/null 2>&1; then
    chown -R "${panel_service_user}:${panel_service_user}" "${FPTN_CONFIG_DIR}"
    chmod 750 "${FPTN_CONFIG_DIR}"
    find "${FPTN_CONFIG_DIR}" -maxdepth 1 -type f -exec chmod 640 {} \;
  fi

  local_metrics_url="http://127.0.0.1:${FPTN_PROXY_PORT}/api/v1/metrics/${PROMETHEUS_SECRET_ACCESS_KEY}"
  set_panel_env_var "FPTN_CONFIG_DIR" "${FPTN_CONFIG_DIR}"
  set_panel_env_var "FPTN_PROMETHEUS_METRICS_URL" "${local_metrics_url}"
  set_panel_env_var "FPTN_PROMETHEUS_INSECURE_TLS" "false"
  systemctl restart "${PANEL_SERVICE_NAME}"
  wait_for_panel_health "${PANEL_URL}"

  (
    set -a
    # shellcheck disable=SC1090
    source "${PANEL_ENV_FILE}"
    set +a
    cd /opt/phantom-control-plane
    /opt/phantom-control-plane/.venv/bin/python - "${PROXY_DOMAIN}" <<'PY'
import sys
from app.config import load_settings
from app.service import ControlPlaneService

proxy_domain = sys.argv[1]
settings = load_settings()
service = ControlPlaneService(settings)
service.initialize()
defaults = service.dashboard()["node_defaults"]
service.update_node_defaults(
    default_node_host=str(defaults.get("host", "")),
    default_node_port=int(defaults.get("port", 8443) or 8443),
    default_node_tier=str(defaults.get("tier", "public")),
    default_node_region=str(defaults.get("region", "Unassigned")),
    default_proxy_domain=proxy_domain,
    node_transport_hint=str(defaults.get("transport_hint", "")),
)
service.sync_fptn()
print("Panel synced to local FPTN config dir.")
PY
  )
}

install_node_controller() {
  local installer_path="$1"
  local effective_metrics_url="${LOCAL_METRICS_URL}"
  if [[ -z "${effective_metrics_url}" ]]; then
    effective_metrics_url="http://127.0.0.1:${FPTN_PROXY_PORT}/api/v1/metrics/${PROMETHEUS_SECRET_ACCESS_KEY}"
  fi
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
    --config-dir "${FPTN_CONFIG_DIR}"
    --metrics-url "${effective_metrics_url}"
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
    --dns-ipv4-primary)
      DNS_IPV4_PRIMARY="$2"
      shift 2
      ;;
    --dns-ipv4-secondary)
      DNS_IPV4_SECONDARY="$2"
      shift 2
      ;;
    --allowed-sni-list)
      ALLOWED_SNI_LIST="$2"
      shift 2
      ;;
    --prometheus-key)
      PROMETHEUS_SECRET_ACCESS_KEY="$2"
      shift 2
      ;;
    --max-sessions)
      MAX_ACTIVE_SESSIONS_PER_USER="$2"
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
    --fptn-proxy-image)
      FPTN_PROXY_IMAGE="$2"
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
    --fptn-proxy-port)
      FPTN_PROXY_PORT="$2"
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
    --skip-net-tuning)
      SKIP_NET_TUNING="true"
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
configure_docker_api_compat
apply_network_tuning
write_compose_file
ensure_certs
start_fptn
verify_fptn_stack
open_firewall
sync_local_panel_with_fptn

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
echo "Metrics:      http://127.0.0.1:${FPTN_PROXY_PORT}/api/v1/metrics/${PROMETHEUS_SECRET_ACCESS_KEY}"
echo "DNS:          ${DNS_IPV4_PRIMARY}, ${DNS_IPV4_SECONDARY}"
echo "Metrics key:  ${PROMETHEUS_SECRET_ACCESS_KEY}"
echo
echo "Verify:"
echo "  docker ps"
echo "  ss -ltnp | grep ${NODE_PORT}"
echo "  curl -vk https://${NODE_HOST}:${NODE_PORT}/"
