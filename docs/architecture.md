# Architecture

## 1. What this service is

The Snap7 Industrial Gateway sits between Siemens S7 PLCs and PTC DeviceWise. It
runs two Snap7 roles at once:

```
  ┌────────────┐   S7-comm (client role)   ┌──────────────┐   S7-comm (server role)   ┌────────────┐
  │  Real PLC  │ <──────────────────────── │   Gateway    │ ────────────────────────> │ DeviceWise │
  │ S7-300/400 │      gateway polls        │              │   gateway hosts a         │  Siemens   │
  │ 1200/1500  │                           │              │   virtual S7 CPU          │  driver    │
  └────────────┘                           └──────────────┘                           └────────────┘
```

DeviceWise's Siemens driver already speaks native S7 with `Connection: Direct`
(verified against the live device configuration, which currently points at an
ACCON NetLink-Pro box). So the gateway does not need an OPC UA, Modbus or MQTT
adapter: it presents itself *as a PLC*. Cutting over is a change of IP address
on the DeviceWise side and nothing else.

## 2. Decision record: FastAPI + uvicorn

CLAUDE.md section 2 requires this choice to be made once and documented.

**Chosen: FastAPI with uvicorn.**

Rationale:

* The gateway is asynchronous at its core. Each PLC connection is a supervised
  `asyncio` task, and the virtual-CPU sync loop is another. Running the web UI
  on the same event loop means the UI reads live in-memory state directly - no
  second process, no shared file, no IPC.
* A "Test Read" against a dead PLC blocks for the configured timeout. With an
  async server that request occupies one task while every other request and
  every polling loop keeps running. Under Flask's synchronous model it would
  occupy a worker thread and the deployment would need a WSGI server tuned for
  it.
* Dependency injection gives one place to enforce "signed in", "is an admin"
  and "CSRF token valid", so a newly added route cannot silently skip a guard.
* uvicorn terminates TLS directly, so there is no nginx/IIS requirement on an
  edge box.

Consequences accepted:

* Every blocking Snap7 call must be dispatched to a thread. That is handled in
  exactly one place per role (`ConnectionWorker._call`, `AreaSyncTask`), never
  ad hoc.
* FastAPI's automatic OpenAPI docs are switched **off** (`docs_url=None`): an
  industrial network should not be handed a self-describing API surface.

The frontend is server-rendered Jinja2 with one stylesheet and ~10 lines of
vanilla JS. No component framework, no icon set, no CDN: factory networks are
air-gapped and the brief calls for text labels only. The UI works with
JavaScript disabled - the JS only adds confirmation prompts on destructive
actions.

## 3. Module map

| Package | Responsibility |
| --- | --- |
| `snap7_gateway/paths.py` | Resolves the single data directory (DB, logs, crash, certs) |
| `snap7_gateway/logging_/` | Structured rotating logs, in-memory tail, crash snapshots |
| `snap7_gateway/db/` | SQLite schema, typed rows, accessors, audit trail |
| `snap7_gateway/plc/` | **Client role**: `client.py` (I/O), `discovery.py` (block map + diff), `decoding.py` (data types) |
| `snap7_gateway/core/` | `worker.py` (one supervised loop per PLC), `manager.py` (hot reload), `datastore.py` (latest values), `validation.py`, `runtime.py` (composition root) |
| `snap7_gateway/virtual_plc/` | **Server role**: `server.py` (registerArea management), `sync.py` (buffer refresh) |
| `snap7_gateway/auth/` | Argon2id hashing, password policy, blocklist, sessions, lockout |
| `snap7_gateway/web/` | FastAPI app, routes, Jinja2 templates, TLS, i18n |
| `snap7_gateway/service/` | CLI, uvicorn entrypoint, Windows service wrapper |

The tree inside `src/snap7_gateway/` mirrors CLAUDE.md section 5 exactly; the
one addition is the top-level package name, so the project is `pip install`-able
without putting a bare `src/` on `sys.path`.

## 4. Threading and concurrency model

```
main thread ── asyncio event loop
   ├── uvicorn HTTP server           (request handlers)
   ├── ConnectionWorker task  #1 ────┐
   ├── ConnectionWorker task  #2 ────┼─> each owns a 1-thread executor
   ├── ConnectionWorker task  #N ────┘   (all Snap7 client calls run there)
   └── AreaSyncTask                  ──> asyncio.to_thread per tick
```

Rules:

1. **One Snap7 client handle per thread.** A Snap7 `S7Object` is not safe for
   concurrent use, so each worker pins its client to a dedicated single-worker
   `ThreadPoolExecutor`. The Test Read tool submits to that same executor, so it
   queues behind the poll cycle instead of racing it.
2. **A blocked PLC blocks only its own thread.** The event loop, the web UI and
   every other connection keep running.
3. **The worker task never raises.** `_supervise()` catches everything, records
   it in `ConnectionHealth`, and the loop reconnects with exponential backoff
   (1s → 60s, ±20% jitter).
4. **The datastore is the only shared mutable state** between the roles, and it
   is guarded by a lock and bounded by configuration (one entry per configured
   area, replaced in place - no queues, no history).

## 5. Data flow

1. **Connect.** `ConnectionWorker` opens a `PlcClient`.
2. **Discover.** `ListBlocks` → DB numbers; `GetAgBlockInfo` → each DB's exact
   `MC7Size`. I/Q/M have no block info on an S7 CPU, so their usable size is
   probed downward from the configured maximum until a read succeeds.
3. **Diff and persist.** `apply_discovery` compares against `plc_areas` and
   writes the result. New areas are unexposed in Whitelist mode, exposed in
   Mirror All mode. Existing exposure flags are never overwritten. Every change
   goes to the audit trail.
