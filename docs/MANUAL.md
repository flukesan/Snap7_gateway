# Operator Manual — Snap7 Industrial Gateway

> เอกสารนี้เป็นภาษาอังกฤษตามธรรมเนียมของโครงการ ส่วนหน้าเว็บรองรับทั้ง
> **English** และ **ไทย** เลือกได้ที่ *Users & Security → Presentation → Language*

This manual covers installing, configuring and operating the gateway. Every
configurable behaviour in the software has an entry here.

---

## 1. What the gateway does

It polls your Siemens S7 PLCs and re-hosts their data as a **virtual S7 CPU**
that DeviceWise connects to exactly as it connects to a real PLC. Replacing an
ACCON NetLink-Pro box means pointing the DeviceWise device at the gateway's IP
address — no driver change, no protocol change.

It reads data blocks (DB), inputs (I), outputs (Q) and merkers/flags (M), and
lets you decide per area whether DeviceWise may see it at all.

---

## 2. Installation

### 2.1 Linux (systemd)

```bash
sudo ./install/linux/install.sh
```

This creates the `snap7gw` service account, installs into `/opt/snap7-gateway`,
creates `/var/lib/snap7-gateway`, and enables the unit with
`Restart=on-failure`. The unit grants `CAP_NET_BIND_SERVICE` so the virtual CPU
can bind TCP/102 without running as root.

```bash
systemctl status snap7-gateway
journalctl -u snap7-gateway -f
tail -f /var/lib/snap7-gateway/logs/gateway.log
```

Remove with `sudo ./install/linux/uninstall.sh` (add `--purge` to delete the
data directory as well).

### 2.2 Windows (native service)

From an **elevated** PowerShell prompt:

```powershell
.\install\windows\install-service.ps1
```

This installs into `%ProgramFiles%\Snap7Gateway`, stores data in
`%ProgramData%\Snap7Gateway`, registers the `Snap7Gateway` service for automatic
start, and configures restart-on-failure (5 s, 10 s, then every 30 s).

Allow the S7 port through the firewall:

```powershell
New-NetFirewallRule -DisplayName "Snap7 Gateway S7" -Direction Inbound `
  -Protocol TCP -LocalPort 102 -Action Allow
```

Remove with `.\install\windows\uninstall-service.ps1` (add `-Purge`).

### 2.3 Running in the foreground (commissioning, bench work)

Set up a virtualenv with the pinned dependencies:

```bash
./scripts/setup-venv.sh --dev          # Linux / macOS
.\scripts\setup-venv.ps1 -Dev          # Windows (PowerShell)
```

The script picks a Python 3.13+ interpreter, creates `.venv`, installs the
requirements, installs the gateway itself, and checks that the Snap7 client
library loads. Options: `--dev` / `-Dev` adds the test tooling, `--locked` /
`-Locked` pins every transitive package as well, and `--venv DIR` / `-VenvDir`
puts the environment somewhere else.

Then:

```bash
source .venv/bin/activate
snap7-gateway run --data-dir ./gw-data --port 8443
```

### 2.4 Dependency files

| File | Contents |
| --- | --- |
| `requirements.txt` | Runtime dependencies, pinned exactly. What a deployment installs |
| `requirements-dev.txt` | The above plus pytest, httpx and ruff |
| `requirements-windows.txt` | The above plus pywin32 for the Windows service |
| `requirements.lock.txt` | Every package including transitive ones, pinned — for reproducible, audited or air-gapped installations |
| `pyproject.toml` | The version *ranges* the project is compatible with, and the package metadata |

Install by hand instead of using the script:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -e .
```

`--no-deps` on the second line is deliberate: the requirements file has already
decided every version, and letting pip re-resolve would quietly defeat the pins.

A test (`tests/test_packaging.py`) fails if `requirements.txt` and
`pyproject.toml` ever drift apart, or if the lock file goes stale.

See `docs/build.md` for the Snap7 C library on each platform and architecture.

---

## 3. First run

