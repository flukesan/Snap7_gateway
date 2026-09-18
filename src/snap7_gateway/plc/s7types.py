"""Declared S7 types from an engineering export, mapped onto what we can read.

A STEP 7 symbol table or TIA tag table names a type the *engineer* used
(``S5TIME``, ``DATE_AND_TIME``, ``STRING[32]``), which is a larger vocabulary
than the gateway decodes. Three things are needed about each of them:

* how many bytes it occupies and how it aligns - needed even for types the
  gateway cannot decode, because a data block's later offsets depend on it;
* which of our :class:`~snap7_gateway.plc.decoding.DataType` values shows it
  usefully, if any;
* an honest note when the mapping is a raw view rather than a true decode
  (``TIME`` read as ``DINT`` is milliseconds, not a formatted duration).

Nothing here guesses silently: a type we cannot present is returned as
unsupported with a reason, and its size is still reported so the layout walk
stays correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .decoding import MAX_STRING_LENGTH, DataType

#: STRING with no declared length is STRING[254] on an S7 CPU.
DEFAULT_STRING_LENGTH = 254

_STRING = re.compile(r"^W?STRING\s*(?:\[\s*(?P<length>\d+)\s*\])?$", re.IGNORECASE)
_ARRAY = re.compile(
    r"^ARRAY\s*\[\s*(?P<lo>-?\d+)\s*\.\.\s*(?P<hi>-?\d+)\s*\]\s*OF\s+(?P<element>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TypeInfo:
    """What one declared type means for reading and for layout."""

    declared: str
    #: Gateway data type, or ``None`` when the gateway cannot present it.
    data_type: str | None
    #: STRING character count; 1 for everything else.
    length: int = 1
    #: Bytes occupied inside a data block.
    size_bytes: int = 1
    #: 1 = byte aligned, 2 = word aligned, 0 = bit (packed).
    alignment: int = 1
    #: Set when the mapping is a raw view rather than a faithful decode.
    note: str = ""
    #: Set when ``data_type`` is ``None``.
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.data_type is not None

    @property
    def is_bit(self) -> bool:
        return self.alignment == 0


def _ok(declared: str, data_type: DataType, size: int, alignment: int, *, note: str = "",
        length: int = 1) -> TypeInfo:
    return TypeInfo(declared, data_type.value, length, size, alignment, note)


def _no(declared: str, size: int, alignment: int, reason: str) -> TypeInfo:
    return TypeInfo(declared, None, 1, size, alignment, "", reason)


#: Fixed (non-parameterised) types. Sizes follow the S7 standard-access layout;
#: alignment is word-based, which is what S7-300/400 and non-optimized
#: S7-1200/1500 data blocks use.
_FIXED: dict[str, TypeInfo] = {
    "BOOL": _ok("BOOL", DataType.BOOL, 1, 0),
    "BYTE": _ok("BYTE", DataType.BYTE, 1, 1),
    "CHAR": _ok("CHAR", DataType.CHAR, 1, 1),
    "SINT": _ok("SINT", DataType.BYTE, 1, 1, note="signed 8-bit, shown as unsigned BYTE"),
    "USINT": _ok("USINT", DataType.BYTE, 1, 1),
    "WORD": _ok("WORD", DataType.WORD, 2, 2),
    "INT": _ok("INT", DataType.INT, 2, 2),
    "UINT": _ok("UINT", DataType.UINT, 2, 2),
    "DWORD": _ok("DWORD", DataType.DWORD, 4, 2),
    "DINT": _ok("DINT", DataType.DINT, 4, 2),
    "UDINT": _ok("UDINT", DataType.UDINT, 4, 2),
    "REAL": _ok("REAL", DataType.REAL, 4, 2),
    "LREAL": _ok("LREAL", DataType.LREAL, 8, 2),
    # Time and date types are integers underneath; showing the raw integer is
    # useful and honest, so long as the note says what the number is.
    "S5TIME": _ok("S5TIME", DataType.WORD, 2, 2,
                  note="S5TIME raw word (BCD time base and value)"),
    "TIME": _ok("TIME", DataType.DINT, 4, 2, note="IEC TIME raw, milliseconds"),
    "DATE": _ok("DATE", DataType.UINT, 2, 2, note="IEC DATE raw, days since 1990-01-01"),
    "TIME_OF_DAY": _ok("TIME_OF_DAY", DataType.UDINT, 4, 2,
                       note="TIME_OF_DAY raw, milliseconds since midnight"),
    "TOD": _ok("TOD", DataType.UDINT, 4, 2, note="TIME_OF_DAY raw, milliseconds since midnight"),
    # Multi-word structures have no single scalar reading.
    "DATE_AND_TIME": _no("DATE_AND_TIME", 8, 2,
                         "DATE_AND_TIME is an 8-byte BCD structure; add byte tags if needed"),
    "DT": _no("DT", 8, 2, "DT is an 8-byte BCD structure; add byte tags if needed"),
    "DTL": _no("DTL", 12, 2, "DTL is a 12-byte structure; add member tags if needed"),
    "LTIME": _no("LTIME", 8, 2, "LTIME is 64-bit; not decoded by this gateway"),
    "LINT": _no("LINT", 8, 2, "LINT is 64-bit; not decoded by this gateway"),
    "ULINT": _no("ULINT", 8, 2, "ULINT is 64-bit; not decoded by this gateway"),
    "LWORD": _no("LWORD", 8, 2, "LWORD is 64-bit; not decoded by this gateway"),
    # Block and PLC-object references carry no readable payload.
    "TIMER": _no("TIMER", 2, 2, "timers are not mirrored by this gateway"),
    "COUNTER": _no("COUNTER", 2, 2, "counters are not mirrored by this gateway"),
    "IEC_TIMER": _no("IEC_TIMER", 16, 2, "IEC_TIMER is a structure; add member tags if needed"),
    "IEC_COUNTER": _no("IEC_COUNTER", 16, 2, "IEC_COUNTER is a structure; add member tags if needed"),
}

#: Spellings that differ between STEP 7 classic, TIA Portal and German projects.
_ALIASES: dict[str, str] = {
    "BOOLEAN": "BOOL",
    "INTEGER": "INT",
    "DOUBLE INT": "DINT",
    "DOUBLE WORD": "DWORD",
    "TIME_OF_DAY (TOD)": "TOD",
    "DATE_AND_TIME (DT)": "DT",
    "REAL (FLOAT)": "REAL",
    "BOOL (BIT)": "BOOL",
    "CHARACTER": "CHAR",
}


def normalize_declared(declared: str) -> str:
    """Upper-case and squeeze whitespace so lookups are spelling-tolerant."""
    text = re.sub(r"\s+", " ", str(declared or "").strip()).upper()
    text = text.removeprefix('"').removesuffix('"')
    return _ALIASES.get(text, text)


def map_declared_type(declared: str) -> TypeInfo:
    """Resolve one declared type. Never raises.

    An unknown type comes back unsupported with a one-byte size, which keeps a
    layout walk moving but is flagged so the caller can tell the operator that
    the rest of that block's offsets are not trustworthy.
    """
    text = normalize_declared(declared)
    if not text:
        return _no("", 1, 1, "no data type given")

    fixed = _FIXED.get(text)
    if fixed is not None:
        return fixed

    match = _STRING.match(text)
    if match:
        raw_length = match.group("length")
        length = int(raw_length) if raw_length else DEFAULT_STRING_LENGTH
        if text.upper().startswith("WSTRING"):
            return _no(text, 2 * length + 4, 2,
                       "WSTRING is 16-bit per character; not decoded by this gateway")
        if not 1 <= length <= MAX_STRING_LENGTH:
            return _no(text, 2, 2, f"STRING length {length} is outside 1..{MAX_STRING_LENGTH}")
        return _ok(text, DataType.STRING, length + 2, 2, length=length)

    match = _ARRAY.match(text)
    if match:
        low, high = int(match.group("lo")), int(match.group("hi"))
        element = map_declared_type(match.group("element"))
        count = high - low + 1
        if count <= 0:
            return _no(text, 1, 1, f"array bounds [{low}..{high}] are empty")
        return TypeInfo(
            declared=text,
            data_type=None,
            length=1,
            size_bytes=array_size(element, count),
            alignment=max(element.alignment, 1),
            reason=(
                f"ARRAY[{low}..{high}] OF {element.declared} - import the elements you "
                "need as individual tags"
            ),
        )

    return _no(text, 1, 1, f"unrecognised data type '{declared}'")


def array_size(element: TypeInfo, count: int) -> int:
    """Bytes occupied by ``count`` elements of ``element`` inside a data block."""
    if element.is_bit:
        # BOOLs pack 8 per byte and the array is padded to a whole word.
        return _round_up(_round_up(count, 8) // 8, 2)
    stride = element.size_bytes
    if element.alignment == 2:
        stride = _round_up(stride, 2)
    return _round_up(stride * count, 2) if element.alignment == 2 else stride * count


def _round_up(value: int, multiple: int) -> int:
    if multiple <= 1:
        return value
    remainder = value % multiple
    return value if remainder == 0 else value + (multiple - remainder)
