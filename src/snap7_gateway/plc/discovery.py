"""Block discovery and rescan diffing (CLAUDE.md sections 4.6 and 4.7).

The virtual S7 CPU can only present areas the gateway explicitly registers, so
the sizes it registers must match the real PLC exactly - otherwise DeviceWise's
auto-enumeration shows placeholder shapes instead of the real
``DB1 UINT1[20304]``-style tree. Discovery is therefore the source of truth:

1. ``ListBlocks``/``ListBlocksOfType`` enumerate the DB numbers on the CPU;
2. ``GetAgBlockInfo`` gives each DB's exact ``MC7Size``;
3. I/Q/M have no block info, so their usable size is probed (see
   :meth:`~snap7_gateway.plc.client.PlcClient.probe_area_size`).

Rescans never mutate exposure decisions and never deregister anything on their
own: a shrunk or vanished block is *flagged* for the operator, because a live
DeviceWise consumer may still be querying the old shape.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Sequence

from ..db.models import AreaStatus, AreaType, ExposureMode, PlcArea, PlcConnection
from ..db.store import Database
from .client import PlcClient, PlcError

logger = logging.getLogger(__name__)


class ChangeKind(StrEnum):
    """What a rescan observed about one area relative to the stored state."""

    ADDED = "added"
    GROWN = "grown"
    SHRUNK = "shrunk"
    MISSING = "missing"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class DiscoveredArea:
    """One memory area seen on the real PLC during a discovery pass."""

    area_type: str
    db_number: int
    size_bytes: int

    @property
    def key(self) -> str:
        return f"DB{self.db_number}" if self.area_type == AreaType.DB else str(self.area_type)


@dataclass(slots=True)
class DiscoveryResult:
    """Outcome of one discovery pass against a single connection."""

    connection_id: int
    areas: list[DiscoveredArea] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        """True when at least one area was found and nothing fatal happened."""
        return bool(self.areas) and not self.fatal

    @property
    def fatal(self) -> bool:
        """A pass that found nothing at all is treated as a failure."""
        return not self.areas and bool(self.errors)

    def by_key(self) -> dict[str, DiscoveredArea]:
        return {a.key: a for a in self.areas}


@dataclass(frozen=True, slots=True)
class AreaChange:
    """A single line in the discovery diff written to the audit trail."""

    kind: str
    key: str
    area_type: str
    db_number: int
    old_size: int | None
    new_size: int | None

    def describe(self) -> str:
        if self.kind == ChangeKind.ADDED:
            return f"{self.key} added ({self.new_size} bytes)"
        if self.kind == ChangeKind.MISSING:
            return f"{self.key} no longer present on the PLC (was {self.old_size} bytes)"
        if self.kind in (ChangeKind.GROWN, ChangeKind.SHRUNK):
            return f"{self.key} {self.kind} {self.old_size} -> {self.new_size} bytes"
        return f"{self.key} unchanged"


@dataclass(slots=True)
class DiscoveryDiff:
    """Aggregated changes produced by :func:`apply_discovery`."""

    connection_id: int
    changes: list[AreaChange] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[AreaChange]:
        return [c for c in self.changes if c.kind == kind]

    @property
    def significant(self) -> list[AreaChange]:
        return [c for c in self.changes if c.kind != ChangeKind.UNCHANGED]

    @property
    def is_empty(self) -> bool:
        return not self.significant

    def summary(self) -> str:
        if self.is_empty:
            return "no changes"
        counts: dict[str, int] = {}
        for change in self.significant:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        return ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))


def discover(
    client: PlcClient, connection: PlcConnection, *, max_db_count: int = 2048
) -> DiscoveryResult:
    """Enumerate the areas of one connected PLC.

    The client must already be connected. Individual failures are collected
    into ``errors`` rather than raised, so one unreadable DB cannot abort the
    whole pass - a partial map is far more useful on the factory floor than
    none at all.
    """
    started = time.perf_counter()
    result = DiscoveryResult(connection_id=connection.id)

    try:
        db_numbers = client.list_db_numbers(max_count=max_db_count)
    except PlcError as exc:
        result.errors.append(exc.detail)
        db_numbers = []

    for db_number in db_numbers:
        try:
            size = client.get_db_size(db_number)
        except PlcError as exc:
            result.errors.append(f"DB{db_number}: {exc.detail}")
            continue
        if size <= 0:
            result.errors.append(f"DB{db_number}: reported zero size, skipped")
            continue
        result.areas.append(DiscoveredArea(AreaType.DB, db_number, size))

    probes: Sequence[tuple[str, bool, int]] = (
        (AreaType.INPUT, connection.read_inputs, connection.max_input_bytes),
        (AreaType.OUTPUT, connection.read_outputs, connection.max_output_bytes),
        (AreaType.MERKER, connection.read_merkers, connection.max_merker_bytes),
    )
    for area_type, enabled, max_bytes in probes:
        if not enabled or max_bytes <= 0:
            continue
        try:
            size = client.probe_area_size(area_type, max_bytes)
        except PlcError as exc:  # pragma: no cover - probe swallows PlcError itself
            result.errors.append(f"{area_type}: {exc.detail}")
            continue
        if size > 0:
            result.areas.append(DiscoveredArea(area_type, 0, size))
        else:
            result.errors.append(f"{area_type}: area not readable on this CPU, skipped")

    result.duration_ms = (time.perf_counter() - started) * 1000.0
    result.timestamp = time.time()
    return result


def diff_discovery(existing: Iterable[PlcArea], result: DiscoveryResult) -> list[AreaChange]:
    """Compare stored areas against a fresh discovery pass.

    Pure function - no database writes - so the rescan preview shown in the web
    UI and the applied diff always agree.
    """
    stored = {area.key: area for area in existing}
    found = result.by_key()
    changes: list[AreaChange] = []

    for key, area in found.items():
        previous = stored.get(key)
        if previous is None:
            changes.append(
                AreaChange(ChangeKind.ADDED, key, area.area_type, area.db_number, None, area.size_bytes)
            )
        elif area.size_bytes > previous.size_bytes:
            changes.append(
                AreaChange(
                    ChangeKind.GROWN, key, area.area_type, area.db_number,
                    previous.size_bytes, area.size_bytes,
                )
            )
        elif area.size_bytes < previous.size_bytes:
            changes.append(
                AreaChange(
                    ChangeKind.SHRUNK, key, area.area_type, area.db_number,
                    previous.size_bytes, area.size_bytes,
                )
            )
        else:
            changes.append(
                AreaChange(
                    ChangeKind.UNCHANGED, key, area.area_type, area.db_number,
                    previous.size_bytes, area.size_bytes,
                )
            )

    for key, area in stored.items():
        if key not in found:
            changes.append(
                AreaChange(
                    ChangeKind.MISSING, key, area.area_type, area.db_number, area.size_bytes, None
                )
            )

    changes.sort(key=lambda c: (c.area_type, c.db_number))
    return changes


_STATUS_FOR_KIND: dict[str, str] = {
    ChangeKind.ADDED: AreaStatus.NEW,
    ChangeKind.GROWN: AreaStatus.GROWN,
    ChangeKind.SHRUNK: AreaStatus.SHRUNK,
    ChangeKind.UNCHANGED: AreaStatus.OK,
}


def apply_discovery(
    db: Database,
    connection: PlcConnection,
    result: DiscoveryResult,
    *,
    actor: str = "system",
) -> DiscoveryDiff:
    """Persist a discovery pass and return the diff.

    Rules enforced here (CLAUDE.md section 4.7):

    * Mirror All - every discovered area is stored already exposed.
    * Whitelist  - a newly discovered area is stored **unexposed**; an operator
      must whitelist it deliberately. Existing exposure flags are never touched.
    * A missing or shrunk area is flagged in ``status`` and left registered; the
      operator decides, because DeviceWise may still be reading the old shape.
    * Every non-trivial change is written to the audit trail.
    """
    if result.fatal:
        logger.warning(
            "discovery for connection %s produced no areas: %s",
            connection.id,
            "; ".join(result.errors) or "unknown error",
        )
        return DiscoveryDiff(connection_id=connection.id)

    existing = db.list_areas(connection.id)
    changes = diff_discovery(existing, result)
    mirror_all = connection.exposure_mode == ExposureMode.MIRROR_ALL
    found = result.by_key()

    for change in changes:
        if change.kind == ChangeKind.MISSING:
            area = next((a for a in existing if a.key == change.key), None)
            if area is not None:
                db.set_area_status(
                    area.id,
                    AreaStatus.MISSING,
                    f"not present in the rescan at {_stamp(result.timestamp)}; "
                    "left registered so live consumers keep their shape",
                )
            continue

        discovered = found[change.key]
        detail = None
        if change.kind == ChangeKind.SHRUNK:
            detail = (
                f"shrunk {change.old_size} -> {change.new_size} bytes; "
                "registration keeps the previous size until an operator confirms"
            )
        elif change.kind == ChangeKind.GROWN:
            detail = f"grew {change.old_size} -> {change.new_size} bytes"
        db.upsert_area(
            connection.id,
            discovered.area_type,
            discovered.db_number,
            discovered.size_bytes,
            status=_STATUS_FOR_KIND[change.kind],
            status_detail=detail,
            default_exposed=mirror_all,
        )
        # Mirror All means new areas go live immediately; existing flags are
        # left alone so an operator's explicit "do not expose" survives.
        if mirror_all and change.kind == ChangeKind.ADDED:
            area = db.get_area(connection.id, discovered.area_type, discovered.db_number)
            if area is not None and not area.exposed:
                db.set_area_exposed(area.id, True)

    diff = DiscoveryDiff(connection_id=connection.id, changes=changes)

    for change in diff.significant:
        db.audit(
            actor,
            f"discovery.{change.kind}",
            f"{connection.name}/{change.key}",
            change.describe(),
        )
    if result.errors:
        db.audit(
            actor,
            "discovery.partial",
            connection.name,
            "; ".join(result.errors[:20]),
        )
    logger.info(
        "discovery for connection %s (%s): %s in %.0f ms",
        connection.id,
        connection.name,
        diff.summary(),
        result.duration_ms,
    )
    return diff


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