1. The service generates an `admin` account with a random 20-character password
   and writes it to the log **once**:

   ```
   ========================================================================
   FIRST RUN: a default administrator account has been created.
     username: admin
     password: 7kQ$mVr2Xp9wTz4nB!Ld
   This password is shown ONCE and must be changed at first login ...
   ========================================================================
   ```

   Find it with `journalctl -u snap7-gateway | grep -A4 "FIRST RUN"` or in
   `<data-dir>/logs/gateway.log`.

2. Open `https://<gateway-host>:8443/`. The certificate is self-signed on first
   run, so the browser will warn; accept it, or install your own certificate
   (section 7.4).

3. Sign in. You are sent straight to **Change your password** and **no other
   page is reachable** until the change succeeds — not the dashboard, not the
   PLC configuration, nothing.

4. Choose a password that satisfies the policy (section 7.2). Rejections tell
   you exactly what is wrong, for example *"'siemens' is a well-known word;
   adding digits or symbols around it does not make it safe"*.

5. All sessions are signed out after the change. Sign in again with the new
   password.

**Lost the password?** On the gateway host:

```bash
sudo -u snap7gw /opt/snap7-gateway/venv/bin/snap7-gateway reset-password admin
sudo -u snap7gw /opt/snap7-gateway/venv/bin/snap7-gateway unlock admin
```

---

## 4. PLC Connections

*PLC Connections* → **Add**.

| Field | Meaning | Default |
| --- | --- | --- |
| Name | Label used in the UI, logs and audit trail | — |
| PLC address | IPv4/IPv6 address or hostname | — |
| Rack | S7 rack number (0–7) | 0 |
| Slot | S7 slot number (0–31). S7-300/400 usually 2; S7-1200/1500 usually 1 | 2 |
| TCP port | ISO-on-TCP port | 102 |
| Connection type | `PG`, `OP` or `S7_BASIC` — the connection resource requested | PG |
| Timeout (ms) | Per-request timeout, 250–60000 | 3000 |
| Poll interval (ms) | Time between poll cycles, 50–3600000 | 1000 |
| Enabled | Unticking stops polling without deleting the configuration | on |
| Inputs / Outputs / Merkers | Which non-DB areas to read | all on |
| Max input / output / merker bytes | Upper bound for size probing (see below) | 1024 / 1024 / 8192 |
| Exposure mode | `Mirror All` or `Whitelist only` | Whitelist |
| Allow writes to this PLC | Enables the write path for this connection | **off** |

**Why the "max bytes" fields exist.** An S7 CPU reports the exact size of every
data block, but *not* of its process image or merker area, and the usable size
differs per CPU family. The gateway therefore probes downward from the value you
give until a read succeeds, and registers the size the CPU actually served.

**Writes are off by default.** A gateway's job is monitoring. Only enable writes
on a connection that genuinely needs to control the PLC.

### 4.1 Test Connection

Each connection has a **Test Connection** button. It connects, identifies the
CPU, counts the visible data blocks and disconnects, then reports either

* `Line 3: connected in 12 ms - CPU 315-2 PN/DP, 4 DB(s) visible`, or
* the real Snap7 failure text and error code, for example
  `connecting to 10.20.30.40:102 (rack 0, slot 2) - TCP connection failed:
  [Errno 111] Connection refused`.

Never a generic "failed".

### 4.2 Editing without downtime

Saving a connection applies immediately. Only transport-level fields (address,
rack, slot, port, connection type, timeout, poll interval, enabled) restart that
one connection's worker; everything else is applied in place. Other connections
keep polling throughout. A service restart is never required except for the web
server's own bind address, port and TLS settings, which the UI tells you about.

---

## 5. Tag Mapping

### 5.1 Discovered areas

After a connection comes up, the gateway enumerates the PLC's blocks and lists
them here with their **real sizes** — this is what makes DeviceWise's own
enumeration show `DB1 UINT1[20304]` instead of a guess.

| Column | Meaning |
| --- | --- |
| Area | `DB<n>`, `I`, `Q` or `M` |
| Size (bytes) | The size reported by the PLC |
| Exposed to DeviceWise | Whether this area is on the whitelist |
| Registered | Whether it is live on the virtual CPU right now |
| Live data | Age and size of the newest successful read |
| Status | `New`, `OK`, `Larger than before`, `Smaller than before`, `Missing on the PLC` |

