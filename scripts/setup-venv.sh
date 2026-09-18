#!/usr/bin/env bash
# Create a Python virtualenv for the Snap7 Industrial Gateway and install it.
#
# This is the development / bench setup. For a supervised service installation
# use install/linux/install.sh instead.
#
#   ./scripts/setup-venv.sh              # runtime only
#   ./scripts/setup-venv.sh --dev        # also pytest, httpx, ruff
#   ./scripts/setup-venv.sh --locked     # pin every transitive package too
#   ./scripts/setup-venv.sh --venv /opt/gw/venv
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
REQUIREMENTS="requirements.txt"
WITH_DEV=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)    WITH_DEV=1; REQUIREMENTS="requirements-dev.txt"; shift ;;
    --locked) REQUIREMENTS="requirements.lock.txt"; shift ;;
    --venv)   VENV_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# --- 1. Find an interpreter new enough -----------------------------------
PYTHON=""
for candidate in python3.13 python3.14 python3 python; do
  if command -v "${candidate}" >/dev/null 2>&1 && \
     "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)'; then
    PYTHON="${candidate}"
    break
  fi
done

if [[ -z "${PYTHON}" ]]; then
  echo "ERROR: Python 3.13 or newer is required but was not found." >&2
  echo "       Found: $(python3 --version 2>&1 || echo 'no python3')" >&2
  echo "       Debian/Ubuntu: sudo apt-get install python3.13 python3.13-venv" >&2
  echo "       RHEL/Rocky:    sudo dnf install python3.13" >&2
  exit 1
fi
echo "==> Using $("${PYTHON}" --version) at $(command -v "${PYTHON}")"

# --- 2. Create the virtualenv --------------------------------------------
if [[ -d "${VENV_DIR}" ]]; then
  echo "==> Reusing existing virtualenv at ${VENV_DIR}"
else
  echo "==> Creating virtualenv at ${VENV_DIR}"
  "${PYTHON}" -m venv "${VENV_DIR}"
fi
PIP="${VENV_DIR}/bin/pip"
VPYTHON="${VENV_DIR}/bin/python"

echo "==> Upgrading pip"
"${PIP}" install --quiet --upgrade pip

# --- 3. Install dependencies ---------------------------------------------
echo "==> Installing ${REQUIREMENTS}"
"${PIP}" install --requirement "${PROJECT_ROOT}/${REQUIREMENTS}"

# --- 4. Install the gateway itself ---------------------------------------
# --no-deps because the requirements file above already decided every version;
# letting pip re-resolve here would silently defeat the pins.
echo "==> Installing the gateway (editable, no dependency re-resolution)"
"${PIP}" install --quiet --no-deps --editable "${PROJECT_ROOT}"

# --- 5. Verify -----------------------------------------------------------
echo "==> Verifying the Snap7 library"
if "${VPYTHON}" - <<'PY'
import sys
try:
    import snap7
    snap7.client.Client()
except Exception as exc:
    print(f"    Snap7 client library did NOT load: {exc}")
    sys.exit(1)
print("    Snap7 client library loads")
PY
then :; else
  echo "    The gateway can still host the virtual S7 CPU, but it cannot poll a"
  echo "    real PLC until this is fixed. See docs/build.md."
fi

echo "==> Verifying the CLI"
"${VENV_DIR}/bin/snap7-gateway" --version

cat <<EOF

Done. Next steps:

  Activate:   source ${VENV_DIR}/bin/activate
  Run:        snap7-gateway run --data-dir ./gw-data --port 8443
EOF
if [[ ${WITH_DEV} -eq 1 ]]; then
  echo "  Test:       pytest tests/ -q"
fi
echo "
The first-run administrator password is printed ONCE at startup and must be
changed at first sign-in before any other page is reachable."
