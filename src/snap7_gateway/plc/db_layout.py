"""Working out where each member of a data block actually lives.

A symbol table gives absolute addresses (``DB10.DBW4``) and needs no
arithmetic. A data block's *declaration* does not: TIA and STEP 7 export the
member list in order, and the byte offsets follow from the S7 layout rules.
This module applies those rules so an operator can import a whole DB's
structure with its comments instead of typing offsets by hand.

The rules implemented are those of **standard (non-optimized) block access**,
which is the only mode Snap7 can address by offset at all:

* ``BOOL`` packs eight to a byte, in declaration order;
* any non-bit member first closes a partially used byte;
* two-byte and larger scalars start on an **even** byte (S7 alignment is
  word-based, not size-based - ``REAL`` aligns to 2, not to 4);
* ``STRING[n]`` occupies ``n + 2`` bytes and starts on an even byte;
* ``ARRAY`` and ``STRUCT`` start on an even byte and occupy an even number of
  bytes;
* the block's total length is rounded up to an even number.

If a source says ``S7_Optimized_Access := 'TRUE'`` the members have no fixed
offsets at all and the import is refused with that explanation, rather than
producing numbers that would silently read the wrong bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from .s7types import TypeInfo, map_declared_type, normalize_declared

if TYPE_CHECKING:  # imported lazily at runtime to avoid a circular import
    from .symbols import SymbolFile

#: Nested members are named ``Parent.Child`` so the origin stays readable.
PATH_SEPARATOR = "."

#: Guard against a pathological or hand-edited source.
MAX_MEMBERS = 20_000
MAX_NESTING = 16


class DbSourceError(ValueError):
    """The data-block source could not be read, or its offsets are meaningless."""


@dataclass(slots=True)
class MemberNode:
    """One declared member, before offsets are known."""

    name: str
    datatype: str
    comment: str = ""
    children: list["MemberNode"] = field(default_factory=list)

    @property
    def is_struct(self) -> bool:
        return bool(self.children) or normalize_declared(self.datatype) == "STRUCT"


@dataclass(slots=True)
class DbMember:
    """One member with its resolved position inside the block."""

    path: str
    declared: str
    byte_offset: int
    bit_offset: int
    size_bytes: int
    comment: str = ""
    data_type: str | None = None
    length: int = 1
    note: str = ""
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.data_type is not None

    def address(self, db_number: int) -> str:
        """Siemens-style address, or a plain byte reference for a non-scalar.

        A STRUCT or ARRAY has no single value, so writing ``DB12.DBD4`` for one
        would imply a reading that does not exist; those get ``DB12.DBB4+size``.
        """
        if self.data_type == "BOOL":
            return f"DB{db_number}.DBX{self.byte_offset}.{self.bit_offset}"
        if not self.supported:
            return f"DB{db_number}.DBB{self.byte_offset}+{self.size_bytes}"
        suffix = {1: "B", 2: "W", 4: "D"}.get(self.size_bytes, "B")
        return f"DB{db_number}.DB{suffix}{self.byte_offset}"


@dataclass(slots=True)
class DbLayout:
    """A parsed data block: its identity, its members and any caveats."""

    db_number: int
    db_name: str = ""
    members: list[DbMember] = field(default_factory=list)
    total_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    optimized: bool | None = None

    @property
    def supported_members(self) -> list[DbMember]:
        return [m for m in self.members if m.supported]

    def summary(self) -> str:
        return (
            f"DB{self.db_number}"
            + (f" ({self.db_name})" if self.db_name else "")
            + f": {len(self.supported_members)} readable member(s) of {len(self.members)}, "
            f"{self.total_bytes} bytes"
        )


# ----------------------------------------------------------------------
# the layout walk
# ----------------------------------------------------------------------
class _Cursor:
    """Tracks the current byte and bit position while walking members."""

    __slots__ = ("byte", "bit")

    def __init__(self) -> None:
        self.byte = 0
        self.bit = 0

    def close_byte(self) -> None:
        """Finish a partially used byte - any non-bit member starts after it."""
        if self.bit:
            self.byte += 1
            self.bit = 0

    def align(self, alignment: int) -> None:
        self.close_byte()
        if alignment >= 2 and self.byte % 2:
            self.byte += 1

    def take_bit(self) -> tuple[int, int]:
        position = (self.byte, self.bit)
        self.bit += 1
        if self.bit > 7:
            self.bit = 0
            self.byte += 1
        return position

    def take_bytes(self, size: int, alignment: int) -> int:
        self.align(alignment)
        start = self.byte
        self.byte += size
        return start


def compute_layout(
    nodes: list[MemberNode], *, db_number: int = 0, db_name: str = ""
) -> DbLayout:
    """Resolve declaration order into byte/bit offsets."""
    layout = DbLayout(db_number=db_number, db_name=db_name)
    cursor = _Cursor()
    _walk(nodes, cursor, layout, prefix="", depth=0)
    cursor.close_byte()
    if cursor.byte % 2:
        cursor.byte += 1
    layout.total_bytes = cursor.byte
    return layout


def _walk(
    nodes: list[MemberNode], cursor: _Cursor, layout: DbLayout, *, prefix: str, depth: int
) -> None:
    if depth > MAX_NESTING:
        layout.warnings.append(
            f"stopped at nesting depth {MAX_NESTING}; deeper members were not imported"
        )
        return

    for node in nodes:
        if len(layout.members) >= MAX_MEMBERS:
            layout.warnings.append(
                f"stopped after {MAX_MEMBERS} members; the rest were not imported"
            )
            return

        path = f"{prefix}{PATH_SEPARATOR}{node.name}" if prefix else node.name

        if node.is_struct:
            # A nested structure starts on an even byte and is padded to one.
            cursor.align(2)
            start = cursor.byte
            # Listed before its children so the tree reads in declaration order;
            # its size is only known once the children have been walked.
            container = DbMember(
                path=path,
                declared="Struct",
                byte_offset=start,
                bit_offset=0,
                size_bytes=0,
                comment=node.comment,
                reason="structures have no single value; their members are imported instead",
            )
            layout.members.append(container)
            _walk(node.children, cursor, layout, prefix=path, depth=depth + 1)
            cursor.close_byte()
            if cursor.byte % 2:
                cursor.byte += 1
            container.size_bytes = cursor.byte - start
            continue

        info: TypeInfo = map_declared_type(node.datatype)
        normalized = normalize_declared(node.datatype)

        if normalized.startswith("ARRAY"):
            # The size already accounts for the element type and count.
            start = cursor.take_bytes(info.size_bytes, 2)
            layout.members.append(
                DbMember(
                    path=path,
                    declared=node.datatype,
                    byte_offset=start,
                    bit_offset=0,
                    size_bytes=info.size_bytes,
                    comment=node.comment,
                    reason=info.reason,
                )
            )
            continue

        if info.is_bit:
            byte_offset, bit_offset = cursor.take_bit()
            layout.members.append(
                DbMember(
                    path=path,
                    declared=node.datatype,
                    byte_offset=byte_offset,
                    bit_offset=bit_offset,
                    size_bytes=1,
                    comment=node.comment,
                    data_type=info.data_type,
                    length=info.length,
                    note=info.note,
                    reason=info.reason,
                )
            )
            continue

        start = cursor.take_bytes(info.size_bytes, info.alignment)
        if not info.supported and info.reason.startswith("unrecognised data type"):
            layout.warnings.append(
                f"{path}: {info.reason}; offsets after this member may be wrong"
            )
        layout.members.append(
            DbMember(
                path=path,
                declared=node.datatype,
                byte_offset=start,
                bit_offset=0,
                size_bytes=info.size_bytes,
                comment=node.comment,
                data_type=info.data_type,
                length=info.length,
                note=info.note,
                reason=info.reason,
            )
        )


# ----------------------------------------------------------------------
# TIA Portal XML export
# ----------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_comment(element: ElementTree.Element) -> str:
    """Pull the comment text out of a TIA ``<Comment>`` child, if present."""
    for child in element:
        if _local(child.tag) != "Comment":
            continue
        texts = [node.text or "" for node in child.iter() if _local(node.tag) == "MultiLanguageText"]
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        if joined:
            return joined
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _xml_members(element: ElementTree.Element) -> list[MemberNode]:
    nodes: list[MemberNode] = []
    for child in element:
        if _local(child.tag) != "Member":
            continue
        nodes.append(
            MemberNode(
                name=child.get("Name", "").strip(),
                datatype=child.get("Datatype", "").strip(),
                comment=_xml_comment(child),
                children=_xml_members(child),
            )
        )
    return nodes


def parse_tia_db_xml(text: str) -> DbLayout:
    """Parse a TIA Portal data-block XML export.

    Handles both the Openness document layout and the simpler
    ``Sections/Section/Member`` form. Offsets are computed from the declaration
    order rather than read from the file, because the ``Offset`` attribute is
    absent from most exports and present only for optimized blocks, where it is
    not a byte offset at all.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise DbSourceError(f"not valid XML: {exc}") from exc

    db_number = 0
    db_name = ""
    optimized: bool | None = None

    for element in root.iter():
        name = _local(element.tag)
        value = (element.text or "").strip()
        if name == "Number" and value.isdigit() and not db_number:
            db_number = int(value)
        elif name == "Name" and value and not db_name:
            db_name = value
        elif name == "MemoryLayout" and value:
            optimized = value.strip().lower() == "optimized"
        elif name == "BooleanAttribute" and element.get("Name") == "Optimized":
            optimized = value.strip().lower() == "true"

    sections = [e for e in root.iter() if _local(e.tag) == "Section"]
    nodes: list[MemberNode] = []
    for section in sections:
        section_name = (section.get("Name") or "").strip().lower()
        # A global DB's data lives in the Static section; Input/Output/Temp
        # belong to function blocks and are not addressable in a DB read.
        if section_name and section_name not in {"static", "none", ""}:
            continue
        nodes.extend(_xml_members(section))
    if not nodes:
        nodes = _xml_members(root)
        if not nodes:
            for element in root.iter():
                nodes = _xml_members(element)
                if nodes:
                    break

    if not nodes:
        raise DbSourceError(
            "no <Member> elements found. Export the block with "
            "right-click > Generate source from blocks, or via TIA Openness."
        )

    if optimized:
        raise DbSourceError(
            f"DB{db_number or '?'} uses optimized block access, so its members have no fixed "
            "byte offsets and Snap7 cannot address them. In TIA Portal, clear "
            "'Optimized block access' in the block's properties, recompile and download, "
            "then export again."
        )

    layout = compute_layout(nodes, db_number=db_number, db_name=db_name)
    layout.optimized = optimized
    if not db_number:
        layout.warnings.append(
            "the export does not state a DB number; choose it on the import form"
        )
    return layout