### 5.2 Exposure

* **Mirror All** — every discovered area is registered on the virtual CPU
  automatically, including areas found by a later rescan.
* **Whitelist only** — only areas you tick are registered. An unticked area
  **does not exist** on the virtual CPU: DeviceWise cannot enumerate it at all,
  rather than seeing it empty. New areas found by a rescan always arrive
  unexposed, so nothing is ever exposed without a deliberate decision.

Un-exposing an area withdraws it from the virtual CPU within one sync tick.

### 5.3 Rescan

**Rescan** re-runs block discovery against the connection. It can also run on a
timer — *Device / Output Side → Discovery → Automatic rescan interval*
(0 = manual only, the default).

What a rescan does with the differences:

| Change | Mirror All | Whitelist only |
| --- | --- | --- |
| New DB appears | Registered automatically | Added as **unexposed**; you decide |
| DB grew | Re-registered at the new size | Same, if it is exposed |
| DB shrank | Flagged `Smaller than before`; **keeps its old registered size** | Same |
| DB disappeared | Flagged `Missing on the PLC`; **stays registered** | Same |

Shrunk and missing blocks are deliberately *not* deregistered automatically: a
live DeviceWise consumer may still be querying the old shape. For a shrunk
block, the bytes the PLC still provides are refreshed normally and the remainder
is filled with explicit zeros. When you are satisfied, press **Confirm new
size** to re-register it at the smaller size, or **Delete** to drop a block that
is gone for good.

Every difference is written to the audit trail.

### 5.4 Importing names and comments from the PLC project

**Snap7 cannot read a symbol table off a PLC.** The S7 protocol returns bytes
and block sizes; a classic S7-300/400 CPU does not store symbolic names or
comments at all - they live in the STEP 7 or TIA Portal project. So to know
that `MW20` is *Speed setpoint in rpm*, export that information from the
engineering tool and import it here. The gateway then joins the two by address.

Go to *Tag Mapping* → **Import names and comments**.

#### What to export, and from where

| Source | How to export | Covers |
| --- | --- | --- |
| STEP 7 classic (SIMATIC Manager) | Symbol table → Table → Export → `.sdf` (recommended) or `.asc` | I, Q, M and absolute DB addresses, with comments |
| TIA Portal | PLC tags → tag table → Export to file → `.xlsx` (or `.csv`) | I, Q, M with comments |
| TIA Portal / STEP 7 | Right-click a data block → Generate source from blocks → `.db` / `.scl` / `.awl`, or an Openness `.xml` | The whole structure of one DB, with member comments |

German projects are handled: `E` (Eingang) reads as `I` and `A` (Ausgang) as
`Q`. Both `%MW20` (TIA) and `MW    20` (STEP 7 column padding) are understood.

#### Data blocks must use standard, not optimized, access

A symbol table gives absolute addresses, so no arithmetic is needed. A data
block *declaration* does not - the gateway computes each member's byte offset
from the declaration order using the S7 layout rules (BOOL packs eight to a
byte, words align to even bytes, `STRING[n]` takes n+2 bytes, structures are
word-aligned and padded).

Those rules only exist for **standard block access**. A block with *Optimized
block access* ticked has no fixed offsets at all, and Snap7 cannot address it.
The import refuses such a file and says so. To fix it in TIA Portal: block
properties → Attributes → clear **Optimized block access** → compile → download
→ export again.

#### The import form

| Field | Meaning |
| --- | --- |
| Export file | The file from the table above |
| Read as | Leave on *Detect from the file*; override if the extension is misleading |
| DB number | Only for a data-block source that does not state its own number |
| Name prefix | Prepended to every imported name, e.g. `Line3_`, so two blocks with a `Speed` member do not collide |
| Update existing tags with the same name | Off by default: an existing tag is left alone and reported |
| Apply | **Off by default.** Leave it unticked to see exactly what would happen; tick it and upload again to write |

Previewing changes nothing, and the preview report is identical to what
applying does.

#### Reading the report

* **New tags** - created.
* **Updated tags** - an existing tag of the same name was replaced (only with
  *Update existing tags* ticked, and only when something actually differs).
