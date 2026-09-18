"""Reading symbol/comment exports from STEP 7 and TIA Portal.

Snap7 gives the gateway bytes and block sizes; it cannot tell anyone what
``MW20`` means, because a classic S7 CPU does not hold the symbol table - names
and comments live in the engineering project. Importing an export file is
therefore the only way to put meaning next to an address, and this module reads
the formats a site actually has:

``.sdf``
    STEP 7 classic symbol table, quoted CSV:
    ``"Motor_Start","E      0.0","BOOL","Start button"``

``.asc``
    STEP 7 classic symbol table, whitespace/fixed-width columns, usually with a
    leading ordinal: ``126,Motor_Start   E 0.0   BOOL   Start button``

``.csv``
    Any comma/semicolon/tab-separated export with a header row - TIA Portal's
    CSV output, or a table an engineer maintains by hand.

``.xlsx``
    TIA Portal PLC tag table ("Export to file"), read with the standard library
    only: an xlsx is a zip of XML, and no dependency is worth adding to an
    air-gapped install for four columns.

Everything is reported, never guessed: a row that cannot be parsed, an address
the gateway does not mirror (timers, counters, block names) and a data type it
cannot decode all come back in ``skipped`` with a specific reason.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

from ..db.models import AreaType
from .addressing import AddressError, ParsedAddress, parse_address
from .s7types import map_declared_type

#: Tag names must satisfy the same rule as names typed into the web form.
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9 ._\-]")
MAX_NAME_LENGTH = 64

#: Refuse absurd files rather than trying to parse them.
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100_000

_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "tag name", "symbol", "symbolname", "symbol name", "tagname"),
    "address": ("logical address", "address", "adresse", "operand", "logical_address"),
    "data_type": ("data type", "datatype", "data_type", "type", "datentyp", "typ"),
    "comment": ("comment", "kommentar", "description", "beschreibung", "remark"),
}


@dataclass(slots=True)
class SymbolEntry:
    """One importable symbol, already resolved to an address and a data type."""

    name: str
    address_text: str
    area_type: str
    db_number: int
    byte_offset: int
    bit_offset: int
    data_type: str
    length: int
    comment: str = ""
    #: Set when the original name had to be adjusted to fit the tag-name rule.
    original_name: str = ""
    #: Set when the data type is a raw view rather than a faithful decode.
    note: str = ""
    source_row: int = 0

    @property
    def renamed(self) -> bool:
        return bool(self.original_name) and self.original_name != self.name

    def as_tag_values(self) -> dict[str, object]:
        """The payload :meth:`Database.create_tag` expects."""
        description = self.comment.strip()
        if self.note:
            description = f"{description} [{self.note}]" if description else f"[{self.note}]"
        return {
            "name": self.name,
            "area_type": self.area_type,
            "db_number": self.db_number,
            "byte_offset": self.byte_offset,
            "bit_offset": self.bit_offset,
            "data_type": self.data_type,
            "length": self.length,
            "description": description[:255] or None,
        }


@dataclass(slots=True)
class SkippedSymbol:
    """A row that was deliberately not imported, and why."""

    name: str
    address_text: str
    reason: str
    source_row: int = 0


@dataclass(slots=True)
class SymbolFile:
    """Everything one export file yielded."""

    format: str
    entries: list[SymbolEntry] = field(default_factory=list)
    skipped: list[SkippedSymbol] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return len(self.entries) + len(self.skipped)

    def summary(self) -> str:
        parts = [f"{len(self.entries)} symbol(s) read"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.errors:
            parts.append(f"{len(self.errors)} file-level problem(s)")
        return ", ".join(parts)


class SymbolImportError(ValueError):
    """The file could not be read at all."""


# ----------------------------------------------------------------------
# name handling
# ----------------------------------------------------------------------
def sanitize_name(raw: str, *, taken: set[str] | None = None) -> str:
    """Turn an engineering symbol into a name the tag table accepts.

    TIA and STEP 7 allow characters the gateway's own validator does not (a tag
    name ends up in logs, audit entries and URLs). Disallowed characters become
    underscores, and a collision gets a numeric suffix rather than overwriting
    an earlier symbol.
    """
    text = str(raw or "").strip().strip('"').strip()
    text = _NAME_ALLOWED.sub("_", text)
    text = re.sub(r"_{3,}", "__", text).strip()
    if text and not text[0].isalnum():
        text = f"T{text}"
    text = text[:MAX_NAME_LENGTH].strip() or "Tag"

    if taken is None:
        return text
    candidate = text
    suffix = 2
    while candidate.lower() in taken:
        tail = f"_{suffix}"
        candidate = f"{text[: MAX_NAME_LENGTH - len(tail)]}{tail}"
        suffix += 1
    taken.add(candidate.lower())
    return candidate


# ----------------------------------------------------------------------
# row -> entry
# ----------------------------------------------------------------------
def build_entry(
    name: str,
    address_text: str,
    declared_type: str,
    comment: str,
    *,
    row_number: int,
    taken: set[str],
) -> SymbolEntry | SkippedSymbol:
    """Resolve one raw row. Returns either an entry or the reason it was skipped."""
    raw_name = str(name or "").strip()
    address_text = str(address_text or "").strip()

    if not raw_name and not address_text:
        return SkippedSymbol("", "", "empty row", row_number)
    if not address_text:
        return SkippedSymbol(raw_name, "", "no address given", row_number)

    try:
        parsed: ParsedAddress = parse_address(address_text)
    except AddressError as exc:
        return SkippedSymbol(raw_name, address_text, str(exc), row_number)

    if parsed.unsupported:
        return SkippedSymbol(raw_name, address_text, parsed.unsupported_reason, row_number)
    if parsed.area_type not in {AreaType.DB, AreaType.INPUT, AreaType.OUTPUT, AreaType.MERKER}:
        return SkippedSymbol(
            raw_name, address_text, f"area {parsed.area_type} is not mirrored", row_number
        )

    # The export's own type column wins; the address form is the fallback for
    # tables that do not carry one.
    info = map_declared_type(declared_type) if str(declared_type or "").strip() else None
    if info is None or (not info.supported and not str(declared_type or "").strip()):
        data_type, length, note = parsed.implied_type, 1, ""
    elif not info.supported:
        return SkippedSymbol(raw_name, address_text, info.reason, row_number)
    else:
        data_type, length, note = info.data_type, info.length, info.note

    # A bit address must carry a bit type and vice versa, whatever the columns say.
    if parsed.is_bit and data_type != "BOOL":
        return SkippedSymbol(
            raw_name,
            address_text,
            f"address is a single bit but the type is {data_type}",
            row_number,
        )
    if not parsed.is_bit and data_type == "BOOL":
        return SkippedSymbol(
            raw_name,
            address_text,
            "type is BOOL but the address has no bit number",
            row_number,
        )

    clean_name = sanitize_name(raw_name or parsed.canonical(), taken=taken)
    return SymbolEntry(
        name=clean_name,
        address_text=address_text,
        area_type=parsed.area_type,
        db_number=parsed.db_number,
        byte_offset=parsed.byte_offset,
        bit_offset=parsed.bit_offset,
        data_type=str(data_type),
        length=length,
        comment=str(comment or "").strip(),
        original_name=raw_name,
        note=note,
        source_row=row_number,
    )


def _collect(rows: list[tuple[int, str, str, str, str]], fmt: str) -> SymbolFile:
    """Turn ``(row, name, address, type, comment)`` tuples into a result."""
    result = SymbolFile(format=fmt)
    taken: set[str] = set()
    for row_number, name, address, declared, comment in rows[:MAX_ROWS]:
        outcome = build_entry(
            name, address, declared, comment, row_number=row_number, taken=taken
        )
        if isinstance(outcome, SymbolEntry):
            result.entries.append(outcome)
        else:
            result.skipped.append(outcome)
    if len(rows) > MAX_ROWS:
        result.errors.append(f"file has {len(rows)} rows; only the first {MAX_ROWS} were read")
    return result


# ----------------------------------------------------------------------
# STEP 7 classic
# ----------------------------------------------------------------------
def parse_sdf(text: str) -> SymbolFile:
    """STEP 7 classic ``.sdf`` symbol table (quoted, comma separated)."""
    rows: list[tuple[int, str, str, str, str]] = []
    reader = csv.reader(io.StringIO(text), quotechar='"', skipinitialspace=True)
    for index, fields in enumerate(reader, start=1):
        if not fields or all(not f.strip() for f in fields):
            continue
        padded = [f.strip() for f in fields] + ["", "", "", ""]
        rows.append((index, padded[0], padded[1], padded[2], padded[3]))
    return _collect(rows, "STEP 7 .sdf symbol table")


#: STEP 7 writes ``.asc`` as fixed-width columns whose widths differ between
#: versions, and an address itself contains spaces ("E      0.0"), so neither a
#: whitespace split nor a fixed slice is reliable. Instead the address is found
#: by pattern: everything before it is the symbol, the first token after it is
#: the data type, and the rest is the comment.
_ASC_ORDINAL = re.compile(r"^\s*\d+\s*,\s*")

_ASC_ADDRESS = re.compile(
    r"(?<![\w.])("
    r"DB\s*\d+\s*\.\s*DB[XBWD]\s*\d+(?:\.\d+)?"      # DB10.DBW 4
    r"|[IEQAMF]\s*[BWDX]?\s*\d+(?:\.\d+)?"               # E 0.0 / MW 20
    r"|[TCZ]\s*\d+"                                        # T 5 / Z 12
    r"|(?:DB|FB|FC|OB|SFB|SFC|UDT|VAT)\s*\d+"               # block symbols
    r")(?![\w.])",
    re.IGNORECASE,
)

#: Tokens that mark the start of the data-type column, used to disambiguate a
#: symbol name that happens to look like an address.
_ASC_TYPE_TOKEN = re.compile(
    r"^(BOOL|BYTE|CHAR|WORD|INT|DWORD|DINT|UINT|UDINT|SINT|USINT|REAL|LREAL|S5TIME|TIME"
    r"|DATE(_AND_TIME)?|DT|DTL|TOD|TIME_OF_DAY|W?STRING(\s*\[\s*\d+\s*\])?|TIMER|COUNTER"
    r"|FB|FC|OB|SFB|SFC|UDT|DB|IEC_TIMER|IEC_COUNTER)\b",
    re.IGNORECASE,
)


def _split_asc_line(line: str) -> tuple[str, str, str, str] | None:
    """Split one ``.asc`` line into (name, address, data type, comment)."""
    matches = list(_ASC_ADDRESS.finditer(line))
    if not matches:
        return None

    # Prefer the match whose trailing text starts with a data-type token; that
    # rules out a symbol such as "M1_Pump" being read as merker address M1.
    chosen = None
    for match in matches:
        if _ASC_TYPE_TOKEN.match(line[match.end() :].lstrip()):
            chosen = match
            break
    if chosen is None:
        chosen = matches[0]

    name = line[: chosen.start()].strip()
    remainder = line[chosen.end() :].strip()
    type_match = _ASC_TYPE_TOKEN.match(remainder)
    if type_match:
        declared = type_match.group(0)
        comment = remainder[type_match.end() :].strip()
    else:
        parts = remainder.split(None, 1)
        declared = parts[0] if parts else ""
        comment = parts[1].strip() if len(parts) > 1 else ""
    return name, chosen.group(1), declared, comment


def parse_asc(text: str) -> SymbolFile:
    """STEP 7 classic ``.asc`` symbol table (fixed-width columns)."""
    rows: list[tuple[int, str, str, str, str]] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        line = _ASC_ORDINAL.sub("", raw.rstrip())
        if not line.strip():
            continue
        split = _split_asc_line(line)
        if split is None:
            rows.append((index, line.strip(), "", "", ""))
            continue
        name, address, declared, comment = split
        rows.append((index, name, address, declared, comment))
    return _collect(rows, "STEP 7 .asc symbol table")


# ----------------------------------------------------------------------
# delimited text with a header row
# ----------------------------------------------------------------------
def _match_headers(header: list[str]) -> dict[str, int] | None:
    """Map our four fields onto column indexes, or ``None`` if this is not a header."""
    lowered = [re.sub(r"\s+", " ", h or "").strip().lower() for h in header]
    mapping: dict[str, int] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        for index, cell in enumerate(lowered):
            if cell in aliases:
                mapping[field_name] = index
                break
    return mapping if "address" in mapping else None


def parse_delimited(text: str) -> SymbolFile:
    """Any delimited export that carries a header row (TIA CSV, hand-made CSV)."""
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    all_rows = list(csv.reader(io.StringIO(text), dialect))
    if not all_rows:
        raise SymbolImportError("the file is empty")

    header_index = None
    mapping = None
    for index, row in enumerate(all_rows[:20]):
        candidate = _match_headers(row)
        if candidate:
            header_index, mapping = index, candidate
            break
    if mapping is None:
        raise SymbolImportError(
            "no header row found. Expected a column named 'Address' or 'Logical Address' "
            "(a STEP 7 .sdf export has no header - save it with the .sdf extension instead)."
        )

    rows: list[tuple[int, str, str, str, str]] = []
    for offset, row in enumerate(all_rows[header_index + 1 :], start=header_index + 2):
        if not row or all(not cell.strip() for cell in row):
            continue

        def cell(name: str) -> str:
            index = mapping.get(name)  # type: ignore[union-attr]
            return row[index].strip() if index is not None and index < len(row) else ""

        rows.append((offset, cell("name"), cell("address"), cell("data_type"), cell("comment")))
    return _collect(rows, "delimited tag table")


# ----------------------------------------------------------------------
# TIA Portal .xlsx
# ----------------------------------------------------------------------
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column_index(reference: str) -> int:
    """``"AB12"`` -> 27 (zero-based column index)."""
    letters = "".join(ch for ch in reference if ch.isalpha()).upper()
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(index - 1, 0)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    strings: list[str] = []
    for item in root.findall(f"{_SHEET_NS}si"):
        strings.append("".join(node.text or "" for node in item.iter(f"{_SHEET_NS}t")))
    return strings


def _sheet_rows(archive: zipfile.ZipFile, name: str, shared: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(archive.read(name))
    rows: list[list[str]] = []
    for row in root.iter(f"{_SHEET_NS}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{_SHEET_NS}c"):
            index = _column_index(cell.get("r", "A"))
            kind = cell.get("t", "n")
            if kind == "inlineStr":
                text = "".join(n.text or "" for n in cell.iter(f"{_SHEET_NS}t"))
            else:
                value = cell.find(f"{_SHEET_NS}v")
                text = value.text or "" if value is not None else ""
                if kind == "s":
                    try:
                        text = shared[int(text)]
                    except (ValueError, IndexError):
                        text = ""
                elif kind == "b":
                    text = "TRUE" if text == "1" else "FALSE"
            cells[index] = text
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


def parse_xlsx(data: bytes) -> SymbolFile:
    """TIA Portal PLC tag table exported as ``.xlsx``.

    Read with ``zipfile`` and ``xml.etree`` from the standard library: an xlsx
    is a zip of XML, and pulling in a spreadsheet library for four columns is
    not worth the dependency on an air-gapped install. Formulas are not
    evaluated - a tag table exported from TIA contains literal values.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SymbolImportError("not a readable .xlsx file (bad zip archive)") from exc

    with archive:
        shared = _shared_strings(archive)
        sheet_names = sorted(
            n for n in archive.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        if not sheet_names:
            raise SymbolImportError("the workbook contains no worksheets")

        rows: list[tuple[int, str, str, str, str]] = []
        mapping: dict[str, int] | None = None
        row_number = 0
        for sheet in sheet_names:
            for raw_row in _sheet_rows(archive, sheet, shared):
                row_number += 1
                if mapping is None:
                    candidate = _match_headers(raw_row)
                    if candidate:
                        mapping = candidate
                    continue
                if all(not cell.strip() for cell in raw_row):
                    continue

                def cell(name: str, row=raw_row) -> str:
                    index = mapping.get(name)  # type: ignore[union-attr]
                    return row[index].strip() if index is not None and index < len(row) else ""

                rows.append(
                    (row_number, cell("name"), cell("address"), cell("data_type"), cell("comment"))
                )

    if mapping is None:
        raise SymbolImportError(
            "no header row found in the workbook. A TIA PLC tag table export has "
            "'Name', 'Data Type' and 'Logical Address' columns."
        )
    result = _collect(rows, "TIA Portal .xlsx tag table")
    return result


# ----------------------------------------------------------------------
# dispatcher
# ----------------------------------------------------------------------
def _decode_text(data: bytes) -> str:
    """STEP 7 exports are usually Windows-1252; TIA writes UTF-8 (often with BOM)."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def parse_symbol_export(filename: str, data: bytes) -> SymbolFile:
    """Parse an export, choosing the reader from the extension and the content.

    Raises:
        SymbolImportError: when the file is unreadable or the format is unknown.
    """
    if not data:
        raise SymbolImportError("the file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise SymbolImportError(
            f"file is {len(data) // 1024} KiB; the limit is {MAX_FILE_BYTES // 1024} KiB"
        )

    suffix = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""

    if suffix in {"xlsx", "xlsm"} or data[:2] == b"PK":
        return parse_xlsx(data)

    text = _decode_text(data)
    if suffix == "sdf":
        return parse_sdf(text)
    if suffix == "asc":
        return parse_asc(text)
    if suffix in {"csv", "txt", "tsv", "seq", "dif"}:
        try:
            return parse_delimited(text)
        except SymbolImportError:
            # A .csv without a header is most likely an .sdf saved under the
            # wrong extension, which is common enough to be worth retrying.
            return parse_sdf(text)

    # Unknown extension: decide from the content.
    first = next((line for line in text.splitlines() if line.strip()), "")
    if first.lstrip().startswith('"'):
        return parse_sdf(text)
    if _match_headers(next(csv.reader(io.StringIO(first)), [])):
        return parse_delimited(text)
    return parse_asc(text)
