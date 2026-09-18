# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Import of tag names and comments from engineering exports, because Snap7
  cannot read a symbol table off a PLC - a classic S7 CPU does not store one.
  Reads STEP 7 classic symbol tables (`.sdf`, `.asc`, including German `E`/`A`
  mnemonics and fixed-width columns), TIA Portal PLC tag tables (`.xlsx` and
  `.csv`, parsed with the standard library so no spreadsheet dependency is
  added), and data-block sources (`.db`, `.scl`, `.awl`, TIA `.xml`).
- Data-block member offsets computed from the declaration using the S7
  standard-access layout rules: BOOL packing, word alignment, `STRING[n]` as
  n+2 bytes, and word-aligned padded structures and arrays. A block using
  optimized access is refused with the steps to change it, rather than
  producing offsets that would read the wrong bytes.
- Import preview by default: the Tag Mapping page and the CLI both report every
  created, updated, skipped, rejected and unusable row with its reason, and
  write nothing until the operator asks. Existing tags are never overwritten
  without `--overwrite` / "Update existing tags".
- `snap7-gateway import-symbols` for bulk commissioning from the console.
- `requirements.txt`, `requirements-dev.txt` and `requirements-windows.txt`
  with exact version pins, so a gateway installed today and one installed next
  year run identical code. `pyproject.toml` keeps the compatible-range
  declarations.
- `requirements.lock.txt` pinning every transitive package, for reproducible,
  audited or air-gapped installations and for building a wheelhouse.
- `scripts/setup-venv.sh` and `scripts/setup-venv.ps1`: locate a Python 3.13+
  interpreter, create the virtualenv, install the pinned requirements and the
  gateway, and verify that the Snap7 client library loads and the CLI runs.
- `tests/test_packaging.py` (24 tests) failing the build if `requirements.txt`
  and `pyproject.toml` drift apart, if a version is not pinned exactly, if the
  lock file goes stale, or if pywin32 leaks into a file used on Linux.

### Added
- A page left idle now signs itself out when the session's idle timeout runs
  out, instead of sitting open on an unattended screen until somebody clicks.
  A warning strip with a countdown and a "Stay signed in" button appears near
  the end of the window. It follows the existing **Idle timeout** setting rather
  than adding a second one, and the browser asks the server to extend a session
  only after real interaction - a plain timer ping would hold a session open
  next to an empty chair. With JavaScript disabled nothing is weakened: the
  server enforces the same timeout at the next request.
- `POST /api/keepalive` (CSRF-protected) extends the session and reports the
  time remaining, so the page can re-sync a countdown that a background tab may
  have throttled.

### Fixed
- Account actions (Save / Disable / Delete) overflowed the right-hand border of
  the Users & Security panel: the role dropdown took the full 30rem control
  width inside a `nowrap` cell. Action cells now flow and wrap, table controls
  size to their content, and the accounts table scrolls inside its panel on a
  narrow screen.
- The masthead **Log out** button was accent-blue text on the accent-blue
  masthead - visible only on hover. It now has its own contrast and a border, so
  it reads as a control without needing an icon.
- Controls in a form grid lined up with their labels rather than with each
  other, so a label wrapping onto two lines ("DB number (data block sources that
  omit it)") dragged its dropdown out of line. Grid cells are now columns whose
  control sits on the bottom edge, and file inputs are styled to match.
- Sign-in could become permanently impossible. The login form's anti-forgery
  check rendered a fresh token into the page without setting the matching
  cookie, so after one failure every later attempt failed too - on a correct
  password. Every render of the sign-in page now issues the matching cookie,
  and the token's lifetime went from 10 minutes to an hour so that fetching the
  first-run password from the log no longer expires the page.
- Routine client disconnects (a closed browser tab, an abandoned TLS handshake,
  DeviceWise dropping a socket) no longer write crash snapshots. On Windows the
  Proactor event loop reports every one of these through the asyncio exception
  handler, and because the snapshot folder is capped, that noise pushed genuine
  crashes out of it.

### Changed
- The manual and README showed Linux-only paths for installing by hand, which
  left a Windows operator without a working command. Both now give the
  `.venv\Scripts\...` form, the activated-virtualenv form, and note that
  `pip install --no-deps -e .` is what creates the `snap7-gateway` executable,
  with `python -m snap7_gateway` as the equivalent that needs no install.
  Troubleshooting covers "'snap7-gateway' is not recognized" and the case where
  a shell prompt was copied along with a pasted command.
- The first-run administrator account is now `admin` / `admin`, a documented
  default, instead of a generated password that had to be retrieved from the
  log. CLAUDE.md section 3.2 allows either; the forced password change before
  any other page is reachable (section 4.4) is unchanged and is what makes a
  known default acceptable. Because the credential is published, the sign-in
  page warns while it is still live and the service log repeats the warning at
  every start, not only the first. `SNAP7_GATEWAY_FIRST_RUN_PASSWORD` set
  before the first start selects `random` or an explicit password instead.
- The manual's first-run section now documents the default pair, states plainly
  what the published default costs and for how long, covers the environment
  override, and explains how to read a generated password out of the log on each
  platform; the troubleshooting section covers both sign-in page messages.
- `PlcTag.address` now renders the width that matches the data type, so an INT
  at byte 20 reads `MW20` rather than `MB20`.
- `install/linux/install.sh` and `install/windows/install-service.ps1` now
  install the pinned requirements first and then the gateway with `--no-deps`,
  so pip cannot silently re-resolve past the pins. `LOCKED=1` on the Linux
  script uses the fully pinned lock file.

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
- `docs/release-checklist.md` — manual pre-release verification standing in for
  CI on Windows and on real PLC hardware.

[Unreleased]: https://github.com/flukesan/snap7_gateway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/flukesan/snap7_gateway/releases/tag/v0.1.0
