#!/usr/bin/env bash
# Remove the service and program files. The data directory is kept unless
# --purge is passed, because it holds the configuration and the audit trail.
set -euo pipefail

PREFIX="/opt/snap7-gateway"
DATA_DIR="/var/lib/snap7-gateway"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root." >&2
  exit 1
fi

systemctl disable --now snap7-gateway.service 2>/dev/null || true
rm -f /etc/systemd/system/snap7-gateway.service
systemctl daemon-reload
rm -rf "${PREFIX}"

if [[ "${1:-}" == "--purge" ]]; then
  echo "Removing ${DATA_DIR} (configuration, logs, audit trail)"
  rm -rf "${DATA_DIR}"
  userdel snap7gw 2>/dev/null || true
else
  echo "Kept ${DATA_DIR}. Pass --purge to delete it as well."
fi
