"""Turning a parsed symbol export into rows in the tag table.

The parsers in :mod:`snap7_gateway.plc.symbols` and
:mod:`snap7_gateway.plc.db_layout` decide *what an export says*. This module
decides *what to do about it*, and it is deliberately conservative:

* every entry goes through the same :func:`~snap7_gateway.core.validation.validate_tag`
  the web form uses, so an import cannot introduce a row that the UI would have
  rejected;
* an existing tag is never silently overwritten - the caller has to ask;
* nothing is written until the whole file has been checked, so a bad row halfway
  down cannot leave the tag table half-updated;
* the report names every entry that was not imported and why.

Symbols carry no data of their own: they are names and comments for addresses
the gateway already reads. Importing them changes what an operator sees on the
Tag Mapping page; it does not change what DeviceWise is served, because the
virtual CPU publishes whole memory areas, not named tags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..db.models import PlcConnection
from ..db.store import Database
from ..plc.symbols import SymbolEntry, SymbolFile
from .validation import validate_tag

logger = logging.getLogger(__name__)

#: A single import may not create more tags than this, so a wrong file cannot
#: fill an edge device's database.
MAX_IMPORT_TAGS = 5000


@dataclass(slots=True)
class PlannedTag:
    """One entry and what importing it would do."""

    entry: SymbolEntry
    action: str  # create | update | skip | reject
    reason: str = ""
    existing_id: int | None = None

    @property
    def will_write(self) -> bool:
        return self.action in {"create", "update"}


@dataclass(slots=True)
class ImportPlan:
    """The decision for every row of an export, before anything is written."""

    connection_id: int
    source_format: str
    planned: list[PlannedTag] = field(default_factory=list)
    file_skipped: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: bool = False

    def of_action(self, action: str) -> list[PlannedTag]:
        return [p for p in self.planned if p.action == action]

    @property
    def creates(self) -> int:
        return len(self.of_action("create"))

    @property
    def updates(self) -> int:
        return len(self.of_action("update"))

    @property
    def skips(self) -> int:
        return len(self.of_action("skip"))

    @property
    def rejects(self) -> int:
        return len(self.of_action("reject"))

    @property
    def writes(self) -> int:
        return self.creates + self.updates

    def summary(self) -> str:
        verb = "imported" if self.applied else "would be imported"
        parts = [f"{self.creates} new tag(s) {verb}"]
        if self.updates:
            parts.append(f"{self.updates} updated" if self.applied else f"{self.updates} to update")
        if self.skips:
            parts.append(f"{self.skips} already present, left alone")
        if self.rejects:
            parts.append(f"{self.rejects} rejected")
        if self.file_skipped:
            parts.append(f"{len(self.file_skipped)} not usable from the file")
        return "; ".join(parts)


def plan_import(
    db: Database,
    connection_id: int,
    symbols: SymbolFile,
    *,
    overwrite: bool = False,
) -> ImportPlan:
    """Decide the outcome for every row without touching the database."""
    plan = ImportPlan(connection_id=connection_id, source_format=symbols.format)
    plan.errors.extend(symbols.errors)
    plan.file_skipped.extend(
        (item.name, item.address_text, item.reason) for item in symbols.skipped
    )

    existing = {tag.name.lower(): tag for tag in db.list_tags(connection_id)}
    # Names already spoken for by this file, so two rows cannot both claim one.
    claimed: set[str] = set()

    for entry in symbols.entries:
        values = entry.as_tag_values()
        key = entry.name.lower()

        # Validate against the same rules the web form uses. Duplicate-name
        # checking is done here instead, because "already exists" is a decision
        # (skip or update), not a validation failure.
        _, errors = validate_tag(values)
        if errors:
            plan.planned.append(
                PlannedTag(entry, "reject", "; ".join(f"{k}: {v}" for k, v in errors.items()))
            )
            continue

        if key in claimed:
            plan.planned.append(
                PlannedTag(entry, "reject", "another row in this file already uses this name")
            )
            continue

        current = existing.get(key)
        if current is None:
            claimed.add(key)
            plan.planned.append(PlannedTag(entry, "create"))
            continue

        if not overwrite:
            plan.planned.append(
                PlannedTag(
                    entry,
                    "skip",
                    f"a tag named '{current.name}' already exists at {current.address}; "
                    "tick 'Update existing tags' to replace it",
                    existing_id=current.id,
                )
            )
            continue

        unchanged = (
            str(current.area_type) == entry.area_type
            and current.db_number == entry.db_number
            and current.byte_offset == entry.byte_offset
            and current.bit_offset == entry.bit_offset
            and current.data_type == entry.data_type
            and current.length == entry.length
            and (current.description or "") == (values.get("description") or "")
        )
        if unchanged:
            plan.planned.append(
                PlannedTag(entry, "skip", "already present and identical", existing_id=current.id)
            )
        else:
            claimed.add(key)
            plan.planned.append(
                PlannedTag(
                    entry,
                    "update",
                    f"replaces {current.address} {current.data_type}",
                    existing_id=current.id,
                )
            )

    if plan.writes > MAX_IMPORT_TAGS:
        plan.errors.append(
            f"this file would write {plan.writes} tags; the limit for one import is "
            f"{MAX_IMPORT_TAGS}. Split the export or import one block at a time."
        )
    return plan


def apply_import(
    db: Database,
    connection: PlcConnection,
    plan: ImportPlan,
    *,
    actor: str = "system",
    ip: str | None = None,
) -> ImportPlan:
    """Write a plan to the tag table and record it in the audit trail.

    Returns the same plan with ``applied`` set. Rows that fail to write are
    turned into rejections, so the report always matches what is in the
    database afterwards.
    """
    if plan.writes > MAX_IMPORT_TAGS:
        raise ValueError(
            f"refusing to import {plan.writes} tags; the limit is {MAX_IMPORT_TAGS}"
        )

    created = updated = 0
    for item in plan.planned:
        if not item.will_write:
            continue
        values = item.entry.as_tag_values()
        try:
            if item.action == "create":
                db.create_tag(connection.id, values)
                created += 1
            else:
                assert item.existing_id is not None
                db.update_tag(item.existing_id, values)
                updated += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the rest
            logger.warning(
                "could not import tag '%s' on connection %s: %s",
                item.entry.name,
                connection.id,
                exc,
            )
            item.action = "reject"
            item.reason = f"database rejected this row: {exc}"

    plan.applied = True
    db.audit(
        actor,
        "tags.import",
        connection.name,
        {
            "format": plan.source_format,
            "created": created,
            "updated": updated,
            "skipped": plan.skips,
            "rejected": plan.rejects,
            "unusable_rows": len(plan.file_skipped),
        },
        ip,
    )
    logger.info(
        "symbol import on connection %s (%s): %d created, %d updated",
        connection.id,
        connection.name,
        created,
        updated,
    )
    return plan