* **Left alone** - already present. Nothing is overwritten silently.
* **Rejected** - failed the same validation the manual tag form applies.
* **Not usable from this file** - rows the gateway will not map. Timers (`T5`),
  counters (`C5`/`Z5`), block names (`DB10`, `FC1`) and structures/arrays are
  expected here; anything else is worth reading.

Names are adjusted where they have to be: a character the tag table does not
allow becomes `_` (so `Valve/Open` imports as `Valve_Open`), names longer than
64 characters are truncated, and a collision gets a `_2` suffix. Every
adjustment is shown in the report.

Types the gateway cannot decode faithfully are imported as a raw view with an
honest note in the comment - `TIME` becomes `DINT` with *"IEC TIME raw,
milliseconds"* appended - and types with no scalar reading at all (`DATE_AND_TIME`,
`DTL`, `WSTRING`) are skipped with their reason.

#### From the command line

Useful for bulk commissioning, and it works without signing in:

```bash
snap7-gateway import-symbols --connection "Line 3" line3.sdf              # preview
snap7-gateway import-symbols --connection "Line 3" line3.sdf --apply      # write
snap7-gateway import-symbols --connection "Line 3" recipes.db \
    --kind db_source --db-number 12 --prefix Recipes_ --apply
```

Add `--overwrite` to replace existing tags of the same name.

#### What the import does *not* do

Imported names and comments are for the operator, on this gateway. They are not
sent to DeviceWise: the virtual S7 CPU publishes whole memory areas, so
DeviceWise still enumerates `DB1 UINT1[20304]` and names its own tags on its
side. Importing also does not change which areas are polled or exposed - but a
named tag does cause its area to be polled, so its live value can be shown.

Every import is recorded in the audit trail.

### 5.5 Named tags

Below the area table you can name individual addresses:

| Field | Notes |
| --- | --- |
| Name | Unique within the connection |
| Area / DB number / Byte offset | The address |
| Bit | BOOL only (0–7) |
| Data type | BOOL, BYTE, CHAR, WORD, DWORD, INT, DINT, UINT, UDINT, REAL, LREAL, STRING |
| Length | STRING only — the declared maximum character count (on the wire: length + 2) |

Named tags show their live decoded value and how old that value is. They also
cause their area to be polled even when it is not exposed to DeviceWise.

---

## 6. Device / Output Side (DeviceWise)

This page configures the virtual S7 CPU and shows what it is currently serving.

| Setting | Meaning | Default |
| --- | --- | --- |
| Run the virtual S7 CPU | Master switch for the server role | on |
| Bind address | Which interface to listen on (`0.0.0.0` = all) | 0.0.0.0 |
| TCP port | S7 port DeviceWise connects to | 102 |
| Rack / Slot (reported) | Values shown to operators for the DeviceWise device configuration | 0 / 2 |
| Max clients | Simultaneous S7 clients | 64 |
| Buffer sync interval (ms) | How often registered buffers are refreshed, 50–60000 | 250 |
| Stale timeout (s) | An area whose newest good read is older than this is **unregistered** | 30 |
| Automatic rescan interval (min) | 0 = manual only | 0 |
| Max DBs to enumerate | Ceiling for `ListBlocks` | 2048 |

**Port 102 requires privileges.** On Linux the systemd unit grants
`CAP_NET_BIND_SERVICE`; running by hand needs root or a port above 1023. On
Windows the service runs as LocalSystem. If the bind fails, the page shows the
reason and the rest of the gateway keeps running.

**The stale timeout is a safety feature.** If a PLC stops answering, the gateway
withdraws its areas rather than letting DeviceWise read bytes that are no longer
true. DeviceWise then gets a clean read failure, which is a diagnosable fault
instead of silently frozen data.

### 6.1 Cutting over from the NetLink-Pro box

1. Configure the PLC connection here and confirm data is flowing (Tag Mapping
   shows fresh ages).
2. Expose the areas DeviceWise needs.
3. In DeviceWise, open the Siemens device that currently points at the
   NetLink-Pro box and change only its **address** to the gateway's IP. Leave
   the driver, `Connection: Direct`, rack and slot as they are.
