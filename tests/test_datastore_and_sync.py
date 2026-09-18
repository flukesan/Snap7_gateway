"""Datastore freshness and the virtual CPU's registration policy."""

from __future__ import annotations

import time

import pytest

from snap7_gateway.core.datastore import AreaKey
from snap7_gateway.db.models import AreaStatus, AreaType, ExposureMode
from snap7_gateway.virtual_plc.server import VirtualPlcServer
from snap7_gateway.virtual_plc.sync import AreaSyncTask

from .test_discovery import make_connection


KEY = AreaKey(1, "DB", 1)


class TestDataStore:
    def test_publish_and_read_back(self, store) -> None:
        store.publish(KEY, b"\x01\x02\x03")
        snapshot = store.get(KEY)
        assert snapshot.data == b"\x01\x02\x03" and snapshot.ok and snapshot.size == 3

    def test_failure_marks_the_entry_bad_without_losing_diagnostics(self, store) -> None:
        store.publish(KEY, b"\x01\x02")
        store.publish_failure(KEY, "timeout")
        snapshot = store.get(KEY)
        assert snapshot.ok is False and snapshot.error == "timeout"
        assert snapshot.data == b"\x01\x02"  # kept for diagnostics only

    def test_fresh_requires_both_success_and_recency(self, store) -> None:
        store.publish(KEY, b"\x01", read_at=time.time() - 100)
        assert store.fresh(KEY, 30) is None
        store.publish(KEY, b"\x01")
        assert store.fresh(KEY, 30) is not None
        store.publish_failure(KEY, "boom")
        assert store.fresh(KEY, 30) is None

    def test_retain_drops_unconfigured_areas(self, store) -> None:
        store.publish(AreaKey(1, "DB", 1), b"\x01")
        store.publish(AreaKey(1, "DB", 2), b"\x02")
        store.publish(AreaKey(2, "M", 0), b"\x03")
        dropped = store.retain({AreaKey(1, "DB", 1)})
        assert dropped == 2 and store.keys() == [AreaKey(1, "DB", 1)]

    def test_store_is_bounded_by_configuration_not_uptime(self, store) -> None:
        for _ in range(5000):
            store.publish(KEY, b"\x01\x02\x03")
        assert len(store.keys()) == 1

    def test_dropping_a_connection_clears_its_areas(self, store) -> None:
        store.publish(AreaKey(1, "DB", 1), b"\x01")
        store.publish(AreaKey(2, "DB", 1), b"\x02")
        store.drop_connection(1)
        assert store.keys() == [AreaKey(2, "DB", 1)]


