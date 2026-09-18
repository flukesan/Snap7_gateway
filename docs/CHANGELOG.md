# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-18

First working gateway: polls real PLCs, re-hosts them as a virtual S7 CPU for
DeviceWise, and ships the configuration UI, service packaging and tests.

### Added

**PLC client role**
- Multi-connection polling, each PLC isolated in its own supervised task with a
  dedicated thread for its Snap7 handle.
- Exponential-backoff reconnect (1 s → 60 s, ±20 % jitter) with per-connection
  health state: Connected / Connecting / Reconnecting / Failed / Disabled /
  Stopped, plus last successful read, poll and error counts.
- Reads DB, I, Q and M. Data-block sizes come from `ListBlocks` +
  `GetAgBlockInfo`; I/Q/M sizes are probed downward from a configurable maximum
  because an S7 CPU does not report them.
- Data-type decoding and encoding for BOOL, BYTE, CHAR, WORD, DWORD, INT, DINT,
  UINT, UDINT, REAL, LREAL and STRING, with every failure raised as
  `DecodeError` rather than escaping as `IndexError`/`struct.error`.

**Virtual S7 CPU (server role)**
- `snap7.server`-based virtual CPU that DeviceWise connects to with
  `Connection: Direct`, so cutover from an ACCON NetLink-Pro box is a change of
  IP address only.
- Areas registered with the real sizes reported by the PLC, so DeviceWise's own
  enumeration returns true shapes.
- Exposure enforced at registration time: an unexposed area is never
  `registerArea()`'d and is therefore invisible to DeviceWise's enumeration.
  Mirror All and Whitelist modes per connection.
- Background sync task refreshing each registered buffer from the newest poll
  result, under the server's own area lock.
- Areas whose newest good read is older than the stale timeout are
  **unregistered**, so a DeviceWise read fails cleanly instead of returning
  stale bytes.

**Discovery and rescan**
- On-demand Rescan plus an optional interval.
- Diffing with added / grown / shrunk / missing classification, every difference
  written to the audit trail.
- New areas are never silently exposed in Whitelist mode; shrunk and missing
  blocks are flagged and keep their registered shape until an operator confirms.

**Web configuration UI**
- Server-rendered, icon-free, no external assets: System Status, PLC
  Connections, Tag Mapping, Device / Output Side, Test Tools, Users & Security,
  Logs and Audit Trail.
- Per-connection Test Connection (latency plus the real Snap7 error text) and a
  Test Read tool that works before anything is saved.
- English and Thai UI text, selectable at runtime.
- Read-only JSON status API.

**Security**
- HTTPS by default with a self-signed certificate generated and auto-renewed on
  first run; operator-supplied certificates supported.
- Argon2id password hashing (64 MiB, t=3, p=4).
- First-run admin password generated randomly, logged once, and a forced change
  before any other page is reachable.
- Configurable password policy with a 12-character hard floor, an offline
  common-password blocklist and specific rejection reasons.
- Per-account lockout plus per-source-IP rate limiting; uniform timing for
  unknown usernames.
- DB-backed sessions with idle and absolute expiry, `HttpOnly`/`SameSite`/
  `Secure` cookies, only token hashes stored, invalidation on logout, password
  change, disable and delete.
- CSRF protection on every state-changing request; server-side validation of
  every input; CSP and related security headers.
- `admin` and `viewer` roles, with the last enabled administrator protected from
  demotion, disabling and deletion.
- PLC writes disabled by default, per connection.

**Operations**
- Structured rotating logs with configurable size and retention, an in-memory
  tail for the UI, and a runtime log-level switch.
- Crash snapshots on any unhandled exception (main thread, worker thread,
  asyncio task or web request) containing the traceback, recent log lines, all
  connection states, the virtual CPU registration table and a thread dump.
- Unclean-shutdown detection through a runtime marker file, surfaced on System
  Status.
- Audit trail of every configuration change with who, what, when and source IP.
- Hot reload: connection and most settings changes apply without restarting the
  service.

**Packaging**
- Linux systemd unit with `Restart=on-failure`, `CAP_NET_BIND_SERVICE` for
  TCP/102 and systemd hardening, plus install/uninstall scripts.
- Windows service via pywin32 with automatic restart, plus install/uninstall
  PowerShell scripts.
- `snap7-gateway` CLI: `run`, `install-service`, `uninstall-service`,
  `reset-password`, `unlock`, `show-config`.

**Tests** (228)
- Decoding unit tests over recorded byte buffers, independent of any PLC.
- Password policy, blocklist, lockout, rate limiting and session tests.
- Server-side validation tests including hostile input.
- Discovery diff and rescan-policy tests.
- Client-role integration tests against a Snap7 server simulator.
- Full-bridge integration tests where a real `python-snap7` client reads through
  the gateway's virtual CPU, covering enumeration, live updates, whitelist
  invisibility, stale withdrawal and connection isolation.
- Web tests covering access control, CSRF, the forced first-login change, roles,
  lockout and each page.

### Documentation
- `docs/MANUAL.md` — operator manual covering every setting and its default.
- `docs/architecture.md` — architecture, the FastAPI decision record and the
  design tie-breaks.
- `docs/build.md` — per-platform Snap7 build and install instructions.

[Unreleased]: https://github.com/flukesan/snap7_gateway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/flukesan/snap7_gateway/releases/tag/v0.1.0
