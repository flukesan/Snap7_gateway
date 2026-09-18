# CLAUDE.md — Snap7 Industrial Gateway

> หมายเหตุ: เอกสารนี้เขียนเป็นภาษาอังกฤษตามธรรมเนียมไฟล์ CLAUDE.md (ให้ Claude Code และเครื่องมืออื่นอ่านได้ตรงกัน) ส่วน docstring/comment ในโค้ดให้เขียนเป็นภาษาอังกฤษเช่นกัน แต่ log message ที่ user เห็นในเว็บ UI ให้รองรับ Thai/English ตาม locale config

This file guides Claude Code when working in this repository. Read it in full before writing or modifying any code.

## 1. Project Overview

**Snap7 Industrial Gateway** is a production-grade, 24/7 background service that:
- Connects to Siemens S7 PLCs (S7-300/400/1200/1500 and S7-compatible controllers such as Sinumerik-integrated PLCs) via the Snap7 protocol library.
- Reads **all standard memory areas**: Data Blocks (DB), Inputs (I), Outputs (Q), and Merkers/Flags (M) — functionally equivalent to Siemens NetLink Pro.
- Exposes collected data outward to **DeviceWise** (PTC IIoT platform), with per-tag access control (expose everything vs. an explicit whitelist).
- Ships with a **plain, icon-free web configuration UI** for all connection settings (PLC side and device/network side).
- Enforces secure first-login behavior (default credentials → forced password change with strength validation).
- Includes built-in connection and data-read test tools.
- Logs extensively for post-mortem debugging, including capturing state around unexpected crashes/restarts.
- Runs identically on **Windows** (as a Windows Service) and **Linux** (as a systemd service).

This is safety- and uptime-critical industrial software. Stability, security, and predictable behavior take priority over feature velocity or code elegance.

## 2. Tech Stack (fixed — do not substitute without explicit approval)

- **Language**: Python 3.13+
- **PLC communication**: `python-snap7` (wraps the Snap7 C library)
  - Windows: bundle `snap7.dll` (x64) with the installer/build.
  - Linux: build/install `libsnap7.so` per target architecture (x86_64, arm64/aarch64 for Raspberry Pi/edge deployments). Document the build steps for each target in `/docs/build.md`.
- **Web config server**: FastAPI (async, good for concurrent polling + web UI) with `uvicorn` as the ASGI server, or Flask if the team prefers sync simplicity — **decide once at project start and stay consistent**; document the choice and rationale in `/docs/architecture.md`.
- **Frontend**: Server-rendered HTML (Jinja2 templates) + minimal vanilla JS. No component frameworks, no icon libraries, no external CDN dependencies at runtime (air-gapped factory networks are expected).
- **Database / config storage**: SQLite for local config, credentials, and tag mapping (single-file, no external DB server dependency — this must run standalone on an edge device).
- **Password hashing**: `argon2-cffi` (Argon2id) — never store plaintext or reversible-encrypted passwords.
- **Process supervision**:
  - Windows: `pywin32` to register as a native Windows Service, with auto-restart on failure.
  - Linux: systemd unit file with `Restart=on-failure`.
- **Logging**: Python `logging` with `RotatingFileHandler` (size + backup count limits so disk never fills).
- **DeviceWise integration**: DeviceWise talks **native S7 protocol directly** (its "S7-400 PLC" / Siemens device driver, `Connection: Direct`) — confirmed from the live DeviceWise device config. The gateway therefore does **not** need an OPC UA/Modbus/MQTT adapter for this integration. Instead, `python-snap7`'s server module (`snap7.server`) is used to expose the gateway itself as a **virtual S7 PLC** that DeviceWise connects to exactly as it would a real one. See Section 4.6.

## 3. Non-Functional Requirements (read before every change)

