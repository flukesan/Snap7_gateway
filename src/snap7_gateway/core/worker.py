"""Supervised per-connection polling worker (CLAUDE.md sections 3.1 and 4.1).

Each configured PLC gets exactly one :class:`ConnectionWorker`, and each worker
owns:

* one :class:`~snap7_gateway.plc.client.PlcClient`,
* one single-threaded executor that every blocking Snap7 call is dispatched to.

Pinning a client to one thread is a hard requirement - a Snap7 ``S7Object``
handle is not safe to use concurrently - and it also guarantees that a PLC that
stops answering blocks only its own thread, never the event loop and never
another connection's polling.

The supervision contract: the worker's task never propagates an exception. Any
failure is logged, recorded in :class:`~snap7_gateway.core.datastore.ConnectionHealth`
and retried with exponential backoff, so a single disconnected PLC can never
take the process down.
"""

from __future__ import annotations

import asyncio
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Callable, Sequence

from ..db.models import AreaStatus, PlcArea, PlcConnection
from ..db.store import Database
from ..logging_.setup import connection_logger
from ..plc.client import PlcClient, PlcConnectionParams, PlcError
from ..plc.discovery import DiscoveryDiff, apply_discovery, discover
from .datastore import AreaKey, ConnectionHealth, ConnectionState, DataStore

#: Reconnect backoff: 1s, 2s, 4s ... capped, with jitter so a site-wide network
#: blip does not resynchronise every gateway into a thundering herd.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_SECONDS = 60.0
BACKOFF_JITTER = 0.2

#: Consecutive failed reads before the connection is torn down and re-opened.
#: A single malformed read is tolerated; a run of them means the link is gone.
READ_FAILURES_BEFORE_RECONNECT = 3

#: Sentinel distinguishing "leave the error text as it is" from "clear it".
_UNSET = object()


