# Snap7 Industrial Gateway

A 24/7 background service that polls Siemens S7 PLCs and re-hosts their data as
a **virtual S7 CPU** that PTC DeviceWise connects to exactly as it would a real
PLC.

```
  ┌────────────┐   Snap7 client (poll)   ┌───────────┐   Snap7 server (host)   ┌────────────┐
  │  Real PLC  │ ──────────────────────> │  Gateway  │ ──────────────────────> │ DeviceWise │
  └────────────┘                         └───────────┘                         └────────────┘
```

Replacing an ACCON NetLink-Pro box is a change of IP address on the DeviceWise
side — same Siemens driver, same `Connection: Direct`, same rack/slot.

## Features

- Multiple simultaneous PLC connections, each isolated: one disconnect never
  affects another, and auto-reconnect uses exponential backoff.
- Reads DB, I, Q and M. Data-block sizes come from the PLC itself
  (`ListBlocks` / `GetAgBlockInfo`), so DeviceWise enumerates true shapes.
- Per-area access control: **Mirror All** or **Whitelist only**. An unexposed
  area is never registered, so DeviceWise cannot even see that it exists.
- An area is only ever served from a current, successful read. Stale areas are
  withdrawn so a read fails cleanly instead of returning old data.
- Plain, icon-free web UI over HTTPS with forced first-login password change,
  Argon2id hashing, account lockout, CSRF protection and a full audit trail.
- Built-in Test Connection and Test Read tools for commissioning.
- Structured rotating logs plus crash snapshots for post-mortem debugging.
- Runs as a Windows service or a Linux systemd service from one codebase.
- English / Thai web UI.

## Quick start

```bash
# Linux
sudo ./install/linux/install.sh
journalctl -u snap7-gateway | grep -A4 "FIRST RUN"   # the one-time admin password

# Windows (elevated PowerShell)
.\install\windows\install-service.ps1
```

Then open `https://<gateway-host>:8443/`, sign in as `admin`, and change the
password — no other page is reachable until you do.

For a bench run without installing as a service:

```bash
./scripts/setup-venv.sh --dev          # Linux / macOS
.\scripts\setup-venv.ps1 -Dev          # Windows (PowerShell)

source .venv/bin/activate
snap7-gateway run --data-dir ./gw-data --port 8443
```

The script needs Python 3.13+, creates `.venv`, installs the pinned
requirements and checks that the Snap7 client library loads. To do it by hand:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or requirements-dev.txt
.venv/bin/pip install --no-deps -e .
```

| File | Contents |
| --- | --- |
| `requirements.txt` | Runtime dependencies, pinned exactly |
| `requirements-dev.txt` | The above plus pytest, httpx, ruff |
| `requirements-windows.txt` | The above plus pywin32 |
| `requirements.lock.txt` | Every transitive package pinned, for air-gapped or audited installs |

## Documentation

| Document | Contents |
| --- | --- |
| [docs/MANUAL.md](docs/MANUAL.md) | Operator manual: installation, every page, every setting, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | Architecture, the FastAPI decision record, concurrency model, design tie-breaks |
| [docs/build.md](docs/build.md) | Building and installing the Snap7 C library per platform and architecture |
| [docs/release-checklist.md](docs/release-checklist.md) | Manual pre-release verification: service restart on both platforms, PLC and DeviceWise cutover, security spot checks |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Release history |

## Tests

```bash
pytest tests/ -q
```

The suite covers both directions of the bridge: the client role against a Snap7
server simulator, and a real `python-snap7` client reading through the gateway's
virtual CPU. No hardware required.

## Requirements

Python 3.13+, `python-snap7`, FastAPI, uvicorn, Jinja2, argon2-cffi,
cryptography. SQLite is the only datastore — no external database server.
