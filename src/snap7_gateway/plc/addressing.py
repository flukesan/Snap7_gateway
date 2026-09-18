"""Parsing Siemens absolute addresses into the gateway's area/offset model.

Snap7 hands back raw bytes; it cannot tell you what ``MW20`` *means*, because a
classic S7 CPU does not store the symbol table at all - names and comments live
in the STEP 7 / TIA engineering project. The bridge between the two is an
address string, so this module turns every spelling an operator's export might
contain into ``(area, db, byte, bit)``:

    English mnemonics   I0.0   IB10   IW22   ID24   Q1.3   M7.1   MB7  MW20  MD24
    German mnemonics    E0.0   EB10   EW22   ED24   A1.3   M7.1   MB7  MW20  MD24
    TIA percent form    %I0.0  %MW20  %Q0.1  %DB10.DBW4
    Data blocks         DB10.DBX4.2   DB10.DBB4   DB10.DBW4   DB10.DBD4
    Spaced variants     "I 0.0"   "DB10.DBW 4"   (STEP 7 .asc exports)

Timers (``T5``) and counters (``C5``/``Z5``) parse to their own area constants so
a caller can report them as deliberately unsupported rather than silently
mis-mapping them onto merkers - the gateway mirrors I/Q/M/DB only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..db.models import AreaType
from .decoding import DataType

#: Areas that exist in an export but that this gateway does not mirror.
UNSUPPORTED_TIMER = "T"
UNSUPPORTED_COUNTER = "C"

#: Mnemonic letter -> gateway area. German STEP 7 installations are common on
#: machines built in Europe, so E (Eingang) and A (Ausgang) are accepted too.
_AREA_LETTERS: dict[str, str] = {
    "I": AreaType.INPUT,
    "E": AreaType.INPUT,
    "Q": AreaType.OUTPUT,
    "A": AreaType.OUTPUT,
    "M": AreaType.MERKER,
    "F": AreaType.MERKER,  # "Flag", used by some third-party tools
}

#: Width suffix -> (bytes, default data type when the export gives none).
_WIDTH_SUFFIX: dict[str, tuple[int, str]] = {
    "": (0, DataType.BOOL.value),  # bit address, e.g. M7.1
    "B": (1, DataType.BYTE.value),
    "W": (2, DataType.WORD.value),
    "D": (4, DataType.DWORD.value),
    "X": (0, DataType.BOOL.value),  # DBX
}

# I/Q/M/T/C: optional %, letter, optional width suffix, byte, optional .bit
_SIMPLE = re.compile(
    r"^%?(?P<letter>[IEQAMF])\s*(?P<width>[BWDX]?)\s*(?P<byte>\d+)(?:\.(?P<bit>\d+))?$",
    re.IGNORECASE,
)
_TIMER_COUNTER = re.compile(r"^%?(?P<letter>[TCZ])\s*(?P<number>\d+)$", re.IGNORECASE)

# DB10.DBW4 / %DB10.DBX4.2 / DB10.DBB 4
_DB = re.compile(
    r"^%?DB\s*(?P<db>\d+)\s*\.\s*DB(?P<width>[XBWD])\s*(?P<byte>\d+)(?:\.(?P<bit>\d+))?$",
    re.IGNORECASE,
)

# A whole block symbol such as "DB10", "FC12", "OB1" - a name for the block
# itself, carrying no data address.
_BLOCK = re.compile(r"^%?(?P<kind>DB|FB|FC|OB|SFB|SFC|UDT|VAT)\s*(?P<number>\d+)$", re.IGNORECASE)


class AddressError(ValueError):
    """Raised when an address string cannot be understood."""


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    """An absolute address resolved to the gateway's area model."""

    area_type: str
    db_number: int
    byte_offset: int
    bit_offset: int
    #: Data type implied by the address form itself (``MW`` -> WORD). An export's
    #: own data-type column, when it has one, wins over this.
    implied_type: str
    #: True for areas this gateway cannot mirror (timers, counters, blocks).
    unsupported: bool = False
    unsupported_reason: str = ""

    @property
    def is_bit(self) -> bool:
        return self.implied_type == DataType.BOOL.value

    def canonical(self) -> str:
        """Render the address back in a consistent, English-mnemonic form."""
        if self.unsupported:
            return f"{self.area_type}{self.db_number or self.byte_offset}"
        if self.area_type == AreaType.DB:
            if self.is_bit:
                return f"DB{self.db_number}.DBX{self.byte_offset}.{self.bit_offset}"
            suffix = {1: "B", 2: "W", 4: "D"}.get(_width_of(self.implied_type), "B")
            return f"DB{self.db_number}.DB{suffix}{self.byte_offset}"
        letter = {AreaType.INPUT: "I", AreaType.OUTPUT: "Q", AreaType.MERKER: "M"}[
            self.area_type  # type: ignore[index]
        ]
        if self.is_bit:
            return f"{letter}{self.byte_offset}.{self.bit_offset}"
        suffix = {1: "B", 2: "W", 4: "D"}.get(_width_of(self.implied_type), "B")
        return f"{letter}{suffix}{self.byte_offset}"