class ConnectionWorker:
    """Owns the lifecycle of one PLC link: connect, discover, poll, recover."""

    def __init__(
        self,
        connection: PlcConnection,
        db: Database,
        store: DataStore,
        *,
        discovery_max_db_count: int = 2048,
        discovery_interval_seconds: float = 0.0,
        on_topology_change: Callable[[int], None] | None = None,
    ) -> None:
        self.connection = connection
        self.db = db
        self.store = store
        self.discovery_max_db_count = discovery_max_db_count
        self.discovery_interval_seconds = discovery_interval_seconds
        self._on_topology_change = on_topology_change

        self.log = connection_logger("snap7_gateway.core.worker", connection.id)
        self._client = PlcClient(PlcConnectionParams.from_model(connection), logger_=self.log)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"plc-{connection.id}"
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._rescan = asyncio.Event()
        self._last_discovery_at = 0.0
        self._health = ConnectionHealth(
            connection_id=connection.id,
            name=connection.name,
            state=ConnectionState.STOPPED if connection.enabled else ConnectionState.DISABLED,
        )
        self._publish_health()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._supervise(), name=f"plc-worker-{self.connection.id}")

    async def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop to exit and wait for the thread to drain."""
        self._stop.set()
        self._rescan.set()  # wake an idle wait immediately
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.log.warning("worker did not stop within %.0fs, cancelling", timeout)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        # Closing the client on its own thread avoids touching the handle from
        # the event loop.
        try:
            await self._call(self._client.disconnect)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._set_state(ConnectionState.STOPPED)

    def request_rescan(self) -> None:
        """Ask the loop to re-run block discovery at the next opportunity."""
        self._rescan.set()

    # ------------------------------------------------------------------
    # supervision
    # ------------------------------------------------------------------
    async def _supervise(self) -> None:
        """Outermost guard: this coroutine must never raise."""
        try:
            await self._run()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - last line of defence
            self.log.exception("polling loop terminated unexpectedly: %s", exc)
            self._set_state(ConnectionState.FAILED, error=f"internal error: {exc}")

    async def _run(self) -> None:
        if not self.connection.enabled:
            self._set_state(ConnectionState.DISABLED)
            return

        attempt = 0
        while not self._stop.is_set():
            self._set_state(
                ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RECONNECTING
            )
            self._health.last_attempt_at = time.time()
            try:
                await self._call(self._client.connect)
            except PlcError as exc:
                attempt += 1
                delay = self._backoff(attempt)
                self._health.consecutive_failures = attempt
                self._health.next_retry_at = time.time() + delay
                self._set_state(
                    ConnectionState.RECONNECTING if attempt < 5 else ConnectionState.FAILED,
                    error=exc.detail,
                )
                self.log.warning(
                    "connect failed (attempt %d), retrying in %.1fs: %s", attempt, delay, exc.detail
                )
                if await self._sleep(delay):
                    break
                continue

            attempt = 0
            now = time.time()
            self._health.consecutive_failures = 0
            self._health.connected_since = now
            self._health.next_retry_at = None
            self._set_state(ConnectionState.CONNECTED, error=None)

            try:
                identity = await self._call(self._client.cpu_identity)
                self._health.cpu = identity.as_dict()
                self._publish_health()
            except PlcError as exc:
                self.log.debug("CPU identity unavailable: %s", exc.detail)

            await self._run_discovery(reason="connect")
            await self._poll_until_disconnected()

            await self._call_safe(self._client.disconnect)
            if not self._stop.is_set():
                self._set_state(ConnectionState.RECONNECTING)

        await self._call_safe(self._client.disconnect)
        self._set_state(ConnectionState.STOPPED)

    async def _poll_until_disconnected(self) -> None:
        """Poll on the configured interval until the link fails or we stop."""
        interval = max(self.connection.poll_interval_ms, 50) / 1000.0
        read_failures = 0

        while not self._stop.is_set():
            if self._rescan.is_set():
                self._rescan.clear()
                if not self._stop.is_set():
                    await self._run_discovery(reason="rescan")
            elif (
                self.discovery_interval_seconds > 0
                and time.time() - self._last_discovery_at >= self.discovery_interval_seconds
            ):
                await self._run_discovery(reason="interval")

            started = time.perf_counter()
            areas = self._areas_to_poll()
            self._health.polled_areas = len(areas)
            failed_this_cycle = 0

            for area in areas:
                if self._stop.is_set():
                    return
                key = AreaKey(self.connection.id, str(area.area_type), area.db_number)
                read_started = time.perf_counter()
                try:
                    data = await self._call(
                        self._client.read_area,
                        str(area.area_type),
                        area.db_number,
                        0,
                        area.size_bytes,
                    )
                except PlcError as exc:
                    failed_this_cycle += 1
                    self._health.total_errors += 1
                    self.store.publish_failure(key, exc.detail)
                    self.log.warning("read %s failed: %s", key.label, exc.detail)
                    self._health.last_error = exc.detail
                    continue
                except Exception as exc:  # noqa: BLE001 - never let a read kill the loop
                    failed_this_cycle += 1
                    self._health.total_errors += 1
                    self.store.publish_failure(key, f"unexpected error: {exc}")
                    self.log.exception("unexpected error reading %s", key.label)
                    continue

                self.store.publish(
                    key, data, duration_ms=(time.perf_counter() - read_started) * 1000.0
                )
                self._health.last_success_at = time.time()

            self._health.total_polls += 1
            self._health.last_poll_duration_ms = (time.perf_counter() - started) * 1000.0

            if areas and failed_this_cycle == len(areas):
                read_failures += 1
            else:
                read_failures = 0
                if failed_this_cycle == 0:
                    self._health.last_error = None
            self._publish_health()

            if read_failures >= READ_FAILURES_BEFORE_RECONNECT:
                self.log.warning(
                    "%d consecutive failed poll cycles, reconnecting", read_failures
                )
                self._set_state(ConnectionState.RECONNECTING, error=self._health.last_error)
                return

            if not await self._call_safe(self._client_is_connected):
                self.log.warning("link reported not connected, reconnecting")
                self._set_state(ConnectionState.RECONNECTING, error="link lost")
                return

            if await self._sleep(interval):
                return

    def _client_is_connected(self) -> bool:
        return self._client.connected

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    async def _run_discovery(self, *, reason: str) -> DiscoveryDiff | None:
        """Run block discovery and persist the diff. Never raises."""
        try:
            result = await self._call(
                discover,
                self._client,
                self.connection,
                max_db_count=self.discovery_max_db_count,
            )
        except PlcError as exc:
            self.log.warning("discovery (%s) failed: %s", reason, exc.detail)
            self._health.last_error = exc.detail
            self._publish_health()
            return None
        except Exception as exc:  # noqa: BLE001
            self.log.exception("discovery (%s) raised unexpectedly", reason)
            self._health.last_error = f"discovery error: {exc}"
            self._publish_health()
            return None

        self._last_discovery_at = time.time()
        diff = await asyncio.to_thread(
            apply_discovery, self.db, self.connection, result, actor=f"system:{reason}"
        )
        self._health.last_discovery_at = self._last_discovery_at
        self._health.last_discovery_summary = (
            f"{reason}: {diff.summary()}"
            + (f" ({len(result.errors)} warning(s))" if result.errors else "")
        )
        self._publish_health()

        # Drop cached snapshots for areas that no longer exist in config, so the
        # store stays bounded by configuration.
        configured = {
            AreaKey(self.connection.id, str(a.area_type), a.db_number)
            for a in self.db.list_areas(self.connection.id)
        }
        others = {k for k in self.store.keys() if k.connection_id != self.connection.id}
        self.store.retain(configured | others)

        if self._on_topology_change is not None and not diff.is_empty:
            try:
                self._on_topology_change(self.connection.id)
            except Exception:  # noqa: BLE001 - a listener must not break polling
                self.log.exception("topology change listener failed")
        return diff

    # ------------------------------------------------------------------
    # on-demand operations used by the web test tools
    # ------------------------------------------------------------------
    async def read_once(self, area_type: str, db_number: int, start: int, size: int) -> bytes:
        """Perform a single read on this worker's thread (Test Read tool).

        Queued behind any in-flight poll on the same executor, so it can never
        race the polling loop over the same Snap7 handle.
        """
        data = await self._call(self._client.read_area, str(area_type), db_number, start, size)
        return bytes(data)

    async def write_once(self, area_type: str, db_number: int, start: int, data: bytes) -> None:
        """Write raw bytes, if the connection is configured writable."""
        if not self.connection.write_enabled:
            raise PlcError(
                f"connection '{self.connection.name}' is read-only; enable writes on the "
                "connection first",
                operation="write",
            )
        await self._call(self._client.write_area, str(area_type), db_number, start, data)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _areas_to_poll(self) -> Sequence[PlcArea]:
        """Areas worth reading this cycle.

        An area is polled when it is exposed to DeviceWise (its buffer must stay
        current) or when a configured tag points into it (the operator is
        watching it in the UI). Areas that are neither are skipped so the gateway
        does not spend PLC bandwidth on data nobody consumes; an operator can
        always read them ad hoc with the Test Read tool.
        """
        areas = [
            a
            for a in self.db.list_areas(self.connection.id)
            if a.status != AreaStatus.MISSING and a.size_bytes > 0
        ]
        if not areas:
            return []
        referenced = {
            (str(t.area_type), t.db_number) for t in self.db.list_tags(self.connection.id)
        }
        return [
            a for a in areas if a.exposed or (str(a.area_type), a.db_number) in referenced
        ]

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking Snap7 call on this connection's dedicated thread."""
        loop = asyncio.get_running_loop()
        if kwargs:
            from functools import partial

            return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))
        return await loop.run_in_executor(self._executor, fn, *args)

    async def _call_safe(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return await self._call(fn, *args)
        except Exception:  # noqa: BLE001
            return None

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, returning True if a stop was requested during the wait."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(seconds, 0.0))
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _backoff(attempt: int) -> float:
        raw = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** max(0, attempt - 1))
        capped = min(raw, BACKOFF_MAX_SECONDS)
        jitter = 1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)
        return max(0.1, capped * jitter)

    def _set_state(self, state: str, error: str | None | object = _UNSET) -> None:
        """Record a state transition and publish the updated health snapshot.

        ``error`` is three-valued: omit it to leave the previous error text
        alone, pass ``None`` to clear it, or pass a string to replace it.
        """
        self._health.state = state
        if error is not _UNSET:
            self._health.last_error = error  # type: ignore[assignment]
        if state != ConnectionState.CONNECTED:
            self._health.connected_since = None
        self._publish_health()

    def _publish_health(self) -> None:
        self.store.set_health(replace(self._health))

    @property
    def health(self) -> ConnectionHealth:
        return replace(self._health)
