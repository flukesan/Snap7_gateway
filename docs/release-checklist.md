# Pre-release verification checklist

CLAUDE.md section 4.2 requires that, before a release, the service is verified
to start, survive a forced kill and auto-restart on **both** platforms. There is
no CI runner for Windows or for real PLC hardware in this project, so this is the
manual checklist that stands in for it. Record the result (date, version, host,
pass/fail) alongside the release.

## 0. Automated suite (any platform)

```bash
pytest tests/ -q
```

Expected: all tests pass. This covers both directions of the bridge against a
Snap7 server simulator, but it does **not** cover service supervision, real
hardware, or privileged port binding — that is what the rest of this list is
for.

## 1. Linux (systemd)

| # | Step | Expected |
| --- | --- | --- |
| 1.1 | `sudo ./install/linux/install.sh` | Completes; unit enabled |
| 1.2 | `systemctl is-active snap7-gateway` | `active` |
| 1.3 | `journalctl -u snap7-gateway \| grep -A4 "FIRST RUN"` | The one-time admin password appears exactly once |
| 1.4 | Open `https://<host>:8443/` | Login page over TLS (self-signed warning is expected) |
| 1.5 | Sign in, then try `/connections` | Redirected to the password-change page |
| 1.6 | Change the password, sign in again | Full UI reachable; the password appears nowhere in the log |
| 1.7 | `ss -lntp \| grep :102` | The gateway is listening (proves `CAP_NET_BIND_SERVICE` works without root) |
| 1.8 | `sudo kill -9 $(systemctl show -p MainPID --value snap7-gateway)` | Service restarts within ~5 s (`systemctl status` shows a new PID) |
| 1.9 | Reload the UI | Works; **System Status** reports that the previous run did not shut down cleanly and lists a new crash snapshot |
| 1.10 | `sudo systemctl restart snap7-gateway` | Clean restart; System Status reports **no** unclean shutdown this time |
| 1.11 | `sudo reboot` | Service comes back automatically |
| 1.12 | `ls -l /var/lib/snap7-gateway/gateway.db /var/lib/snap7-gateway/certs/server.key` | Modes `0600`; owned by `snap7gw` |

## 2. Windows (native service)

| # | Step | Expected |
| --- | --- | --- |
| 2.1 | `.\install\windows\install-service.ps1` (elevated) | Completes; service registered |
| 2.2 | `sc query Snap7Gateway` | `RUNNING` |
| 2.3 | `%ProgramData%\Snap7Gateway\logs\gateway.log` | Contains the one-time admin password |
| 2.4 | Open `https://<host>:8443/` | Login page; first-login change enforced as in 1.5–1.6 |
| 2.5 | `netstat -ano \| findstr :102` | The gateway is listening |
| 2.6 | `taskkill /F /PID <service PID>` | Service restarts within ~5 s (`sc qc Snap7Gateway` shows the failure actions) |
| 2.7 | Reload the UI | Works; System Status reports the unclean shutdown and a new crash snapshot |
| 2.8 | `Restart-Computer` | Service starts automatically |
| 2.9 | `sc stop Snap7Gateway` then start | Clean stop; System Status reports no unclean shutdown |

## 3. PLC and DeviceWise (one host is enough)

| # | Step | Expected |
| --- | --- | --- |
| 3.1 | Add a connection to a real PLC, press **Test Connection** | Success with a latency figure and the CPU type; a wrong rack/slot gives the real Snap7 error text, not a generic failure |
| 3.2 | Wait one poll cycle, open **Tag Mapping** | Discovered DBs listed with sizes matching the PLC program |
| 3.3 | **Test Read** a known address | Value matches what TIA Portal / the HMI shows |
| 3.4 | Expose one DB, leave another unexposed | Only the exposed one shows *Registered = Yes* |
| 3.5 | Point the DeviceWise Siemens device at the gateway (address only) | It connects with `Connection: Direct` unchanged |
| 3.6 | Run DeviceWise's enumeration | The exposed DB appears at its real size; the unexposed one does not appear at all |
| 3.7 | Change a value in the PLC | DeviceWise sees it within (poll interval + sync interval) |
| 3.8 | Unplug the PLC network cable | Within the stale timeout, DeviceWise reads fail cleanly; the connection shows *Reconnecting*; other connections keep polling |
| 3.9 | Plug it back in | The connection recovers on its own and the area is re-registered; no restart needed |
| 3.10 | Add a DB to the PLC program, press **Rescan** | Mirror All: registered automatically. Whitelist: listed as **New**, unexposed, until you tick it |

## 4. Multi-connection isolation

| # | Step | Expected |
| --- | --- | --- |
| 4.1 | Configure one reachable PLC and one unreachable address | The healthy connection keeps polling at its normal rate |
| 4.2 | System Status | The dead one shows *Reconnecting* / *Failed* with a real error; the healthy one shows recent successful reads |
| 4.3 | Watch process memory for an hour | Stable — the datastore holds one entry per configured area, replaced in place |

## 5. Security spot checks

| # | Step | Expected |
| --- | --- | --- |
| 5.1 | `grep -ri "password" <data-dir>/logs/` | Only the one first-run line; no password on any later line |
| 5.2 | POST to any form route without `csrf_token` | Refused; nothing changes |
| 5.3 | Fail the configured number of sign-ins | Account locks for the configured cool-down |
| 5.4 | Sign in as a `viewer` | Read-only pages work; every configuration action is refused |
| 5.5 | Try to demote or delete the last admin | Refused |
| 5.6 | `GET /crash/..%2F..%2Fgateway.db` | Refused |
| 5.7 | Check the audit trail after the steps above | Every change recorded with user, action, target and source IP |
