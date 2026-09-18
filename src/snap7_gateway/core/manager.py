"""Connection pool supervision and hot reload (CLAUDE.md section 3.1).

The manager owns one :class:`~snap7_gateway.core.worker.ConnectionWorker` per
configured PLC and reconciles that set against the database whenever the
operator saves a change in the web UI. Only connections whose transport-level
settings actually changed are torn down; everything else is updated in place,
so editing one PLC never interrupts the others and a full service restart is
not required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable

from ..db.models import PlcConnection
from ..db.store import Database
from .datastore import ConnectionHealth, DataStore
from .worker import ConnectionWorker

logger = logging.getLogger(__name__)


def _restart_signature(connection: PlcConnection) -> tuple[object, ...]:
    """Settings that cannot be changed without re-opening the S7 connection."""
    return (*connection.identity(), connection.poll_interval_ms)


class ConnectionManager:
    """Starts, stops and reconciles the per-connection workers."""

    def __init__(
        self,
        db: Database,
        store: DataStore,
        *,
        on_topology_change: Callable[[int], None] | None = None,
    ) -> None:
        self.db = db
        self.store = store
        self._on_topology_change = on_topology_change
        self._workers: dict[int, ConnectionWorker] = {}
        self._signatures: dict[int, tuple[object, ...]] = {}
        self._lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------
    @property
    def workers(self) -> dict[int, ConnectionWorker]:
        return dict(self._workers)

    def worker(self, connection_id: int) -> ConnectionWorker | None:
        return self._workers.get(connection_id)

    def health(self) -> list[ConnectionHealth]:
        return self.store.all_health()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Bring up a worker for every configured connection."""
        self._started = True
        await self.apply_config()

    async def stop(self) -> None:
        """Stop every worker. Individual failures never block the shutdown."""
        self._started = False
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            self._signatures.clear()
        results = await asyncio.gather(
            *(w.stop() for w in workers), return_exceptions=True
        )
        for worker, outcome in zip(workers, results):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "worker for connection %s failed to stop cleanly: %s",
                    worker.connection.id,
                    outcome,
                )

    async def apply_config(self) -> None:
        """Reconcile running workers against the current database state.

        Safe to call at any time; the web layer calls it after every connection
        create/update/delete so changes take effect without a restart.
        """
        if not self._started:
            return
        async with self._lock:
            desired = {c.id: c for c in self.db.list_connections()}
            discovery_max = self.db.get_int("discovery_max_db_count", 2048)
            discovery_interval = self.db.get_int("discovery_interval_minutes", 0) * 60

            # Removed connections
            for connection_id in [cid for cid in self._workers if cid not in desired]:
                await self._stop_worker(connection_id)
                self.store.drop_connection(connection_id)

            for connection_id, connection in desired.items():
                worker = self._workers.get(connection_id)
                signature = _restart_signature(connection)

                if worker is None:
                    if connection.enabled:
                        await self._start_worker(connection, discovery_max, discovery_interval)
                    continue

                if not connection.enabled:
                    await self._stop_worker(connection_id)
                    continue

                if self._signatures.get(connection_id) != signature:
                    logger.info(
                        "connection %s (%s) transport settings changed, restarting worker",
                        connection_id,
                        connection.name,
                    )
                    await self._stop_worker(connection_id)
                    await self._start_worker(connection, discovery_max, discovery_interval)
                    continue

                # Nothing transport-level changed: update in place so the live
                # link is not interrupted.
                worker.connection = connection
                worker.discovery_max_db_count = discovery_max
                worker.discovery_interval_seconds = discovery_interval

    async def _start_worker(
        self, connection: PlcConnection, discovery_max: int, discovery_interval: float
    ) -> ConnectionWorker:
        worker = ConnectionWorker(
            connection,
            self.db,
            self.store,
            discovery_max_db_count=discovery_max,
            discovery_interval_seconds=discovery_interval,
            on_topology_change=self._on_topology_change,
        )
        self._workers[connection.id] = worker
        self._signatures[connection.id] = _restart_signature(connection)
        await worker.start()
        logger.info("started polling worker for connection %s (%s)", connection.id, connection.name)
        return worker

    async def _stop_worker(self, connection_id: int) -> None:
        worker = self._workers.pop(connection_id, None)
        self._signatures.pop(connection_id, None)
        if worker is None:
            return
        try:
            await worker.stop()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("error stopping worker for connection %s", connection_id)
        logger.info("stopped polling worker for connection %s", connection_id)

    # ------------------------------------------------------------------
    def request_rescan(self, connection_id: int | None = None) -> list[int]:
        """Queue a block rescan for one connection, or all of them.

        Returns the connection ids that were signalled. Connections without a
        running worker (disabled, or never connected) are skipped.
        """
        targets: Iterable[ConnectionWorker]
        if connection_id is None:
            targets = list(self._workers.values())
        else:
            worker = self._workers.get(connection_id)
            targets = [worker] if worker else []
        signalled = []
        for worker in targets:
            worker.request_rescan()
            signalled.append(worker.connection.id)
        return signalled

    def state_for_crash_snapshot(self) -> list[dict[str, object]]:
        """Connection states embedded in crash snapshots (CLAUDE.md 4.8)."""
        return [h.as_dict() for h in self.store.all_health()]
