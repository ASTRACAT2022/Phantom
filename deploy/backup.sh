#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PHANTOM_ENV_FILE:-/etc/phantom-control-plane.env}"
BACKUP_DIR="${PHANTOM_BACKUP_DIR:-/var/backups/phantom-control-plane}"
RETENTION_DAYS="${PHANTOM_BACKUP_RETENTION_DAYS:-14}"
TMP_DIR=""

cleanup() {
  if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
    rm -rf "${TMP_DIR}"
  fi
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash deploy/backup.sh" >&2
    exit 1
  fi
}

ensure_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
  fi

  APP_NAME="${APP_NAME:-Phantom Control Plane}"
  DATABASE_URL="${DATABASE_URL:-}"
  DATABASE_PATH="${DATABASE_PATH:-/var/lib/phantom-control-plane/panel.db}"
  FPTN_CONFIG_DIR="${FPTN_CONFIG_DIR:-/var/lib/phantom-control-plane/fptn-config}"
  PANEL_HOST="${PANEL_HOST:-0.0.0.0}"
  PANEL_PORT="${PANEL_PORT:-8000}"
}

backup_sqlite() {
  local source_db="$1"
  local target_db="$2"

  python3 - "$source_db" "$target_db" <<'PY'
import sqlite3
import sys

source_path = sys.argv[1]
target_path = sys.argv[2]

source = sqlite3.connect(source_path)
target = sqlite3.connect(target_path)
with target:
    source.backup(target)
target.close()
source.close()
PY
}

main() {
  trap cleanup EXIT

  ensure_root
  ensure_command python3
  ensure_command tar
  ensure_command find

  load_env

  if [[ ! -d "${FPTN_CONFIG_DIR}" ]]; then
    echo "FPTN config dir not found: ${FPTN_CONFIG_DIR}" >&2
    exit 1
  fi

  install -d "${BACKUP_DIR}"
  TMP_DIR="$(mktemp -d)"

  local timestamp archive_name archive_path
  timestamp="$(date +%Y%m%d-%H%M%S)"
  archive_name="phantom-backup-${timestamp}.tar.gz"
  archive_path="${BACKUP_DIR}/${archive_name}"

  install -d "${TMP_DIR}/data" "${TMP_DIR}/config"
  if [[ "${DATABASE_URL}" == postgres://* || "${DATABASE_URL}" == postgresql://* ]]; then
    ensure_command pg_dump
    pg_dump "${DATABASE_URL}" -Fc -f "${TMP_DIR}/data/panel.dump"
  else
    if [[ ! -f "${DATABASE_PATH}" ]]; then
      echo "Database not found: ${DATABASE_PATH}" >&2
      exit 1
    fi
    backup_sqlite "${DATABASE_PATH}" "${TMP_DIR}/data/panel.db"
  fi
  cp -R "${FPTN_CONFIG_DIR}/." "${TMP_DIR}/config/"

  if [[ -f "${ENV_FILE}" ]]; then
    cp "${ENV_FILE}" "${TMP_DIR}/phantom-control-plane.env"
  fi

  cat > "${TMP_DIR}/metadata.txt" <<EOF
app_name=${APP_NAME}
created_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
panel_host=${PANEL_HOST}
panel_port=${PANEL_PORT}
database_path=${DATABASE_PATH}
database_url=${DATABASE_URL}
fptn_config_dir=${FPTN_CONFIG_DIR}
backup_dir=${BACKUP_DIR}
EOF

  tar -czf "${archive_path}" -C "${TMP_DIR}" .

  if [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] && [[ "${RETENTION_DAYS}" -gt 0 ]]; then
    find "${BACKUP_DIR}" -type f -name 'phantom-backup-*.tar.gz' -mtime +"${RETENTION_DAYS}" -delete
  fi

  echo "Backup created: ${archive_path}"
}

main "$@"
