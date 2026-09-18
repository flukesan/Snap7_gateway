"""S7 data-type decoding and encoding (CLAUDE.md section 4.1).

The gateway decodes raw area bytes itself rather than shipping undecoded
buffers downstream, so every consumer sees the same interpretation. All S7
scalars are big-endian; bit 0 of a byte is the least significant bit, which is
why ``DBX4.2`` is ``(buffer[4] >> 2) & 1``.

``STRING`` uses the classic Siemens layout::

    byte 0 : declared maximum length
    byte 1 : current length
    byte 2+: characters

These functions are pure and take plain ``bytes``/``bytearray`` input, so the
full matrix is unit-testable against recorded buffers with no PLC present.
"""

from __future__ import annotations

import struct
from enum import StrEnum
from typing import Any


class DecodeError(ValueError):
    """Raised when a buffer cannot satisfy the requested address.

    Always raised instead of letting ``IndexError``/``struct.error`` escape, so
    callers in the polling loop and web layer have exactly one exception type
    to guard against.
    """


class DataType(StrEnum):
    """Supported S7 element types."""

    BOOL = "BOOL"
    BYTE = "BYTE"
    CHAR = "CHAR"
    WORD = "WORD"
    DWORD = "DWORD"
    INT = "INT"
    DINT = "DINT"
    UINT = "UINT"
    UDINT = "UDINT"
    REAL = "REAL"
    LREAL = "LREAL"
    STRING = "STRING"

    @property
    def is_string(self) -> bool:
        return self is DataType.STRING

    @property
    def needs_bit(self) -> bool:
        return self is DataType.BOOL

    @property
    def needs_length(self) -> bool:
        """Whether the ``length`` field is meaningful for this type."""
        return self is DataType.STRING


#: Width in bytes of one element, excluding STRING which is length-dependent.
_FIXED_WIDTH: dict[DataType, int] = {
    DataType.BOOL: 1,
    DataType.BYTE: 1,
    DataType.CHAR: 1,
    DataType.WORD: 2,
    DataType.DWORD: 4,
    DataType.INT: 2,
    DataType.DINT: 4,
    DataType.UINT: 2,
    DataType.UDINT: 4,
    DataType.REAL: 4,
    DataType.LREAL: 8,
}

_STRUCT_FORMAT: dict[DataType, str] = {
    DataType.WORD: ">H",
    DataType.DWORD: ">I",
    DataType.INT: ">h",
    DataType.DINT: ">i",
    DataType.UINT: ">H",
    DataType.UDINT: ">I",
    DataType.REAL: ">f",
    DataType.LREAL: ">d",
}

#: Largest STRING an S7 CPU can declare.
MAX_STRING_LENGTH = 254


def coerce_type(data_type: str | DataType) -> DataType:
    """Normalize operator input (``"real"``, ``"REAL"``) to a :class:`DataType`."""
    if isinstance(data_type, DataType):
        return data_type
    try:
        return DataType(str(data_type).strip().upper())
    except ValueError as exc:
        supported = ", ".join(t.value for t in DataType)
        raise DecodeError(f"unsupported data type '{data_type}' (supported: {supported})") from exc


def size_of(data_type: str | DataType, length: int = 1) -> int:
    """Return how many bytes one value of this type occupies.

    For ``STRING``, ``length`` is the declared maximum character count and the
    on-wire size is ``length + 2`` (the max-length and current-length headers).
    """
    dtype = coerce_type(data_type)
    if dtype.is_string:
        if not 1 <= length <= MAX_STRING_LENGTH:
            raise DecodeError(
                f"STRING length must be between 1 and {MAX_STRING_LENGTH} (got {length})"
            )
        return length + 2
    return _FIXED_WIDTH[dtype]


def _check_span(buffer: bytes | bytearray, offset: int, width: int, what: str) -> None:
    if offset < 0:
        raise DecodeError(f"byte offset must be >= 0 (got {offset})")
    if offset + width > len(buffer):
        raise DecodeError(
            f"{what} at byte {offset} needs {width} byte(s) but the buffer holds "
            f"{len(buffer)} byte(s)"
        )