def _width_of(data_type: str) -> int:
    return {
        DataType.BYTE.value: 1,
        DataType.CHAR.value: 1,
        DataType.WORD.value: 2,
        DataType.INT.value: 2,
        DataType.UINT.value: 2,
        DataType.DWORD.value: 4,
        DataType.DINT.value: 4,
        DataType.UDINT.value: 4,
        DataType.REAL.value: 4,
    }.get(data_type, 1)


def parse_address(text: str) -> ParsedAddress:
    """Parse one absolute address string.

    Raises:
        AddressError: when the text is not an address this module recognises.
    """
    if text is None:
        raise AddressError("address is empty")
    cleaned = str(text).strip().replace(" ", " ")
    if not cleaned:
        raise AddressError("address is empty")
    # STEP 7 exports pad addresses into fixed-width columns ("DB10.DBW   4").
    cleaned = re.sub(r"\s+", " ", cleaned)

    match = _DB.match(cleaned)
    if match:
        width = match.group("width").upper()
        bit = match.group("bit")
        if width == "X" and bit is None:
            raise AddressError(f"'{text}' is a bit address but has no bit number")
        if width != "X" and bit is not None:
            raise AddressError(f"'{text}' has a bit number on a non-bit address")
        _, implied = _WIDTH_SUFFIX[width]
        return ParsedAddress(
            area_type=AreaType.DB,
            db_number=int(match.group("db")),
            byte_offset=int(match.group("byte")),
            bit_offset=int(bit or 0),
            implied_type=implied,
        )

    # Block names are checked before the single-letter mnemonics: 'F' is an
    # accepted merker alias, so 'FB2' would otherwise read as merker byte 2.
    match = _BLOCK.match(cleaned)
    if match:
        kind = match.group("kind").upper()
        return ParsedAddress(
            area_type=kind,
            db_number=int(match.group("number")),
            byte_offset=0,
            bit_offset=0,
            implied_type=DataType.BYTE.value,
            unsupported=True,
            unsupported_reason=f"{kind}{match.group('number')} names a block, not a data address",
        )

    match = _SIMPLE.match(cleaned)
    if match:
        letter = match.group("letter").upper()
        width = match.group("width").upper()
        bit = match.group("bit")
        if width in {"", "X"}:
            if bit is None:
                raise AddressError(
                    f"'{text}' looks like a bit address but has no bit number "
                    "(write M7.1, or MB7 for a byte)"
                )
        elif bit is not None:
            raise AddressError(f"'{text}' has a bit number on a non-bit address")
        bit_value = int(bit or 0)
        if not 0 <= bit_value <= 7:
            raise AddressError(f"'{text}' has bit {bit_value}; bits are 0-7")
        _, implied = _WIDTH_SUFFIX[width]
        return ParsedAddress(
            area_type=_AREA_LETTERS[letter],
            db_number=0,
            byte_offset=int(match.group("byte")),
            bit_offset=bit_value,
            implied_type=implied,
        )

    match = _TIMER_COUNTER.match(cleaned)
    if match:
        letter = match.group("letter").upper()
        is_timer = letter == "T"
        return ParsedAddress(
            area_type=UNSUPPORTED_TIMER if is_timer else UNSUPPORTED_COUNTER,
            db_number=int(match.group("number")),
            byte_offset=int(match.group("number")),
            bit_offset=0,
            implied_type=DataType.WORD.value,
            unsupported=True,
            unsupported_reason=(
                "timers are not mirrored by this gateway"
                if is_timer
                else "counters are not mirrored by this gateway"
            ),
        )

    raise AddressError(f"'{text}' is not a recognised S7 address")


def try_parse_address(text: str) -> ParsedAddress | None:
    """Like :func:`parse_address` but returns ``None`` instead of raising."""
    try:
        return parse_address(text)
    except AddressError:
        return None