# ----------------------------------------------------------------------
# STEP 7 / TIA textual source (.db, .scl, .awl)
# ----------------------------------------------------------------------
_DB_HEADER = re.compile(
    r'DATA_BLOCK\s+(?:"(?P<quoted>[^"]+)"|DB\s*(?P<number>\d+)|(?P<bare>\w+))', re.IGNORECASE
)
_DB_NUMBER_ATTR = re.compile(r"\bDB\s*(?P<number>\d+)\b", re.IGNORECASE)
_OPTIMIZED = re.compile(
    r"S7_Optimized_Access\s*:=\s*'(?P<value>TRUE|FALSE)'", re.IGNORECASE
)
#: ``Name : Type := init;   // comment``  - the initial value and the trailing
#: semicolon are both optional in the wild.
_DECL = re.compile(
    r'^\s*(?:"(?P<qname>[^"]+)"|(?P<name>[A-Za-z_][\w$]*))\s*'
    r"(?:\{[^}]*\}\s*)?"          # per-member attributes, e.g. { S7_HMI... }
    r":\s*(?P<type>[^;:=]+?)\s*"
    r"(?::=\s*(?P<init>[^;]*?))?\s*;?\s*"
    r"(?://(?P<comment>.*)|\(\*(?P<block_comment>.*?)\*\))?\s*$"
)


