"""Rescan diffing and the rules that protect a live DeviceWise consumer."""

from __future__ import annotations

import time

from snap7_gateway.db.models import AreaStatus, AreaType, ExposureMode
from snap7_gateway.plc.discovery import (
    ChangeKind,
    DiscoveredArea,
    DiscoveryResult,
    apply_discovery,
    diff_discovery,
)


def make_connection(db, **overrides):
    values = {
        "name": "CNC1",
        "host": "10.0.0.5",
        "rack": 0,
        "slot": 2,
        "tcp_port": 102,
        "connection_type": "PG",
        "timeout_ms": 3000,
        "poll_interval_ms": 1000,
        "exposure_mode": ExposureMode.WHITELIST.value,
        "write_enabled": False,
        "enabled": True,
        "read_inputs": True,
        "read_outputs": True,
        "read_merkers": True,
        "max_input_bytes": 1024,
        "max_output_bytes": 1024,
        "max_merker_bytes": 8192,
    }
    values.update(overrides)
    return db.create_connection(values)


def result_with(connection_id: int, areas: list[tuple[str, int, int]]) -> DiscoveryResult:
    return DiscoveryResult(
        connection_id=connection_id,
        areas=[DiscoveredArea(a, n, s) for a, n, s in areas],
        timestamp=time.time(),
    )


class TestDiff:
    def test_everything_new_on_a_first_scan(self) -> None:
        result = result_with(1, [("DB", 1, 20304), ("DB", 2, 810), ("M", 0, 8192)])
        changes = diff_discovery([], result)
        assert {c.kind for c in changes} == {ChangeKind.ADDED}
        assert {c.key for c in changes} == {"DB1", "DB2", "M"}

    def test_detects_growth_shrinkage_and_removal(self, db) -> None:
        connection = make_connection(db)
        db.upsert_area(connection.id, AreaType.DB, 1, 100, status=AreaStatus.OK)
        db.upsert_area(connection.id, AreaType.DB, 2, 100, status=AreaStatus.OK)
        db.upsert_area(connection.id, AreaType.DB, 3, 100, status=AreaStatus.OK)

        result = result_with(connection.id, [("DB", 1, 200), ("DB", 2, 50), ("DB", 4, 16)])
        changes = {c.key: c for c in diff_discovery(db.list_areas(connection.id), result)}

        assert changes["DB1"].kind == ChangeKind.GROWN
        assert changes["DB2"].kind == ChangeKind.SHRUNK
        assert changes["DB3"].kind == ChangeKind.MISSING
        assert changes["DB4"].kind == ChangeKind.ADDED

    def test_unchanged_is_reported_too(self, db) -> None:
        connection = make_connection(db)
        db.upsert_area(connection.id, AreaType.DB, 1, 100, status=AreaStatus.OK)
        changes = diff_discovery(
            db.list_areas(connection.id), result_with(connection.id, [("DB", 1, 100)])
        )
        assert changes[0].kind == ChangeKind.UNCHANGED

    def test_diff_is_pure(self, db) -> None:
        """The preview must not write anything - the UI shows it before applying."""
        connection = make_connection(db)
        db.upsert_area(connection.id, AreaType.DB, 1, 100, status=AreaStatus.OK)
        before = [(a.id, a.size_bytes, a.status) for a in db.list_areas(connection.id)]
        diff_discovery(db.list_areas(connection.id), result_with(connection.id, [("DB", 1, 999)]))
        assert [(a.id, a.size_bytes, a.status) for a in db.list_areas(connection.id)] == before


class TestApplyWhitelistMode:
    def test_new_areas_are_never_exposed_automatically(self, db) -> None:
        connection = make_connection(db, exposure_mode=ExposureMode.WHITELIST.value)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64), ("M", 0, 128)]))
        areas = db.list_areas(connection.id)
        assert areas and all(not a.exposed for a in areas)
        assert all(a.status == AreaStatus.NEW for a in areas)

    def test_operator_exposure_survives_a_rescan(self, db) -> None:
        connection = make_connection(db)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        area = db.get_area(connection.id, AreaType.DB, 1)
        db.set_area_exposed(area.id, True)

        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        assert db.get_area(connection.id, AreaType.DB, 1).exposed is True

    def test_a_missing_block_is_flagged_not_deleted(self, db) -> None:
        connection = make_connection(db)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64), ("DB", 2, 16)]))
        db.set_area_exposed(db.get_area(connection.id, AreaType.DB, 2).id, True)

        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        gone = db.get_area(connection.id, AreaType.DB, 2)
        assert gone is not None, "a vanished block must not be silently deleted"
        assert gone.status == AreaStatus.MISSING
        assert gone.exposed is True
        assert "left registered" in (gone.status_detail or "")

    def test_a_shrunk_block_is_flagged(self, db) -> None:
        connection = make_connection(db)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 1000)]))
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 100)]))
        area = db.get_area(connection.id, AreaType.DB, 1)
        assert area.status == AreaStatus.SHRUNK
        assert "1000 -> 100" in (area.status_detail or "")


class TestApplyMirrorAllMode:
    def test_every_discovered_area_is_exposed(self, db) -> None:
        connection = make_connection(db, exposure_mode=ExposureMode.MIRROR_ALL.value)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64), ("Q", 0, 32)]))
        assert all(a.exposed for a in db.list_areas(connection.id))

    def test_a_deliberate_hide_is_not_re_exposed_by_a_rescan(self, db) -> None:
        connection = make_connection(db, exposure_mode=ExposureMode.MIRROR_ALL.value)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        db.set_area_exposed(db.get_area(connection.id, AreaType.DB, 1).id, False)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        assert db.get_area(connection.id, AreaType.DB, 1).exposed is False


class TestAuditTrail:
    def test_every_change_is_recorded(self, db) -> None:
        connection = make_connection(db)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 32), ("DB", 9, 8)]))
        actions = [entry.action for entry in db.list_audit()]
        assert "discovery.added" in actions
        assert "discovery.shrunk" in actions

    def test_partial_failures_are_recorded(self, db) -> None:
        connection = make_connection(db)
        result = result_with(connection.id, [("DB", 1, 64)])
        result.errors.append("DB7: read refused")
        apply_discovery(db, connection, result)
        assert any(entry.action == "discovery.partial" for entry in db.list_audit())

    def test_a_totally_failed_scan_changes_nothing(self, db) -> None:
        connection = make_connection(db)
        apply_discovery(db, connection, result_with(connection.id, [("DB", 1, 64)]))
        failed = DiscoveryResult(connection_id=connection.id, errors=["connection reset"])
        diff = apply_discovery(db, connection, failed)
        assert diff.is_empty
        assert db.get_area(connection.id, AreaType.DB, 1).status != AreaStatus.MISSING
