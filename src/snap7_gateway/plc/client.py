"""Snap7 *client* role - polls the real PLCs (CLAUDE.md section 4.1).

Everything the rest of the gateway does against a physical PLC goes through
:class:`PlcClient`. Its contract is deliberately narrow and defensive:

* every Snap7 call is wrapped, and *any* failure surfaces as :class:`PlcError`
  carrying the underlying Snap7 code and text - never a bare ``RuntimeError``,
  never a ``ctypes`` crash escaping into the polling loop or the web layer;
* the object is **not** thread-safe. A ``S7Object`` handle must be used from one
  thread at a time, so :class:`~snap7_gateway.core.worker.ConnectionWorker`
  pins each client to a single dedicated thread;
* nothing here retries or sleeps. Backoff and supervision are the worker's job,
  which keeps this module trivially testable against a Snap7 server simulator.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import snap7
from snap7.type import Area, Block, Parameter

from ..db.models import AreaType, ConnectionType

logger = logging.getLogger(__name__)

#: Map the gateway's area vocabulary onto Snap7's client-side area codes.
AREA_MAP: dict[str, Area] = {
    AreaType.DB: Area.DB,
    AreaType.INPUT: Area.PE,
    AreaType.OUTPUT: Area.PA,
    AreaType.MERKER: Area.MK,
}

#: Reading more than this in one request is split into chunks. Real S7 CPUs
#: negotiate a PDU of 240-960 bytes; Snap7 splits internally, but keeping our
#: own ceiling bounds the memory a single malformed size request can allocate.
MAX_READ_CHUNK = 8192

#: Hard ceiling on a single area read, so a corrupt discovery result cannot ask
#: the gateway to allocate an arbitrary buffer.
MAX_AREA_BYTES = 8 * 1024 * 1024


class PlcError(RuntimeError):
    """Any failure talking to a real PLC.

    Attributes:
        code: Snap7 error code when one was available, else ``None``.
        operation: short label of what was attempted, for logs and the UI.
    """

    def __init__(self, message: str, *, code: int | None = None, operation: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation

    @property
    def detail(self) -> str:
        """Operator-facing text including the Snap7 code (CLAUDE.md 4.5)."""
        parts = [str(self)]
        if self.code is not None:
            parts.append(f"snap7 code 0x{self.code:08X}")
        if self.operation:
            parts.append(f"during {self.operation}")
        return " - ".join(parts)


@dataclass(frozen=True, slots=True)
class PlcConnectionParams:
    """Everything needed to open one S7 connection."""

    host: str
    rack: int = 0
    slot: int = 2
    tcp_port: int = 102
    connection_type: str = ConnectionType.PG
    timeout_ms: int = 3000

    @classmethod
    def from_model(cls, model: Any) -> "PlcConnectionParams":
        return cls(
            host=model.host,
            rack=model.rack,
            slot=model.slot,
            tcp_port=model.tcp_port,
            connection_type=model.connection_type,
            timeout_ms=model.timeout_ms,
        )


@dataclass(slots=True)
class CpuIdentity:
    """Best-effort identification of the attached CPU, shown on Status."""

    module_type: str = ""
    serial_number: str = ""
    as_name: str = ""
    module_name: str = ""
    order_code: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "module_type": self.module_type,
            "serial_number": self.serial_number,
            "as_name": self.as_name,
            "module_name": self.module_name,
            "order_code": self.order_code,
        }


@dataclass(slots=True)
class ConnectionTestResult:
    """Outcome of the per-connection "Test Connection" button."""

    ok: bool
    latency_ms: float
    message: str
    error_code: int | None = None
    cpu: CpuIdentity | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _wrap(operation: str, exc: BaseException) -> PlcError:
    """Normalize any snap7/ctypes failure into :class:`PlcError`."""
    code = getattr(exc, "error_code", None)
    text = str(exc) or exc.__class__.__name__
    return PlcError(text, code=code, operation=operation)


class PlcClient:
    """Thin, defensive wrapper around :class:`snap7.client.Client`."""

    def __init__(self, params: PlcConnectionParams, *, logger_: Any = None) -> None:
        self.params = params
        self.log = logger_ or logger
        self._client: snap7.client.Client | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            return bool(client.get_connected())
        except Exception:  # noqa: BLE001 - a dead handle reads as "not connected"
            return False

    def connect(self) -> None:
        """Open the S7 connection. Raises :class:`PlcError` on any failure."""
        self.disconnect()  # never leak a previous handle
        try:
            client = snap7.client.Client()
        except Exception as exc:  # noqa: BLE001 - missing/incompatible libsnap7
            raise _wrap("loading the Snap7 library", exc) from exc

        try:
            conn_type = ConnectionType(self.params.connection_type)
        except ValueError:
            conn_type = ConnectionType.PG
        try:
            client.set_connection_type(conn_type.snap7_value)
            timeout = max(int(self.params.timeout_ms), 250)
            for param in (Parameter.PingTimeout, Parameter.SendTimeout, Parameter.RecvTimeout):
                try:
                    client.set_param(param, timeout)
                except Exception:  # noqa: BLE001 - optional tuning, never fatal
                    self.log.debug("could not set %s on the Snap7 client", param.name)
            client.connect(
                self.params.host,
                self.params.rack,
                self.params.slot,
                tcp_port=self.params.tcp_port,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                client.destroy()
            except Exception:  # noqa: BLE001
                pass
            raise _wrap(
                f"connecting to {self.params.host}:{self.params.tcp_port} "
                f"(rack {self.params.rack}, slot {self.params.slot})",
                exc,
            ) from exc

        self._client = client
        self.log.info(
            "connected to %s:%s rack=%s slot=%s type=%s",
            self.params.host,
            self.params.tcp_port,
            self.params.rack,
            self.params.slot,
            conn_type.value,
        )

    def disconnect(self) -> None:
        """Close and destroy the handle. Never raises."""
        client, self._client = self._client, None
        if client is None:
            return
        for step, call in (("disconnect", client.disconnect), ("destroy", client.destroy)):
            try:
                call()
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                self.log.debug("ignoring error during %s: %s", step, exc)

    def __enter__(self) -> "PlcClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.disconnect()

    def _require(self) -> snap7.client.Client:
        if self._client is None:
            raise PlcError("not connected", operation="read")
        return self._client

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def read_area(self, area_type: str, db_number: int, start: int, size: int) -> bytearray:
        """Read ``size`` bytes from a memory area, chunking large requests.

        Chunking keeps each S7 job well inside the negotiated PDU and means a
        20 kB DB (as seen on this site's DB1) is read reliably rather than
        failing as one oversized job.
        """
        if size <= 0:
            return bytearray()
        if size > MAX_AREA_BYTES:
            raise PlcError(
                f"refusing to read {size} bytes (limit {MAX_AREA_BYTES})", operation="read_area"
            )
        area = AREA_MAP.get(str(area_type))
        if area is None:
            raise PlcError(f"unknown area type '{area_type}'", operation="read_area")

        client = self._require()
        out = bytearray()
        remaining = size
        cursor = start
        while remaining > 0:
            chunk = min(remaining, MAX_READ_CHUNK)
            try:
                data = client.read_area(area, db_number, cursor, chunk)
            except Exception as exc:  # noqa: BLE001
                raise _wrap(
                    f"reading {area_type}{db_number if area is Area.DB else ''} "
                    f"bytes {cursor}..{cursor + chunk - 1}",
                    exc,
                ) from exc
            if not data:
                raise PlcError(
                    f"empty response reading {area_type} at byte {cursor}", operation="read_area"
                )
            out.extend(data)
            cursor += chunk
            remaining -= chunk
        return out

    def write_area(self, area_type: str, db_number: int, start: int, data: bytes) -> None:
        """Write raw bytes to a memory area.

        Only reachable when the owning connection has ``write_enabled`` set;
        read-only is the default posture (CLAUDE.md section 3.2).
        """
        area = AREA_MAP.get(str(area_type))
        if area is None:
            raise PlcError(f"unknown area type '{area_type}'", operation="write_area")
        client = self._require()
        try:
            client.write_area(area, db_number, start, bytearray(data))
        except Exception as exc:  # noqa: BLE001
            raise _wrap(
                f"writing {len(data)} byte(s) to {area_type}{db_number} at byte {start}", exc
            ) from exc

    # ------------------------------------------------------------------
    # block discovery primitives
    # ------------------------------------------------------------------
    def list_db_numbers(self, max_count: int = 2048) -> list[int]:
        """Return the DB numbers present on the CPU via Snap7 ``ListBlocks``."""
        client = self._require()
        try:
            blocks = client.list_blocks()
            db_count = int(getattr(blocks, "DBCount", 0))
        except Exception as exc:  # noqa: BLE001
            raise _wrap("listing blocks (ListBlocks)", exc) from exc
        if db_count <= 0:
            return []
        try:
            numbers = client.list_blocks_of_type(Block.DB, min(db_count, max_count))
        except Exception as exc:  # noqa: BLE001
            raise _wrap("listing DB numbers (ListBlocksOfType)", exc) from exc
        return sorted({int(n) for n in numbers})

    def get_db_size(self, db_number: int) -> int:
        """Return the DB's payload size in bytes via ``GetAgBlockInfo``.

        ``MC7Size`` is the byte length DeviceWise's enumeration reports as
        ``DBn UINT1[size]``, which is why the virtual CPU must register exactly
        this number (CLAUDE.md section 4.6).
        """
        client = self._require()
        try:
            info = client.get_block_info(Block.DB, db_number)
        except Exception as exc:  # noqa: BLE001
            raise _wrap(f"reading block info for DB{db_number}", exc) from exc
        size = int(getattr(info, "MC7Size", 0))
        if size < 0:
            raise PlcError(f"DB{db_number} reported a negative size", operation="get_db_size")
        return size

    def probe_area_size(self, area_type: str, max_bytes: int) -> int:
        """Determine a readable size for I/Q/M by probing downward.

        Unlike data blocks, an S7 CPU does not report the length of its process
        image or merker area through block info, and the usable size differs per
        CPU family. Rather than guessing a constant, this halves the requested
        size until a read succeeds, which converges in at most ~13 requests and
        yields a size the CPU has actually served.

        Returns 0 when even a single byte cannot be read.
        """
        size = min(int(max_bytes), MAX_AREA_BYTES)
        while size > 0:
            try:
                self.read_area(area_type, 0, 0, size)
                return size
            except PlcError:
                if size == 1:
                    return 0
                size = max(1, size // 2)
        return 0

    def cpu_identity(self) -> CpuIdentity:
        """Best-effort CPU identification; missing fields stay empty."""
        client = self._require()
        identity = CpuIdentity()
        try:
            info = client.get_cpu_info()
            identity.module_type = _clean(getattr(info, "ModuleTypeName", b""))
            identity.serial_number = _clean(getattr(info, "SerialNumber", b""))
            identity.as_name = _clean(getattr(info, "ASName", b""))
            identity.module_name = _clean(getattr(info, "ModuleName", b""))
        except Exception as exc:  # noqa: BLE001 - some CPUs/simulators refuse SZL
            self.log.debug("CPU info unavailable: %s", exc)
        try:
            identity.order_code = _clean(client.get_order_code().OrderCode)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("order code unavailable: %s", exc)
        return identity

    # ------------------------------------------------------------------
    # test tooling (CLAUDE.md section 4.5)
    # ------------------------------------------------------------------
    @classmethod
    def test_connection(cls, params: PlcConnectionParams) -> ConnectionTestResult:
        """Connect, identify, disconnect - reporting latency and the real error.

        Never raises: the web layer renders the result either way.
        """
        client = cls(params)
        started = time.perf_counter()
        try:
            client.connect()
        except PlcError as exc:
            return ConnectionTestResult(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                message=exc.detail,
                error_code=exc.code,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        cpu = client.cpu_identity()
        details: dict[str, Any] = {}
        try:
            details["db_count"] = len(client.list_db_numbers())
        except PlcError as exc:
            details["db_count_error"] = exc.detail
        finally:
            client.disconnect()
        return ConnectionTestResult(
            ok=True,
            latency_ms=latency_ms,
            message="connected",
            cpu=cpu,
            details=details,
        )


def _clean(value: Any) -> str:
    """Decode a Snap7 fixed-width C string field into readable text."""
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
    return str(value).strip()