def parse_db_source(text: str) -> DbLayout:
    """Parse a textual data-block source (``.db`` / ``.scl`` / ``.awl``).

    Only the declaration part is read - everything from ``BEGIN`` onwards is
    initial values, which say nothing about layout.
    """
    optimized_match = _OPTIMIZED.search(text)
    optimized = optimized_match.group("value").upper() == "TRUE" if optimized_match else None
    if optimized:
        raise DbSourceError(
            "this block uses optimized block access (S7_Optimized_Access := 'TRUE'), so its "
            "members have no fixed byte offsets and Snap7 cannot address them. Clear "
            "'Optimized block access' in the block's properties, recompile and download, "
            "then export again."
        )

    db_number = 0
    db_name = ""
    header = _DB_HEADER.search(text)
    if header:
        if header.group("number"):
            db_number = int(header.group("number"))
        db_name = (header.group("quoted") or header.group("bare") or "").strip()
    if not db_number:
        # TIA sources name the block and state its number separately.
        for line in text.splitlines()[:40]:
            if "DATA_BLOCK" in line.upper() or "BLOCK_NUMBER" in line.upper():
                found = _DB_NUMBER_ATTR.search(line)
                if found:
                    db_number = int(found.group("number"))
                    break

    lines = text.splitlines()
    # Declarations sit between the first STRUCT and BEGIN / END_DATA_BLOCK.
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^\s*STRUCT\b", line, re.IGNORECASE):
            start = index + 1
            break
    if start is None:
        raise DbSourceError(
            "no STRUCT declaration found. Export the block as a source "
            "(right-click > Generate source from blocks)."
        )
    end = len(lines)
    for index in range(start, len(lines)):
        if re.match(r"^\s*(BEGIN|END_DATA_BLOCK)\b", lines[index], re.IGNORECASE):
            end = index
            break

    root_nodes: list[MemberNode] = []
    stack: list[list[MemberNode]] = [root_nodes]
    pending_struct: list[MemberNode] = []

    for raw in lines[start:end]:
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if re.match(r"^END_STRUCT\s*;?\s*(//.*)?$", line, re.IGNORECASE):
            if len(stack) > 1:
                stack.pop()
                pending_struct.pop()
            continue

        match = _DECL.match(line)
        if not match:
            continue
        name = (match.group("qname") or match.group("name") or "").strip()
        declared = (match.group("type") or "").strip().strip('"')
        comment = (match.group("comment") or match.group("block_comment") or "").strip()
        if not name:
            continue

        if re.match(r"^STRUCT\b", declared, re.IGNORECASE):
            node = MemberNode(name=name, datatype="Struct", comment=comment)
            stack[-1].append(node)
            stack.append(node.children)
            pending_struct.append(node)
            continue

        stack[-1].append(MemberNode(name=name, datatype=declared, comment=comment))

    if not root_nodes:
        raise DbSourceError("the source declares no members")

    layout = compute_layout(root_nodes, db_number=db_number, db_name=db_name)
    layout.optimized = optimized
    if not db_number:
        layout.warnings.append(
            "the source does not state a DB number; choose it on the import form"
        )
    return layout