4. Re-run DeviceWise's enumeration. The tree should show the same DBs at the
   same sizes.

---

## 7. Users & Security

### 7.1 Accounts and roles

| Role | Can do |
| --- | --- |
| `admin` | Everything: connections, tag mapping, settings, users |
| `viewer` | Read-only: System Status, connections list, tag mapping, logs, audit |

New accounts must change their password at first sign-in. The **last enabled
administrator cannot be demoted, disabled or deleted**, so the gateway can never
lock every administrator out. Resetting a user's password signs out all of their
sessions and forces another change at next sign-in.

### 7.2 Password policy

| Setting | Default |
| --- | --- |
| Minimum length (hard floor 12, cannot be configured lower) | 12 |
| Require lower-case / upper-case / digit | on / on / on |
| Require symbol | off |
| Must not contain the username | on |
| Must differ from the current and previous password | on |
| Reject common passwords | on |

The blocklist rejects well-known passwords, keyboard and counting runs, passwords
built around a common word (including Siemens/automation vocabulary) and
passwords using three or fewer distinct characters. Extend it for your site by
creating `<data-dir>/password-blocklist.txt`, one word per line (`#` comments
allowed).

### 7.3 Sign-in protection

| Setting | Meaning | Default |
| --- | --- | --- |
| Failures before lockout | Per-account | 5 |
| Lockout duration (minutes) | | 15 |
| Attempts per window | Per source IP, in front of the account logic | 10 |
| Rate-limit window (seconds) | | 60 |

Clear a lockout from *Users & Security → Unlock*, or with
`snap7-gateway unlock <username>` on the host.

### 7.4 Sessions

| Setting | Default |
| --- | --- |
| Idle timeout (minutes) | 30 |
| Absolute lifetime (hours) | 12 |

Cookies are `HttpOnly`, `SameSite=Lax` and `Secure` over HTTPS. Only the SHA-256
of the session token is stored. Sessions end on logout, on password change, when
the account is disabled or deleted, and at either timeout.

### 7.5 TLS

HTTPS is on by default. On first run — and whenever the stored certificate is
missing, unreadable or within 30 days of expiry — a self-signed certificate is
generated into `<data-dir>/certs`.

To use your own certificate, set *Certificate path* and *Private key path* to
PEM files readable by the service account, then restart the service. The
Users & Security page shows the active certificate's subject, issuer and
validity dates.

Turning HTTPS off sends credentials in clear text and is only acceptable on an
isolated bench network. The service logs a warning when you do.

---

## 8. Test Tools

**Test Read** reads one live value without saving anything first — what you want
while commissioning at the machine.

Pick a connection, an area, an offset and a data type, then press **Read**. You
get the decoded value, the raw bytes in hex, and whether the read came from the
live polling connection or a one-off connection (used when the connection is
disabled or not yet connected). Failures show the real Snap7 error text.

The read is queued on the same thread as that connection's poll cycle, so it can
never race the polling loop.

---

## 9. Logs

*Logs* shows the most recent entries from memory, filtered by minimum severity,
newest first. **Download** fetches the current rotating log file.

**Service log level** changes the level for the whole service immediately, with
no restart.

| Setting | Meaning | Default |
| --- | --- | --- |
| Log level | DEBUG / INFO / WARNING / ERROR / CRITICAL | INFO |
| Max bytes per file | Rotation threshold | 10 MiB |
| Backup count | Rotated files kept (so total ≈ 110 MiB) | 10 |
| In-memory lines | Entries kept for this page and crash snapshots | 2000 |
| Crash log lines | Lines embedded in a crash snapshot | 500 |

Log line format:

```
2026-01-31T09:12:44.512+0700 | WARNING  | snap7_gateway.core.worker | conn=3 | read DB12 failed: ...
```

Credentials and session tokens are never logged, at any level. The single
exception is the first-run administrator password, once, by design.

---

## 10. System Status

* Uptime, start time, data directory, number of discovered areas.
* Per-connection health: state (Connected / Connecting / Reconnecting / Failed /
  Disabled / Stopped), last successful read, poll and error counts, last
  discovery summary and the last error text.
