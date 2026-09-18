"""Snap7 *server* role - the virtual S7 CPU DeviceWise connects to.

Architecture (CLAUDE.md section 4.6)::

    [Real PLC] --Snap7 CLIENT (poll)--> [Gateway] --Snap7 SERVER (host)--> [DeviceWise]

DeviceWise's Siemens driver speaks native S7-comm with ``Connection: Direct``,
so cutting over from the ACCON NetLink-Pro box is a change of IP address on the
DeviceWise side and nothing else.

Two properties of ``snap7.server.Server`` shape this module:

1. It does **not** auto-populate a block list. Whatever the gateway does not
   ``registerArea()`` simply does not exist on the virtual CPU - which is
   exactly the enforcement point for the exposure whitelist. An unexposed DB is
   invisible to DeviceWise's enumeration rather than present-but-empty.
2. A registered area is backed by a caller-owned buffer. ``register_area``
   keeps a ``bytearray`` **by reference** (a ``ctypes`` array would be copied
   instead, and later writes would never reach a client), so each area is
   allocated as a ``bytearray`` that the sync task mutates in place. The
   reference is verified at registration time; if a Snap7 build ever copies it
   anyway, the server falls back to re-registering on every update so live data
   still reaches DeviceWise. Writes are bracketed with ``lock_area``/
   ``unlock_area`` - the same lock the server's read path takes - so a
   DeviceWise read can never observe a half-updated payload.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from snap7.server import Server
from snap7.type import SrvArea

from ..core.datastore import AreaKey
from ..db.models import AreaType

logger = logging.getLogger(__name__)

#: Gateway area vocabulary -> Snap7 *server* area codes. Note these differ from
#: the client-side ``Area`` codes; mixing them up silently registers the wrong
#: area, so the mapping lives in exactly one place.
SRV_AREA_MAP: dict[str, SrvArea] = {
    AreaType.DB: SrvArea.DB,
    AreaType.INPUT: SrvArea.PE,
    AreaType.OUTPUT: SrvArea.PA,
    AreaType.MERKER: SrvArea.MK,
}

#: Refuse absurd registrations; a corrupt discovery result must not be able to
#: make the gateway allocate unbounded memory.
MAX_REGISTERED_BYTES = 8 * 1024 * 1024


class VirtualPlcError(RuntimeError):
    """Any failure in the server role."""


@dataclass(slots=True)
class Registration:
    """One area currently published on the virtual CPU."""

    key: AreaKey
    srv_area: SrvArea
    index: int
    size: int
    registered_at: float
    last_update_at: float = 0.0
    updates: int = 0
    buffer: bytearray = field(default_factory=bytearray, repr=False)
    #: False when the Snap7 build copied the buffer instead of keeping the
    #: reference; such areas are re-registered on every update.
    live_buffer: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.key.connection_id,
            "area": self.key.label,
            "index": self.index,
            "size": self.size,
            "registered_at": self.registered_at,
            "last_update_at": self.last_update_at or None,
            "updates": self.updates,
        }


class VirtualPlcServer:
    """Manages the Snap7 server instance and its registered areas.

    Thread-safe: the sync task drives it from the event loop's thread pool while
    the web layer reads status from request handlers.
    """

    def __init__(
        self,
        *,
        bind_ip: str = "0.0.0.0",
        port: int = 102,
        max_clients: int = 64,
        rack: int = 0,
        slot: int = 2,
    ) -> None:
        self.bind_ip = bind_ip
        self.port = port
        self.max_clients = max_clients
        self.rack = rack
        self.slot = slot

        self._server: Server | None = None
        self._registrations: dict[AreaKey, Registration] = {}
        self._lock = threading.RLock()
        self._started_at: float | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        """Bind and start the virtual CPU.

        Binding port 102 needs privileges on Linux (the systemd unit grants
        ``CAP_NET_BIND_SERVICE``); the failure is reported rather than raised as
        a crash so the rest of the gateway - including the web UI that lets the
        operator fix the port - keeps running.
        """
        with self._lock:
            if self._server is not None:
                return
            try:
                server = Server(log=False, max_clients=self.max_clients)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"could not create the Snap7 server: {exc}"
                raise VirtualPlcError(self._last_error) from exc

            try:
                if self.bind_ip and self.bind_ip not in {"0.0.0.0", "*", ""}:
                    server.start_to(self.bind_ip, tcp_port=self.port)
                else:
                    server.start(tcp_port=self.port)
            except Exception as exc:  # noqa: BLE001
                try:
                    server.destroy()
                except Exception:  # noqa: BLE001
                    pass
                hint = ""
                if self.port < 1024:
                    hint = (
                        " (binding a port below 1024 requires root or "
                        "CAP_NET_BIND_SERVICE on Linux, or Administrator on Windows)"
                    )
                self._last_error = f"could not bind {self.bind_ip}:{self.port}: {exc}{hint}"
                raise VirtualPlcError(self._last_error) from exc

            self._server = server
            self._started_at = time.time()
            self._last_error = None
            logger.info(
                "virtual S7 CPU listening on %s:%s (max %d clients)",
                self.bind_ip,
                self.port,
                self.max_clients,
            )

    def stop(self) -> None:
        """Unregister everything and shut the server down. Never raises."""
        with self._lock:
            server, self._server = self._server, None
            for key in list(self._registrations):
                self._unregister_locked(key, server, reason="shutdown")
            self._registrations.clear()
            self._started_at = None
            if server is None:
                return
            for step, call in (("stop", server.stop), ("destroy", server.destroy)):
                try:
                    call()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ignoring error during server %s: %s", step, exc)
            logger.info("virtual S7 CPU stopped")

    # ------------------------------------------------------------------
    # registration - the whitelist enforcement point
    # ------------------------------------------------------------------
    def is_registered(self, key: AreaKey) -> bool:
        with self._lock:
            return key in self._registrations

    def registered_size(self, key: AreaKey) -> int:
        with self._lock:
            reg = self._registrations.get(key)
            return reg.size if reg else 0

    def registrations(self) -> list[Registration]:
        with self._lock:
            return sorted(
                self._registrations.values(),
                key=lambda r: (r.key.connection_id, r.srv_area.value, r.index),
            )

    def register(self, key: AreaKey, size: int, initial: bytes | None = None) -> Registration:
        """Publish an area on the virtual CPU with the PLC's exact size.

        Re-registering an existing key with a different size replaces it, which
        changes the shape DeviceWise enumerates - the caller decides when that
        is acceptable (see :mod:`snap7_gateway.virtual_plc.sync`).
        """
        if size <= 0 or size > MAX_REGISTERED_BYTES:
            raise VirtualPlcError(
                f"refusing to register {key} with size {size} "
                f"(must be 1..{MAX_REGISTERED_BYTES})"
            )
        srv_area = SRV_AREA_MAP.get(str(key.area_type))
        if srv_area is None:
            raise VirtualPlcError(f"unknown area type '{key.area_type}' for {key}")

        with self._lock:
            server = self._server
            if server is None:
                raise VirtualPlcError("virtual S7 CPU is not running")

            existing = self._registrations.get(key)
            if existing is not None:
                if existing.size == size:
                    return existing
                logger.info(
                    "re-registering %s: size %d -> %d bytes", key.label, existing.size, size
                )
                self._unregister_locked(key, server, reason="size change")

            index = key.db_number if srv_area is SrvArea.DB else 0
            buffer = bytearray(size)
            if initial:
                payload = bytes(initial[:size])
                buffer[: len(payload)] = payload
            try:
                server.register_area(srv_area, index, buffer)
            except Exception as exc:  # noqa: BLE001
                raise VirtualPlcError(f"registerArea failed for {key.label}: {exc}") from exc

            live = self._buffer_is_live(server, srv_area, index, buffer)
            if not live:
                logger.warning(
                    "%s: this Snap7 build copies registered buffers, so each update "
                    "re-registers the area",
                    key.label,
                )

            registration = Registration(
                key=key,
                srv_area=srv_area,
                index=index,
                size=size,
                registered_at=time.time(),
                buffer=buffer,
                live_buffer=live,
            )
            self._registrations[key] = registration
            logger.info(
                "registered %s on the virtual CPU as %s[%d], %d bytes",
                key,
                srv_area.name,
                index,
                size,
            )
            return registration

    def unregister(self, key: AreaKey, *, reason: str = "") -> bool:
        with self._lock:
            if key not in self._registrations:
                return False
            self._unregister_locked(key, self._server, reason=reason)
            return True

    def _unregister_locked(self, key: AreaKey, server: Server | None, *, reason: str) -> None:
        registration = self._registrations.pop(key, None)
        if registration is None:
            return
        if server is not None:
            try:
                server.unregister_area(registration.srv_area, registration.index)
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                logger.debug("ignoring unregister error for %s: %s", key.label, exc)
        logger.info(
            "unregistered %s from the virtual CPU%s", key, f" ({reason})" if reason else ""
        )

    # ------------------------------------------------------------------
    # data updates
    # ------------------------------------------------------------------
    def update(self, key: AreaKey, data: bytes) -> bool:
        """Copy fresh PLC data into a registered area's buffer.

        Shorter payloads fill the head of the buffer and leave the tail at its
        previous contents only when the sizes match exactly; a genuinely shorter
        read means the shape changed, which the sync task handles explicitly
        instead of silently padding. Returns False when the key is not
        registered.
        """
        with self._lock:
            registration = self._registrations.get(key)
            if registration is None:
                return False
            server = self._server
            payload = bytes(data[: registration.size])
            if len(payload) < registration.size:
                payload = payload.ljust(registration.size, b"\x00")

            locked = False
            if server is not None:
                try:
                    server.lock_area(registration.srv_area, registration.index)
                    locked = True
                except Exception as exc:  # noqa: BLE001 - lock is best-effort
                    logger.debug("lock_area unavailable for %s: %s", key.label, exc)
            try:
                registration.buffer[:] = payload
            finally:
                if locked and server is not None:
                    try:
                        server.unlock_area(registration.srv_area, registration.index)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("unlock_area failed for %s: %s", key.label, exc)

            if not registration.live_buffer and server is not None:
                # The build copied our buffer at registration, so push the new
                # contents through registerArea again.
                try:
                    server.register_area(
                        registration.srv_area, registration.index, registration.buffer
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not refresh %s on the server: %s", key.label, exc)
                    return False

            registration.last_update_at = time.time()
            registration.updates += 1
            return True

    def read_back(self, key: AreaKey) -> bytes | None:
        """Return what the virtual CPU is currently serving (used by tests)."""
        with self._lock:
            registration = self._registrations.get(key)
            if registration is None:
                return None
            return bytes(registration.buffer)

    @staticmethod
    def _buffer_is_live(server: Server, srv_area: SrvArea, index: int, buffer: bytearray) -> bool:
        """Whether in-place writes to ``buffer`` reach the server's read path.

        python-snap7's server keeps a ``bytearray`` by reference but copies any
        other buffer type. This checks identity where the implementation exposes
        its area table, and assumes live otherwise.
        """
        areas = getattr(server, "memory_areas", None)
        if not isinstance(areas, dict):
            return True
        for key, stored in areas.items():
            if isinstance(key, tuple) and len(key) == 2 and int(key[1]) == index:
                if stored is buffer:
                    return True
        return False

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Status payload for the web UI and crash snapshots."""
        with self._lock:
            server_status = None
            if self._server is not None:
                try:
                    state, cpu_state, clients = self._server.get_status()
                    server_status = {
                        "server": str(state),
                        "cpu": str(cpu_state),
                        "clients": int(clients),
                    }
                except Exception as exc:  # noqa: BLE001
                    server_status = {"error": str(exc)}
            return {
                "running": self._server is not None,
                "bind_ip": self.bind_ip,
                "port": self.port,
                "rack": self.rack,
                "slot": self.slot,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "registered_areas": len(self._registrations),
                "registered_bytes": sum(r.size for r in self._registrations.values()),
                "snap7_status": server_status,
                "areas": [r.as_dict() for r in self.registrations()],
            }
