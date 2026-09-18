#!/usr/bin/env bash
# Install the Snap7 Industrial Gateway as a systemd service.
#
#   sudo ./install.sh [/path/to/source]
#   sudo LOCKED=1 ./install.sh          # pin every transitive package too
#
# Creates a dedicated unprivileged account, installs the package into its own
# virtualenv under /opt, and enables the unit. Re-running upgrades in place and
# never touches the data directory.
set -euo pipefail

PREFIX="/opt/snap7-gateway"
DATA_DIR="/var/lib/snap7-gateway"
SERVICE_USER="snap7gw"
SOURCE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
UNIT_SRC="$(dirname "${BASH_SOURCE[0]}")/snap7-gateway.service"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root." >&2
  exit 1
fi

echo "==> Checking for the Snap7 C library"
if ! ldconfig -p | grep -q libsnap7; then
  echo "    libsnap7.so was not found. See docs/build.md for per-architecture build steps."
  echo "    Continuing: python-snap7 ships a bundled library on some platforms."
fi

echo "==> Creating service account '${SERVICE_USER}'"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Creating ${DATA_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}/logs" \
        "${DATA_DIR}/crash"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${DATA_DIR}/certs"

echo "==> Installing into ${PREFIX}"
install -d -m 0755 "${PREFIX}"
python3 -m venv "${PREFIX}/venv"
"${PREFIX}/venv/bin/pip" install --upgrade pip >/dev/null

# Install the pinned dependency set first, then the gateway itself with
# --no-deps: requirements.txt has already decided every version, and letting
# pip re-resolve here would silently defeat those pins.
REQUIREMENTS="${SOURCE_DIR}/requirements.txt"
if [[ -f "${SOURCE_DIR}/requirements.lock.txt" && -n "${LOCKED:-}" ]]; then
  REQUIREMENTS="${SOURCE_DIR}/requirements.lock.txt"
  echo "    using the fully pinned lock file"
fi
"${PREFIX}/venv/bin/pip" install -r "${REQUIREMENTS}"
"${PREFIX}/venv/bin/pip" install --no-deps "${SOURCE_DIR}"
install -d -m 0755 "${PREFIX}/docs"
cp -r "${SOURCE_DIR}/docs/." "${PREFIX}/docs/" 2>/dev/null || true

echo "==> Installing the systemd unit"
install -m 0644 "${UNIT_SRC}" /etc/systemd/system/snap7-gateway.service
systemctl daemon-reload
systemctl enable snap7-gateway.service
systemctl restart snap7-gateway.service

echo
echo "Installed. Useful commands:"
echo "  systemctl status snap7-gateway"
echo "  journalctl -u snap7-gateway -f"
echo "  tail -f ${DATA_DIR}/logs/gateway.log"
echo
echo "The first-run administrator password is printed ONCE in the log above."
echo "Open https://<this-host>:8443/ and change it before doing anything else."