* Virtual S7 CPU: listening address, registered areas and bytes, connected
  clients, sync statistics, and any areas currently **withheld** because they
  have no current read.
* Crash diagnostics: whether the previous run exited uncleanly, and a
  downloadable list of crash snapshots.

---

## 11. Audit Trail

Every configuration change is recorded: who, what, when and from which address.
Covered actions include sign-in and sign-out, failed sign-ins and lockouts,
password changes and resets, user create/role/enable/delete, connection
create/update/delete/test, exposure changes, shape confirmations, tag
create/delete, settings changes, rescan requests, every discovery difference,
and log/crash downloads.

The trail is trimmed to the most recent 20 000 entries at shutdown so it cannot
fill an edge device's disk.

---

## 12. Files on disk

| Path | Contents |
| --- | --- |
| `<data-dir>/gateway.db` | SQLite: settings, users, connections, areas, tags, audit (mode 0600) |
| `<data-dir>/logs/gateway.log` | Rotating log file |
| `<data-dir>/crash/` | Crash snapshots (JSON), newest 50 kept |
| `<data-dir>/certs/` | TLS certificate and private key (key mode 0600) |
| `<data-dir>/running.marker` | Present while running; left behind by an unclean exit |
| `<data-dir>/password-blocklist.txt` | Optional site-specific blocked words |

Default data directory: `/var/lib/snap7-gateway` (Linux),
`%ProgramData%\Snap7Gateway` (Windows). Override with `--data-dir` or the
`SNAP7_GATEWAY_DATA_DIR` environment variable. **Back up `gateway.db`** — it is
the entire configuration.

---

## 13. Command line

```
snap7-gateway run [--data-dir DIR] [--host H] [--port P] [--no-console-log]
                  [--trust-proxy-headers]
snap7-gateway install-service [--data-dir DIR] [--manual]   # Windows
snap7-gateway uninstall-service                             # Windows
snap7-gateway reset-password USERNAME
snap7-gateway unlock USERNAME
snap7-gateway show-config
snap7-gateway import-symbols --connection NAME FILE [--apply] [--overwrite]
                            [--kind auto|symbols|db_source] [--db-number N] [--prefix P]
```

`--trust-proxy-headers` makes the gateway honour `X-Forwarded-For`. Only use it
behind a reverse proxy you control: on a flat VLAN a spoofed header would defeat
rate limiting.

---

## 14. Troubleshooting

**The web UI does not answer.**
`systemctl status snap7-gateway`, then `journalctl -u snap7-gateway -n 100`.
A bind failure on the web port is logged at startup.

**"Virtual S7 CPU could not start: could not bind 0.0.0.0:102".**
Privileged port. On Linux confirm the unit still has
`AmbientCapabilities=CAP_NET_BIND_SERVICE`; on Windows confirm the service runs
as LocalSystem. Otherwise choose a port above 1023 and set DeviceWise to match.

**DeviceWise sees no data blocks.**
The connection is in Whitelist mode and nothing is ticked, or no area has a
current successful read. Check Tag Mapping: an area shows *Registered = No* if
either applies. System Status lists areas withheld for staleness.

**DeviceWise sees the wrong sizes.**
Press **Rescan**. If a DB shrank, the gateway intentionally keeps the old
registered size until you press **Confirm new size**.

**A connection sits in Reconnecting.**
Use **Test Connection** for the underlying Snap7 error. Wrong rack/slot is the
most common cause (S7-1200/1500 are usually rack 0 slot 1). Check that TCP/102
is reachable and that the CPU permits PG/OP connections.

**Values look wrong.**
Check the data type and offset with **Test Read** against the raw hex the tool
shows. All S7 scalars are big-endian; `DB10.DBX4.2` means byte 4, bit 2.

**The service restarted by itself.**
System Status shows whether the previous run exited uncleanly, and lists the
crash snapshots. Download one: it holds the traceback, the last 500 log lines,
every connection's state and a full thread dump.

**Locked out.**
`snap7-gateway unlock <user>` or `snap7-gateway reset-password <user>` on the
host.