def decode(
    buffer: bytes | bytearray,
    data_type: str | DataType,
    offset: int = 0,
    bit: int = 0,
    length: int = 1,
) -> Any:
    """Decode a single value out of ``buffer``.

    Args:
        buffer: raw area bytes as read from the PLC.
        data_type: one of :class:`DataType`.
        offset: byte offset of the value inside ``buffer``.
        bit: bit index 0-7, only used for ``BOOL``.
        length: declared maximum length, only used for ``STRING``.

    Raises:
        DecodeError: on an unknown type, a bad bit index, or a short buffer.
    """
    dtype = coerce_type(data_type)

    if dtype is DataType.BOOL:
        if not 0 <= bit <= 7:
            raise DecodeError(f"bit index must be 0-7 (got {bit})")
        _check_span(buffer, offset, 1, "BOOL")
        return bool((buffer[offset] >> bit) & 0x01)

    if dtype is DataType.BYTE:
        _check_span(buffer, offset, 1, "BYTE")
        return int(buffer[offset])

    if dtype is DataType.CHAR:
        _check_span(buffer, offset, 1, "CHAR")
        return chr(buffer[offset])

    if dtype is DataType.STRING:
        return _decode_string(buffer, offset, length)

    width = _FIXED_WIDTH[dtype]
    _check_span(buffer, offset, width, dtype.value)
    try:
        (value,) = struct.unpack_from(_STRUCT_FORMAT[dtype], buffer, offset)
    except struct.error as exc:  # pragma: no cover - guarded by _check_span
        raise DecodeError(f"failed to decode {dtype.value} at byte {offset}: {exc}") from exc
    return value


def _decode_string(buffer: bytes | bytearray, offset: int, length: int) -> str:
    _check_span(buffer, offset, 2, "STRING header")
    declared_max = int(buffer[offset])
    actual = int(buffer[offset + 1])
    # A PLC that has never initialised the string, or a mis-aligned address,
    # shows up here as a nonsense header. Clamp rather than raise so one bad
    # tag cannot stall a whole poll cycle, but never read past the buffer.
    limit = min(declared_max or length, length, MAX_STRING_LENGTH)
    if actual > limit:
        actual = limit
    _check_span(buffer, offset, 2 + actual, "STRING body")
    raw = bytes(buffer[offset + 2 : offset + 2 + actual])
    return raw.decode("latin-1", errors="replace")


def encode(
    value: Any, data_type: str | DataType, length: int = 1, *, bit_source: int = 0
) -> bytes:
    """Encode ``value`` into its S7 wire representation.

    Used by the (opt-in, per-connection) write path. ``BOOL`` returns a single
    byte with only the target bit set; callers must read-modify-write the
    surrounding byte, which :func:`apply_bit` does.
    """
    dtype = coerce_type(data_type)

    if dtype is DataType.BOOL:
        if not 0 <= bit_source <= 7:
            raise DecodeError(f"bit index must be 0-7 (got {bit_source})")
        return bytes([(1 << bit_source) if _as_bool(value) else 0])

    if dtype is DataType.BYTE:
        as_int = _as_int(value)
        if not 0 <= as_int <= 0xFF:
            raise DecodeError(f"BYTE must be 0-255 (got {as_int})")
        return bytes([as_int])

    if dtype is DataType.CHAR:
        text = str(value)
        if len(text) != 1:
            raise DecodeError("CHAR must be exactly one character")
        return text.encode("latin-1", errors="replace")

    if dtype is DataType.STRING:
        if not 1 <= length <= MAX_STRING_LENGTH:
            raise DecodeError(
                f"STRING length must be between 1 and {MAX_STRING_LENGTH} (got {length})"
            )
        raw = str(value).encode("latin-1", errors="replace")[:length]
        return bytes([length, len(raw)]) + raw + bytes(length - len(raw))

    fmt = _STRUCT_FORMAT[dtype]
    number: Any = float(value) if dtype in (DataType.REAL, DataType.LREAL) else _as_int(value)
    try:
        return struct.pack(fmt, number)
    except struct.error as exc:
        raise DecodeError(f"value {value!r} is out of range for {dtype.value}") from exc


def apply_bit(current_byte: int, bit: int, value: bool) -> int:
    """Return ``current_byte`` with ``bit`` set or cleared - read-modify-write."""
    if not 0 <= bit <= 7:
        raise DecodeError(f"bit index must be 0-7 (got {bit})")
    if not 0 <= current_byte <= 0xFF:
        raise DecodeError("current_byte must be 0-255")
    mask = 1 << bit
    return (current_byte | mask) if value else (current_byte & ~mask & 0xFF)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError) as exc:
        raise DecodeError(f"{value!r} is not an integer") from exc


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise DecodeError(f"{value!r} is not a boolean")


def format_value(value: Any, data_type: str | DataType) -> str:
    """Render a decoded value for the web UI in a stable, locale-neutral way."""
    dtype = coerce_type(data_type)
    if dtype is DataType.BOOL:
        return "TRUE" if value else "FALSE"
    if dtype in (DataType.REAL, DataType.LREAL):
        return f"{float(value):.6g}"
    if dtype in (DataType.WORD, DataType.DWORD):
        width = 4 if dtype is DataType.WORD else 8
        return f"{int(value)} (0x{int(value):0{width}X})"
    if dtype is DataType.STRING:
        return str(value)
    return str(value)