1. **24/7 industrial stability**
   - The polling engine must never crash the whole process due to a single PLC disconnect, timeout, or malformed response. Isolate each PLC connection in its own supervised loop/task with exponential backoff reconnect.
   - No unbounded memory growth — profile long-running behavior; queues and buffers must have caps.
   - Config changes via the web UI must apply without requiring a full service restart wherever possible (hot-reload the polling engine's connection pool).

2. **Security**
   - Web UI must be served over HTTPS by default (self-signed cert generated on first run if none provided; allow the operator to supply a real cert).
   - All authentication endpoints must be rate-limited and lock out after repeated failures (with configurable threshold and cool-down).
   - Default credentials (`admin` / a randomly generated or clearly-labeled default password shown once at first run) **must force a password change before any other action is allowed** — no dashboard access, no PLC config, nothing, until the password is changed.
   - Password strength policy (configurable, sane defaults): minimum length 12, must not be in a common-password blocklist, must not equal the username, must not be identical to the previous password. Reject weak passwords with a clear reason, not just "invalid."
   - Sanitize and validate every web UI input server-side, not just client-side (this is an industrial network — assume hostile or misconfigured devices may be on the same VLAN).
   - No default open write access to PLCs from the web UI unless explicitly enabled per-connection (read-only by default is the safer posture for a "gateway" whose job is monitoring, not control).
   - Session tokens must be secure, HttpOnly, expire, and be invalidated on logout and password change.

3. **Documentation discipline**
   - **Every code change that affects behavior, configuration, or setup must be accompanied by a corresponding update to `/docs/MANUAL.md` (or the relevant sub-doc) in the same commit/PR.** Claude Code must treat an undocumented behavior change as an incomplete task.
   - Maintain a `/docs/CHANGELOG.md` following Keep a Changelog format.

## 4. Functional Requirements

### 4.1 PLC Connection Layer
- Support multiple simultaneous PLC connections, each independently configured: IP, rack, slot, connection type (PG/OP/S7-Basic), timeout, poll interval.
- Read full memory areas per connection: DB (with DB number + start/length or full-DB read), I, Q, M — mirroring NetLink Pro's "read everything, then let the consumer decide what it needs" philosophy.
- Support configurable data types per address (BOOL, BYTE, INT, DINT, REAL, STRING, WORD, DWORD) so raw bytes are decoded correctly at the gateway, not left for downstream systems to guess.
- Auto-reconnect with backoff on connection loss; surface connection health state in the web UI (per-connection status: Connected / Reconnecting / Failed, with last successful read timestamp).

### 4.2 Cross-Platform Execution
- Single codebase, no platform-specific business logic branches beyond the service-registration and Snap7 library-loading layers.
- Provide a Windows installer (or at minimum a documented `pywin32` service install script) and a Linux systemd unit + install script.
- CI (or at minimum a documented manual checklist) must verify the service starts, survives a forced kill, and auto-restarts on both platforms before release.

### 4.3 Web Configuration UI
- Plain HTML, **no icon fonts/SVG icon sets, no decorative imagery** — text labels and buttons only, per explicit requirement.
- Sections: 
  - PLC Connections (add/edit/delete, per-connection test button)
  - Device/Output Side (DeviceWise settings: endpoint, protocol, credentials, tag exposure mode)
  - Tag Mapping (address ↔ friendly name ↔ data type ↔ exposure whitelist flag)
  - Users & Security (password change, session settings, password policy config)
  - Logs (viewable/downloadable from the UI, with severity filter)
  - System Status (uptime, connection health, last crash info if any)
- All settings persisted immediately to SQLite on save, with validation feedback before save succeeds.

### 4.4 Authentication & First-Run Flow
- First run generates a default `admin` account with a default or randomly generated password (log it once to console/log file at first boot only).
- On first login, the UI must force a password-change screen before granting access to any other page.
- Enforce the password policy from Section 3.2 at this step and on every future password change.
- Support at least one additional operator role (e.g., read-only viewer) if time allows — otherwise document it as a known future enhancement, not silently skipped.

### 4.5 Connection & Read Test Tools
- "Test Connection" button per PLC config: attempts connect + immediate disconnect, reports latency and success/failure with the underlying Snap7 error code/message (not a generic "failed").
- "Test Read" tool: lets the operator specify an address (DB/I/Q/M + offset + type) and immediately see the live decoded value, without needing to save the config first — critical for commissioning on the factory floor.

### 4.6 DeviceWise Output — Virtual S7 PLC (Snap7 Server)

**Confirmed architecture** (verified against a live DeviceWise device configuration screen — DeviceWise's Siemens driver connects with `Connection: Direct` straight to an S7-comm endpoint, currently pointed at an ACCON NetLink-Pro box):

```
[Real PLC] --Snap7 CLIENT (gateway polls)--> [Gateway] --Snap7 SERVER (gateway hosts)--> [DeviceWise]
```

The gateway runs two Snap7 roles simultaneously:
1. **Client role** — polls the real PLC(s) per Section 4.1.
2. **Server role** (`snap7.server.Server`) — hosts a virtual S7 CPU that DeviceWise connects to over plain Direct S7-comm, no config change needed on the DeviceWise side beyond pointing its IP at the gateway instead of the current NetLink-Pro box.

Requirements specific to the Server role:
- On startup (and on manual "Rescan" from the web UI), the gateway's Client role must call Snap7's block-listing function (`ListBlocks` / `GetAgBlockInfo`) against the real PLC to obtain the live list of DBs and their exact sizes — this is what makes DeviceWise's own auto-enumeration (seen producing the `DB1 UINT1[20304]`, `DB2 UINT1[810]`, ... tree) return correct, real sizes rather than guessed/placeholder ones.
- For every area to be exposed, the gateway must call `registerArea()` on the Snap7 Server with the **same area type and size** as reported by the real PLC (I, Q, M, and each DB individually) — unlike a real CPU, the Snap7 Server does not auto-populate its own block list; the gateway must build it explicitly from the discovery step above.
- **Whitelist enforcement happens at registration time, not at read/query time**: an area not on the whitelist is simply never `registerArea()`'d on the Server. The practical effect is that DeviceWise's own enumeration will not show that DB/area at all — it does not exist on the virtual CPU, rather than existing-but-hidden.
- **Mirror All** mode = register every discovered area unconditionally.
- **Whitelist** mode = register only areas explicitly flagged "exposed" in the Tag Mapping table.
- A background sync task keeps each registered area's buffer updated from the latest Client-role poll results, so a DeviceWise read always returns current data.
- The Server role must support the same `Connection: Direct` semantics DeviceWise already expects (IP/port 102, rack/slot response to the standard S7 connection request) so cutover from the current ACCON NetLink-Pro box is a config-only change on the DeviceWise side (new IP), not a driver change.

### 4.7 Auto-Discovery & Rescan
- On demand (web UI "Rescan" button) and optionally on a configurable interval, re-run the block-discovery step against each connected real PLC.
- Diff the new discovery result against the currently-registered Server areas:
  - New DB appears on the real PLC → register it on the Server automatically only if the connection's exposure mode is Mirror All; if Whitelist mode, add it to the Tag Mapping table as **unexposed by default** and require an operator to explicitly whitelist it (never silently expose new data).
  - A DB disappears or shrinks on the real PLC → flag it in the web UI (Tag Mapping status column) rather than silently deregistering it, since a live consumer (DeviceWise) may still be querying the old size/shape.
- Log every discovery diff (added/changed/removed blocks) to the audit trail described in Section 4.8's logging requirements below.

### 4.8 Logging & Crash Diagnostics
- Structured log format (timestamp, level, module, connection ID where relevant, message).
- Rotating file logs with configurable retention (size + count).
- On unhandled exception or unexpected process exit, write a dedicated crash snapshot (last N log lines + Python traceback + active connection states) to a separate `crash/` folder so a silent crash can still be diagnosed after the fact.
- Log every config change (who, what, when) as an audit trail — this touches security-relevant settings.

## 5. Suggested Project Structure

```
snap7-gateway/
├── CLAUDE.md
├── src/
│   ├── core/            # polling engine, connection manager, reconnect logic
│   ├── plc/
│   │   ├── client.py    # Snap7 Client role — polls real PLCs
│   │   ├── discovery.py # ListBlocks/GetAgBlockInfo based DB discovery + rescan diffing
│   │   └── decoding.py  # data-type decoding (BOOL/BYTE/INT/DINT/REAL/STRING/WORD/DWORD)
│   ├── web/             # FastAPI/Flask app, routes, templates, static (no icon assets)
│   ├── auth/            # login, password policy, session handling
│   ├── virtual_plc/     # Snap7 Server role — virtual S7 CPU exposed to DeviceWise
│   │   ├── server.py    # registerArea() management, whitelist enforcement at registration
│   │   └── sync.py      # background task copying live poll data into registered areas
│   ├── logging_/        # log setup, crash snapshot handler
│   ├── service/         # windows_service.py, linux systemd helper scripts
│   └── db/              # SQLite models/migrations (config, users, tag map)
├── docs/
│   ├── MANUAL.md         # operator-facing manual — update every change
│   ├── architecture.md
│   ├── build.md          # per-platform Snap7 build/install instructions
│   └── CHANGELOG.md
├── tests/
├── install/
│   ├── windows/
│   └── linux/
└── pyproject.toml
```

## 6. Development Guidelines for Claude Code

- Prefer explicit, defensive code around all PLC I/O — a malformed read must never propagate an exception up through the web layer or crash the polling loop for other connections.
- Never log credentials or full session tokens, even at debug level.
- Every new configurable behavior needs: (1) a sane default, (2) server-side validation, (3) a `docs/MANUAL.md` entry.
- Write unit tests for data-type decoding logic (BOOL/BYTE/INT/DINT/REAL/STRING/WORD/DWORD) independent of a live PLC — use recorded/mocked byte buffers.
- Write integration tests against a Snap7 server simulator (Snap7 ships a server component) for the Client role, and against a real `python-snap7` client for the virtual PLC (Server role) — both directions of the bridge need coverage, not just the PLC-facing side.
- Never let the Server role register or update an area that isn't backed by a successful, current read from the Client role — a stale or unregistered area must read as a clear fault to DeviceWise, not stale/garbage bytes.
- Ask before changing the fixed tech stack choices in Section 2.

## 7. Definition of Done (per feature)

- [ ] Works on both Windows and Linux
- [ ] Survives a forced kill / PLC disconnect without taking down other connections
- [ ] Input validated server-side
- [ ] Covered by at least one automated test where feasible
- [ ] `docs/MANUAL.md` and `docs/CHANGELOG.md` updated
- [ ] No plaintext secrets in logs or config files on disk
