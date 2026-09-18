"""Background sync: live poll data -> virtual CPU buffers (CLAUDE.md 4.6).

One asyncio task, running on a fixed interval, that answers a single question
each tick: *which areas should the virtual CPU be publishing right now, and with
what bytes?*

The two rules it enforces are non-negotiable:

* **Exposure is decided at registration time.** An area that is not whitelisted
  (or not covered by Mirror All) is never registered, so DeviceWise's own
  enumeration cannot even see it.
* **No area is ever backed by anything but a current, successful read.** If the
  newest good read for an area is older than the stale timeout, the area is
  *unregistered*. A DeviceWise read then fails cleanly instead of returning
  stale or garbage bytes (CLAUDE.md section 6).

One deliberate tie-break: when a DB shrinks on the real PLC, section 4.7 says
not to silently change what a live consumer sees, while section 6 says never to
serve bytes that are not backed by a real read. The gateway keeps the
*registration* at its previous size so DeviceWise's tag shapes stay valid,
refreshes the prefix the PLC still provides, zero-fills the remainder, and
flags the area as ``shrunk`` in Tag Mapping until an operator confirms the new
shape. The tail is therefore explicit zeros, never leftover data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.datastore import AreaKey, DataStore
from ..db.models import AreaStatus, ExposureMode
from ..db.store import Database
from .server import VirtualPlcError, VirtualPlcServer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlannedArea:
    """An area the virtual CPU should publish this tick."""

    key: AreaKey
    size: int
    data: bytes
    shape_locked: bool = False


@dataclass(slots=True)
class SyncStats:
    """Counters surfaced on the System Status page."""

    last_run_at: float | None = None
    runs: int = 0
    registered: int = 0
    unregistered: int = 0
    updates: int = 0
    errors: int = 0
    last_error: str | None = None
    skipped_stale: list[str] = field(default_factory=list)
    skipped_unexposed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at,
            "runs": self.runs,
            "registered": self.registered,
            "unregistered": self.unregistered,
            "updates": self.updates,
            "errors": self.errors,
            "last_error": self.last_error,
            "skipped_stale": list(self.skipped_stale),
            "skipped_unexposed": self.skipped_unexposed,
        }


class AreaSyncTask:
    """Keeps the virtual CPU's registered areas and buffers current."""

    def __init__(
        self,
        db: Database,
        store: DataStore,
        server: VirtualPlcServer,
        *,
        interval_ms: int = 250,
        stale_timeout_seconds: float = 30.0,
    ) -> None:
        self.db = db
        self.store = store
        self.server = server
        self.interval_ms = max(int(interval_ms), 50)
        self.stale_timeout_seconds = max(float(stale_timeout_seconds), 1.0)
        self.stats = SyncStats()

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._supervise(), name="vplc-sync")

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None

    def request_refresh(self) -> None:
        """Run a tick immediately (after a rescan or an exposure change)."""
        self._wake.set()

    # ------------------------------------------------------------------
    async def _supervise(self) -> None:
        """Never propagates: a sync failure must not take down the gateway."""
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.to_thread(self.sync_once)
                except Exception as exc:  # noqa: BLE001
                    self.stats.errors += 1
                    self.stats.last_error = str(exc)
                    logger.exception("virtual PLC sync tick failed")
                await self._sleep(self.interval_ms / 1000.0)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001
            logger.exception("virtual PLC sync task terminated unexpectedly")

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        stop_wait = asyncio.create_task(self._stop.wait())
        wake_wait = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_wait, wake_wait}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (stop_wait, wake_wait):
                task.cancel()

    # ------------------------------------------------------------------
    def sync_once(self) -> SyncStats:
        """Run one reconciliation pass. Synchronous and directly testable."""
        if not self.server.running:
            return self.stats

        plan, unexposed = self.build_plan()
        planned_keys = {p.key for p in plan}

        # 1. Withdraw anything that must no longer be published.
        for registration in self.server.registrations():
            if registration.key not in planned_keys:
                if self.server.unregister(
                    registration.key, reason="not exposed or no current read"
                ):
                    self.stats.unregistered += 1

        # 2. Register/refresh what should be published.
        for entry in plan:
            try:
                if self.server.registered_size(entry.key) != entry.size:
                    self.server.register(entry.key, entry.size, initial=entry.data)
                    self.stats.registered += 1
                if self.server.update(entry.key, entry.data):
                    self.stats.updates += 1
            except VirtualPlcError as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                logger.warning("could not publish %s: %s", entry.key, exc)

        self.stats.runs += 1
        self.stats.last_run_at = time.time()
        self.stats.skipped_unexposed = unexposed
        return self.stats

    def build_plan(self) -> tuple[list[PlannedArea], int]:
        """Decide what the virtual CPU should publish right now.

        Returns the planned areas plus a count of areas skipped because they are
        not exposed. Pure with respect to the server - it only reads config and
        the datastore - so the decision can be unit-tested without a live CPU.
        """
        plan: list[PlannedArea] = []
        stale: list[str] = []
        unexposed = 0

        for connection in self.db.list_connections(enabled_only=True):
            mirror_all = connection.exposure_mode == ExposureMode.MIRROR_ALL
            for area in self.db.list_areas(connection.id):
                key = AreaKey(connection.id, str(area.area_type), area.db_number)

                if not (mirror_all or area.exposed):
                    unexposed += 1
                    continue
                if area.size_bytes <= 0:
                    continue

                snapshot = self.store.fresh(key, self.stale_timeout_seconds)
                if snapshot is None:
                    # No successful, current read -> never registered, so a
                    # DeviceWise read is a clean fault instead of stale bytes.
                    stale.append(str(key))
                    continue

                size = area.size_bytes
                shape_locked = False
                if area.status == AreaStatus.SHRUNK:
                    already = self.server.registered_size(key)
                    if already > size:
                        size = already
                        shape_locked = True

                data = snapshot.data
                if len(data) < size:
                    data = data.ljust(size, b"\x00")
                elif len(data) > size:
                    data = data[:size]

                plan.append(PlannedArea(key=key, size=size, data=data, shape_locked=shape_locked))

        self.stats.skipped_stale = stale[:50]
        return plan, unexposed

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_ms": self.interval_ms,
            "stale_timeout_seconds": self.stale_timeout_seconds,
            **self.stats.as_dict(),
        }