4. **Poll.** Each cycle reads every area that is either exposed or referenced by
   a named tag, and publishes the bytes plus a timestamp into the `DataStore`.
5. **Sync.** `AreaSyncTask` decides what the virtual CPU should publish and
   copies fresh bytes into the registered buffers.
6. **Serve.** DeviceWise connects to the virtual CPU and reads.

## 6. Key design decisions

### 6.1 The whitelist is enforced at registration, not at query time

`snap7.server.Server` does not auto-populate a block list; whatever the gateway
does not `registerArea()` simply does not exist on the virtual CPU. That makes
registration the natural enforcement point: an unexposed DB is **invisible** to
DeviceWise's enumeration, not present-but-empty. `tests/test_integration_bridge.py`
asserts exactly this - a real client sees `DBCount == 0` until an area is
whitelisted.

### 6.2 An area is never backed by anything but a current, successful read

If the newest good read for an area is older than `vplc_stale_timeout_seconds`,
the sync task **unregisters** it. A DeviceWise read then fails cleanly rather
than returning stale bytes. This is why the registration table is recomputed
every tick instead of being set up once.

### 6.3 Shrunk blocks: the tie-break

Section 4.7 of the brief says a shrunk or vanished DB must be *flagged*, not
silently deregistered, because a live consumer may still be querying the old
shape. Section 6 says never to serve bytes that are not backed by a real read.
Those pull in opposite directions when a DB shrinks.

Resolution:

* the **registration keeps its previous size**, so DeviceWise's tag shapes stay
  valid;
* the prefix the PLC still provides is refreshed from live data every tick;
* the remainder is **explicit zeros**, never left-over bytes;
* the area is flagged `shrunk` in Tag Mapping with the old and new sizes, and an
  operator presses **Confirm new size** to let it re-register smaller.

A vanished DB is flagged `missing` and left registered until an operator removes
it. A *grown* DB is re-registered at the larger size, since existing offsets
remain valid.

### 6.4 Buffer ownership on the server role

`python-snap7` 3.x implements `snap7.server.Server` in pure Python. Its
`register_area` keeps a `bytearray` **by reference** but copies any other buffer
type - a `ctypes` array registered once would never reflect later writes. The
gateway therefore allocates each area as a `bytearray`, verifies at registration
time that the server kept the reference, and falls back to re-registering on
every update if a future build copies it instead. Writes are bracketed with
`lock_area`/`unlock_area`, the same lock the server's read path takes, so a
DeviceWise read can never observe a half-updated payload.

### 6.5 Which areas are polled

An area is polled when it is exposed to DeviceWise (its buffer must stay
current) or when a named tag points into it (an operator is watching it). Areas
that are neither are skipped, so the gateway does not spend PLC bandwidth on
data nobody consumes. They can still be read ad hoc with the Test Read tool.

### 6.6 Hot reload

Saving a connection calls `ConnectionManager.apply_config()`, which reconciles
running workers against the database. Only connections whose *transport* fields
changed (address, rack, slot, port, type, timeout, enabled, poll interval) are
torn down and restarted; everything else is updated in place. Editing one PLC
never interrupts the others, and no service restart is needed. Web bind address,
port and TLS paths are the documented exception and say so in the UI.

## 7. Security model

| Concern | Mechanism |
| --- | --- |
| Transport | HTTPS by default; self-signed certificate generated on first run, operator can supply their own |
| Password storage | Argon2id (64 MiB, t=3, p=4), never plaintext or reversible |
| First run | Random 20-character admin password logged **once**; no page is reachable until it is changed |
| Password strength | Configurable policy, minimum 12 characters (hard floor), blocklist, no reuse of current/previous, must not contain the username; every rejection states its reason |
| Brute force | Per-account lockout plus a per-source-IP sliding-window rate limit in front of it; uniform timing for unknown usernames |
| Sessions | 256-bit token, only its SHA-256 stored; HttpOnly, SameSite=Lax, Secure over HTTPS; idle **and** absolute expiry; invalidated on logout, password change, disable and delete |
| CSRF | Per-session token on every state-changing POST; double-submit cookie for the login form |
| Input | Every field validated server-side in `core/validation.py`; unknown settings keys are ignored, never written |
| PLC writes | Off by default, per connection |
| Roles | `admin` (full config) and `viewer` (read-only); the last enabled admin cannot be demoted, disabled or deleted |
| Headers | CSP, `X-Frame-Options: DENY`, `nosniff`, `no-store`, HSTS over HTTPS |
| Audit | Every configuration change recorded with who, what, when and source IP |
| Secrets in logs | Never - the single exception is the first-run password, once, by design |

## 8. Failure handling

* **PLC disconnect** - that worker backs off and reconnects; other connections
  are untouched; its areas go stale and are withdrawn from the virtual CPU.
* **Malformed read** - raised as `PlcError`, logged, recorded against the area;
  three consecutive fully-failed cycles force a reconnect.
* **Virtual CPU cannot bind** (typically port 102 without privileges) - reported
  on the Device page and logged; the rest of the gateway keeps running so the
  operator can fix it through the UI.
* **Unhandled exception** anywhere (main thread, worker thread, asyncio task,
  web request) - a crash snapshot is written to `<data>/crash/` with the
  traceback, the last N log lines, every connection's state, the virtual CPU's
  registration table and a full thread dump.
* **Unclean exit** (power loss, SIGKILL) - a `running.marker` file left behind
  is detected at next boot, a snapshot is written, and the System Status page
  says so.