def parse_db_export(filename: str, data: bytes) -> DbLayout:
    """Parse a data-block export, choosing the reader from extension and content."""
    text = data.decode("utf-8-sig", errors="replace") if data[:1] != b"<" else data.decode(
        "utf-8-sig", errors="replace"
    )
    suffix = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if suffix == "xml" or text.lstrip().startswith("<"):
        return parse_tia_db_xml(text)
    return parse_db_source(text)


def layout_to_symbols(
    layout: DbLayout, *, db_number: int | None = None, prefix: str = ""
) -> "SymbolFile":
    """Convert a parsed data block into importable symbols.

    ``db_number`` overrides the number found in the export, which matters when
    the file does not state one. ``prefix`` is prepended to every member name so
    two blocks with a member called ``Speed`` do not collide in the tag table.
    """
    from .symbols import SkippedSymbol, SymbolEntry, SymbolFile, sanitize_name

    number = db_number if db_number is not None else layout.db_number
    result = SymbolFile(format=f"data block DB{number} declaration")
    result.errors.extend(layout.warnings)
    taken: set[str] = set()

    if not number:
        result.errors.append("no DB number: nothing can be addressed without it")
        return result

    for index, member in enumerate(layout.members, start=1):
        display = f"{prefix}{member.path}" if prefix else member.path
        if not member.supported:
            result.skipped.append(
                SkippedSymbol(display, member.address(number), member.reason, index)
            )
            continue
        result.entries.append(
            SymbolEntry(
                name=sanitize_name(display, taken=taken),
                address_text=member.address(number),
                area_type="DB",
                db_number=number,
                byte_offset=member.byte_offset,
                bit_offset=member.bit_offset,
                data_type=str(member.data_type),
                length=member.length,
                comment=member.comment,
                original_name=display,
                note=member.note,
                source_row=index,
            )
        )
    return result
