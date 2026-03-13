#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/phantom-node-controller"
ENV_FILE="/etc/phantom-node-controller.env"
SERVICE_FILE="/etc/systemd/system/phantom-node-controller.service"

install -d "${INSTALL_DIR}"
install -m 755 "${SCRIPT_DIR}/agent.py" "${INSTALL_DIR}/agent.py"
install -m 644 "${SCRIPT_DIR}/phantom-node-controller.service" "${SERVICE_FILE}"

if [[ ! -f "${ENV_FILE}" ]]; then
  install -m 600 "${SCRIPT_DIR}/config.env.example" "${ENV_FILE}"
  echo "Created ${ENV_FILE}. Edit it before first start."
fi

systemctl daemon-reload
systemctl enable phantom-node-controller.service

echo
echo "Installation complete."
echo "1. Edit ${ENV_FILE}"
echo "2. Start the agent: systemctl restart phantom-node-controller.service"
echo "3. Check logs: journalctl -u phantom-node-controller.service -f"
