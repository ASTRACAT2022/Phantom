#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PHANTOM_ENV_FILE:-/etc/phantom-control-plane.env}"
SERVICE_NAME="phantom-control-plane.service"
ARCHIVE_PATH=""
WITH_ENV="false"
RESTART_SERVICE="true"
TMP_DIR=""
HAS_SYSTEMCTL="false"

usage() {
  cat <<'EOF'
Usage:
  sudo bash deploy/restore.sh --archive /path/to/phantom-backup.tar.gz [options]

Options:
  --archive FILE   Backup archive path
  --with-env       Also restore /etc/phantom-control-plane.env from archive
  --no-restart     Do not restart phantom-control-plane.service after restore
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
    echo "Run as root: sudo bash deploy/restore.sh --archive ..." >&2
    exit 1
  fi
}

detect_systemctl() {
  if command -v systemctl >/dev/null 2>&1; then
    HAS_SYSTEMCTL="true"
  fi
}

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
  fi

  DATABASE_URL="${DATABASE_URL:-}"
  DATABASE_PATH="${DATABASE_PATH:-/var/lib/phantom-control-plane/panel.db}"
  FPTN_CONFIG_DIR="${FPTN_CONFIG_DIR:-/var/lib/phantom-control-plane/fptn-config}"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --archive)
        ARCHIVE_PATH="$2"
        shift 2
        ;;
      --with-env)
        WITH_ENV="true"
        shift
        ;;
      --no-restart)
        RESTART_SERVICE="false"
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

main() {
  trap cleanup EXIT

  parse_args "$@"
  ensure_root
  detect_systemctl

  if [[ -z "${ARCHIVE_PATH}" ]]; then
    echo "--archive is required." >&2
    exit 1
  fi

  if [[ ! -f "${ARCHIVE_PATH}" ]]; then
    echo "Archive not found: ${ARCHIVE_PATH}" >&2
    exit 1
  fi

  load_env

  TMP_DIR="$(mktemp -d)"
  tar -xzf "${ARCHIVE_PATH}" -C "${TMP_DIR}"

  if [[ ! -d "${TMP_DIR}/config" ]]; then
    echo "Invalid backup archive: missing config/" >&2
    exit 1
  fi

  if [[ "${HAS_SYSTEMCTL}" == "true" ]]; then
    systemctl stop "${SERVICE_NAME}" || true
  fi

  install -d "$(dirname "${DATABASE_PATH}")" "${FPTN_CONFIG_DIR}"
  if [[ -f "${TMP_DIR}/data/panel.db" ]]; then
    install -m 600 "${TMP_DIR}/data/panel.db" "${DATABASE_PATH}"
  elif [[ -f "${TMP_DIR}/data/panel.dump" ]]; then
    if [[ "${DATABASE_URL}" != postgres://* && "${DATABASE_URL}" != postgresql://* ]]; then
      echo "Backup contains PostgreSQL dump, but DATABASE_URL is not configured." >&2
      exit 1
    fi
    if ! command -v pg_restore >/dev/null 2>&1; then
      echo "pg_restore is required to restore PostgreSQL backups." >&2
      exit 1
    fi
    pg_restore --clean --if-exists --no-owner --no-privileges -d "${DATABASE_URL}" "${TMP_DIR}/data/panel.dump"
  else
    echo "Invalid backup archive: missing data/panel.db or data/panel.dump" >&2
    exit 1
  fi
  rm -rf "${FPTN_CONFIG_DIR}"
  install -d "${FPTN_CONFIG_DIR}"
  cp -R "${TMP_DIR}/config/." "${FPTN_CONFIG_DIR}/"

  if [[ "${WITH_ENV}" == "true" && -f "${TMP_DIR}/phantom-control-plane.env" ]]; then
    install -m 640 "${TMP_DIR}/phantom-control-plane.env" "${ENV_FILE}"
  fi

  chown -R phantom:phantom "$(dirname "${DATABASE_PATH}")" "${FPTN_CONFIG_DIR}" 2>/dev/null || true

  if [[ "${RESTART_SERVICE}" == "true" && "${HAS_SYSTEMCTL}" == "true" ]]; then
    systemctl restart "${SERVICE_NAME}"
  fi

  echo "Restore completed from: ${ARCHIVE_PATH}"
}

main "$@"