@pytest.fixture()
def vplc():
    from .conftest import free_port

    server = VirtualPlcServer(bind_ip="127.0.0.1", port=free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


class TestSyncPlan:
    """Whitelist enforcement and the no-stale-data rule (CLAUDE.md 4.6 / 6)."""

    def _setup(self, db, store, vplc, *, mode=ExposureMode.WHITELIST.value):
        connection = make_connection(db, exposure_mode=mode)
        db.upsert_area(connection.id, AreaType.DB, 1, 8, status=AreaStatus.OK)
        db.upsert_area(connection.id, AreaType.DB, 2, 8, status=AreaStatus.OK)
        task = AreaSyncTask(db, store, vplc, stale_timeout_seconds=5)
        return connection, task

    def test_unexposed_areas_are_never_registered(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc)
        store.publish(AreaKey(connection.id, "DB", 1), b"\x01" * 8)
        store.publish(AreaKey(connection.id, "DB", 2), b"\x02" * 8)

        task.sync_once()
        assert vplc.registrations() == []

    def test_exposing_an_area_registers_only_that_area(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc)
        store.publish(AreaKey(connection.id, "DB", 1), b"\x01" * 8)
        store.publish(AreaKey(connection.id, "DB", 2), b"\x02" * 8)
        db.set_area_exposed(db.get_area(connection.id, AreaType.DB, 1).id, True)

        task.sync_once()
        registered = [r.key.label for r in vplc.registrations()]
        assert registered == ["DB1"]

    def test_mirror_all_registers_everything(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        store.publish(AreaKey(connection.id, "DB", 1), b"\x01" * 8)
        store.publish(AreaKey(connection.id, "DB", 2), b"\x02" * 8)

        task.sync_once()
        assert {r.key.label for r in vplc.registrations()} == {"DB1", "DB2"}

    def test_an_area_without_a_successful_read_is_not_registered(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        store.publish_failure(AreaKey(connection.id, "DB", 1), "timeout")
        task.sync_once()
        assert vplc.registrations() == []

    def test_stale_data_is_withdrawn_rather_than_served(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        key = AreaKey(connection.id, "DB", 1)
        store.publish(key, b"\x01" * 8)
        task.sync_once()
        assert vplc.is_registered(key)

        # The PLC stops answering: the newest good read ages past the timeout.
        store.publish(key, b"\x01" * 8, read_at=time.time() - 60)
        task.sync_once()
        assert not vplc.is_registered(key), "stale areas must be unregistered, not served stale"

    def test_unexposing_withdraws_a_live_registration(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        key = AreaKey(connection.id, "DB", 1)
        store.publish(key, b"\x01" * 8)
        store.publish(AreaKey(connection.id, "DB", 2), b"\x02" * 8)
        task.sync_once()
        assert vplc.is_registered(key)

        area = db.get_area(connection.id, AreaType.DB, 1)
        db.update_connection(connection.id, {"exposure_mode": ExposureMode.WHITELIST.value})
        db.set_area_exposed(area.id, False)
        task.sync_once()
        assert not vplc.is_registered(key)

    def test_buffer_is_updated_with_live_data(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        key = AreaKey(connection.id, "DB", 1)
        store.publish(key, bytes(range(8)))
        task.sync_once()
        assert vplc.read_back(key) == bytes(range(8))

        store.publish(key, b"\xFF" * 8)
        task.sync_once()
        assert vplc.read_back(key) == b"\xFF" * 8

    def test_a_shrunk_block_keeps_its_registered_shape(self, db, store, vplc) -> None:
        """A live consumer's tag shapes must survive a DB shrinking."""
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        key = AreaKey(connection.id, "DB", 1)
        store.publish(key, b"\x01" * 8)
        task.sync_once()
        assert vplc.registered_size(key) == 8

        db.upsert_area(connection.id, AreaType.DB, 1, 4, status=AreaStatus.SHRUNK)
        store.publish(key, b"\x02" * 4)
        task.sync_once()

        assert vplc.registered_size(key) == 8, "shape must not change under a live consumer"
        # The bytes the PLC still provides are live; the rest is explicit zeros,
        # never left-over data.
        assert vplc.read_back(key) == b"\x02" * 4 + b"\x00" * 4

    def test_confirming_the_shape_lets_it_re_register_smaller(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        key = AreaKey(connection.id, "DB", 1)
        store.publish(key, b"\x01" * 8)
        task.sync_once()

        db.upsert_area(connection.id, AreaType.DB, 1, 4, status=AreaStatus.SHRUNK)
        store.publish(key, b"\x02" * 4)
        task.sync_once()
        assert vplc.registered_size(key) == 8

        # Operator presses "Confirm new size" on the Tag Mapping page.
        area = db.get_area(connection.id, AreaType.DB, 1)
        db.set_area_status(area.id, AreaStatus.OK, None)
        vplc.unregister(key, reason="operator confirmed")
        task.sync_once()
        assert vplc.registered_size(key) == 4

    def test_a_grown_block_is_re_registered_at_the_new_size(self, db, store, vplc) -> None:
        connection, task = self._setup(db, store, vplc, mode=ExposureMode.MIRROR_ALL.value)
        key = AreaKey(connection.id, "DB", 1)
        store.publish(key, b"\x01" * 8)
        task.sync_once()

        db.upsert_area(connection.id, AreaType.DB, 1, 16, status=AreaStatus.GROWN)
        store.publish(key, b"\x03" * 16)
        task.sync_once()
        assert vplc.registered_size(key) == 16


class TestVirtualPlcServer:
    def test_registration_size_is_range_checked(self, vplc) -> None:
        from snap7_gateway.virtual_plc.server import VirtualPlcError

        with pytest.raises(VirtualPlcError):
            vplc.register(KEY, 0)
        with pytest.raises(VirtualPlcError):
            vplc.register(KEY, 99_000_000)

    def test_unknown_area_type_is_rejected(self, vplc) -> None:
        from snap7_gateway.virtual_plc.server import VirtualPlcError

        with pytest.raises(VirtualPlcError):
            vplc.register(AreaKey(1, "ZZ", 0), 8)

    def test_update_of_an_unregistered_area_is_a_no_op(self, vplc) -> None:
        assert vplc.update(AreaKey(1, "DB", 99), b"\x01") is False

    def test_stop_clears_every_registration(self, vplc) -> None:
        vplc.register(KEY, 8)
        assert vplc.registrations()
        vplc.stop()
        assert vplc.registrations() == []
