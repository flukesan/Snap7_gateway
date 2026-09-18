"""Composition root: wires every subsystem into one supervised runtime.

Startup order matters and is fixed here:

1. paths and logging (so everything after this is diagnosable),
2. crash handlers plus the unclean-shutdown check from the previous run,
3. the SQLite store and the first-run admin account,
4. the virtual S7 CPU (server role),
5. the polling workers (client role),
6. the sync task that bridges the two.

Shutdown reverses it, and every step is individually guarded: a subsystem that
fails to stop is logged and the rest of the shutdown continues, because a
service that cannot exit is worse than one that exits noisily.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..auth.service import AuthService
from ..db.store import Database
from ..logging_.crash import CrashReporter, install_asyncio_handler, install_crash_handlers
from ..logging_.ringbuffer import LogRingBuffer
from ..logging_.setup import configure_logging
from ..paths import DataPaths
from ..virtual_plc.server import VirtualPlcError, VirtualPlcServer
from ..virtual_plc.sync import AreaSyncTask
from .datastore import DataStore
from .manager import ConnectionManager

logger = logging.getLogger(__name__)


class GatewayRuntime:
    """Owns every long-lived object in the process."""

    def __init__(self, data_dir: str | Path | None = None, *, console_logging: bool = True) -> None:
        self.paths: DataPaths = DataPaths.resolve(data_dir).ensure()
        self.started_at: float | None = None
        self.bootstrap_password: str | None = None
        self._console_logging = console_logging

        self.log_buffer: LogRingBuffer = configure_logging(
            self.paths.log_file, console=console_logging, force=True
        )
        self.db = Database(self.paths.db_file)
        self._apply_logging_settings()

        self.crash = CrashReporter(
            self.paths.crash_dir,
            self.log_buffer,
            self.paths.runtime_marker,
            log_lines=self.db.get_int("crash_log_lines", 500),
        )
        install_crash_handlers(self.crash)
        self.previous_unclean_shutdown = self.crash.check_previous_shutdown()
        if self.previous_unclean_shutdown:
            logger.error(
                "previous run did not shut down cleanly; a crash snapshot was written to %s",
                self.paths.crash_dir,
            )

        self.auth = AuthService(self.db, data_dir=self.paths.root)
        self.store = DataStore()
        self.vplc = VirtualPlcServer(
            bind_ip=self.db.get_setting("vplc_bind_ip", "0.0.0.0"),
            port=self.db.get_int("vplc_port", 102),
            max_clients=self.db.get_int("vplc_max_clients", 64),
            rack=self.db.get_int("vplc_rack", 0),
            slot=self.db.get_int("vplc_slot", 2),
        )
        self.sync = AreaSyncTask(
            self.db,
            self.store,
            self.vplc,
            interval_ms=self.db.get_int("vplc_sync_interval_ms", 250),
            stale_timeout_seconds=self.db.get_int("vplc_stale_timeout_seconds", 30),
        )
        self.connections = ConnectionManager(
            self.db, self.store, on_topology_change=self._on_topology_change
        )

        self.crash.register_state_provider("connections", self.connections.state_for_crash_snapshot)
        self.crash.register_state_provider("virtual_plc", self.vplc.status)
        self.crash.register_state_provider("sync", self.sync.status)
        self.crash.register_state_provider("datastore", self.store.snapshot_summary)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start every subsystem. Never raises for a recoverable failure."""
        self.started_at = time.time()
        self.crash.mark_running()
        try:
            install_asyncio_handler(asyncio.get_running_loop(), self.crash)
        except RuntimeError:  # pragma: no cover - no running loop (tests)
            pass

        self.bootstrap_password = self.auth.ensure_bootstrap_admin()
        if self.bootstrap_password:
            self._announce_bootstrap_credentials(self.bootstrap_password)

        self.auth.sessions.purge_expired()
        self.start_virtual_plc()
        await self.sync.start()
        await self.connections.start()
        logger.info("gateway runtime started (data dir: %s)", self.paths.root)

    async def stop(self) -> None:
        """Stop everything in reverse order; each step is independently guarded."""
        logger.info("gateway runtime stopping")
        for label, coro in (
            ("polling workers", self.connections.stop()),
            ("virtual PLC sync", self.sync.stop()),
        ):
            try:
                await coro
            except Exception:  # noqa: BLE001
                logger.exception("error stopping %s", label)
        try:
            self.vplc.stop()
        except Exception:  # noqa: BLE001
            logger.exception("error stopping the virtual PLC")
        try:
            self.db.prune_audit()
            self.db.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing the database")
        self.crash.mark_stopped()
        logger.info("gateway runtime stopped")

    # ------------------------------------------------------------------
    def start_virtual_plc(self) -> bool:
        """(Re)start the virtual CPU from current settings. Returns success."""
        if not self.db.get_bool("vplc_enabled", True):
            logger.info("virtual S7 CPU is disabled in settings")
            return False
        self.vplc.bind_ip = self.db.get_setting("vplc_bind_ip", "0.0.0.0")
        self.vplc.port = self.db.get_int("vplc_port", 102)
        self.vplc.max_clients = self.db.get_int("vplc_max_clients", 64)
        self.vplc.rack = self.db.get_int("vplc_rack", 0)
        self.vplc.slot = self.db.get_int("vplc_slot", 2)
        try:
            self.vplc.start()
            return True
        except VirtualPlcError as exc:
            # Reported, not fatal: the web UI is how the operator fixes this.
            logger.error("virtual S7 CPU could not start: %s", exc)
            return False

    async def apply_settings(self) -> None:
        """Re-read settings after the operator saves them (hot reload)."""
        self._apply_logging_settings()
        self.auth.reload_limits()
        self.crash.log_lines = self.db.get_int("crash_log_lines", 500)

        self.sync.interval_ms = max(self.db.get_int("vplc_sync_interval_ms", 250), 50)
        self.sync.stale_timeout_seconds = max(
            float(self.db.get_int("vplc_stale_timeout_seconds", 30)), 1.0
        )

        wants_server = self.db.get_bool("vplc_enabled", True)
        changed = (
            self.vplc.bind_ip != self.db.get_setting("vplc_bind_ip", "0.0.0.0")
            or self.vplc.port != self.db.get_int("vplc_port", 102)
            or self.vplc.max_clients != self.db.get_int("vplc_max_clients", 64)
        )
        if self.vplc.running and (not wants_server or changed):
            self.vplc.stop()
        if wants_server and not self.vplc.running:
            self.start_virtual_plc()

        await self.connections.apply_config()
        self.sync.request_refresh()

    def _apply_logging_settings(self) -> None:
        from ..logging_.setup import set_level

        set_level(self.db.get_setting("log_level", "INFO"))

    def _on_topology_change(self, connection_id: int) -> None:
        """A rescan changed the area map - refresh registrations promptly."""
        logger.info("topology change on connection %s, refreshing registrations", connection_id)
        self.sync.request_refresh()

    def _announce_bootstrap_credentials(self, password: str) -> None:
        """Log the generated admin password exactly once, at first boot only.

        This is the single place in the codebase permitted to write a
        credential to the log, and it happens once per installation. The account
        cannot be used for anything until the password is changed.
        """
        banner = "=" * 72
        logger.warning(
            "\n%s\nFIRST RUN: a default administrator account has been created.\n"
            "  username: admin\n  password: %s\n"
            "This password is shown ONCE and must be changed at first login "
            "before any other action is possible.\n%s",
            banner,
            password,
            banner,
        )

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0

    def status(self) -> dict[str, Any]:
        """Aggregate status for the web UI and the JSON status endpoint."""
        crash_files = self.crash.list_snapshots()
        return {
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "data_dir": str(self.paths.root),
            "connections": [h.as_dict() for h in self.store.all_health()],
            "virtual_plc": self.vplc.status(),
            "sync": self.sync.status(),
            "areas": self.store.snapshot_summary(),
            "previous_unclean_shutdown": self.previous_unclean_shutdown,
            "crash_snapshots": [
                {"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
                for p in crash_files[:20]
            ],
            "crash_snapshot_count": len(crash_files),
        }
