#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${PHANTOM_GITHUB_REPO:-ASTRACAT2022/Phantom}"
REPO_REF="${PHANTOM_GITHUB_REF:-main}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_REF}/node-controller"
TMP_DIR=""

usage() {
  cat <<EOF
Phantom node-controller GitHub installer

This script downloads the latest node-controller files from GitHub and runs auto-deploy.

Usage:
  curl -fsSL ${RAW_BASE}/install-via-github.sh | sudo bash -s -- --panel-url http://SERVER_IP:8000 --shared-token TOKEN [options]

Optional environment overrides:
  PHANTOM_GITHUB_REPO   Default: ASTRACAT2022/Phantom
  PHANTOM_GITHUB_REF    Default: main

Forwarded options:
  --panel-url URL
  --shared-token TOKEN
  --transport http|grpc
  --grpc-target HOST:PORT
  --grpc-port PORT
  --agent-id ID
  --node-name NAME
  --node-host HOST
  --node-port PORT
  --region REGION
  --tier public|premium|censored
  --cert-path PATH
  --config-dir PATH
  --metrics-url URL
  --interface IFACE
  --heartbeat-interval SEC
  --request-timeout SEC
  --replace-existing
  --replace-agent-id ID
  --help
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

ensure_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

download_file() {
  local file_name="$1"
  local target_path="$2"
  curl -fsSL "${RAW_BASE}/${file_name}" -o "${target_path}"
}

main() {
  if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
  fi

  trap cleanup EXIT

  ensure_root
  ensure_command curl
  ensure_command bash

  TMP_DIR="$(mktemp -d)"

  echo "Downloading Phantom node-controller from ${REPO_SLUG}@${REPO_REF}..."
  download_file "agent.py" "${TMP_DIR}/agent.py"
  download_file "phantom-node-controller.service" "${TMP_DIR}/phantom-node-controller.service"
  download_file "auto-deploy.sh" "${TMP_DIR}/auto-deploy.sh"

  chmod +x "${TMP_DIR}/auto-deploy.sh"

  echo "Starting auto-deploy..."
  bash "${TMP_DIR}/auto-deploy.sh" "$@"
}

main "$@"
