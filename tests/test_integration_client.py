"""Client role against a Snap7 server simulator standing in for a real PLC.

Covers the PLC-facing half of the bridge: connect, enumerate blocks, read exact
sizes, and fail predictably when the PLC is not there.
"""

from __future__ import annotations

import pytest

from snap7_gateway.db.models import AreaType
from snap7_gateway.plc.client import PlcClient, PlcConnectionParams, PlcError
from snap7_gateway.plc.decoding import decode
from snap7_gateway.plc.discovery import discover

from .conftest import free_port
from .test_discovery import make_connection


@pytest.fixture()
def params(fake_plc) -> PlcConnectionParams:
    return PlcConnectionParams(host="127.0.0.1", rack=0, slot=2, tcp_port=fake_plc.port,
                               timeout_ms=2000)


@pytest.fixture()
def client(params) -> PlcClient:
    plc = PlcClient(params)
    plc.connect()
    try:
        yield plc
    finally:
        plc.disconnect()


class TestConnection:
    def test_connect_and_disconnect(self, params) -> None:
        plc = PlcClient(params)
        assert not plc.connected
        plc.connect()
        assert plc.connected
        plc.disconnect()
        assert not plc.connected

    def test_context_manager(self, params) -> None:
        with PlcClient(params) as plc:
            assert plc.connected

    def test_refused_connection_reports_the_real_error(self) -> None:
        params = PlcConnectionParams(host="127.0.0.1", tcp_port=free_port(), timeout_ms=500)
        plc = PlcClient(params)
        with pytest.raises(PlcError) as excinfo:
            plc.connect()
        assert "connecting to 127.0.0.1" in excinfo.value.detail

    def test_test_connection_reports_success_with_latency(self, params) -> None:
        result = PlcClient.test_connection(params)
        assert result.ok
        assert result.latency_ms > 0
        assert result.details.get("db_count") == 2

    def test_test_connection_never_raises_on_failure(self) -> None:
        params = PlcConnectionParams(host="127.0.0.1", tcp_port=free_port(), timeout_ms=500)
        result = PlcClient.test_connection(params)
        assert not result.ok
        assert result.message  # a real reason, not a generic "failed"
        assert "connect" in result.message.lower()

    def test_reading_without_a_connection_raises_plc_error(self, params) -> None:
        with pytest.raises(PlcError, match="not connected"):
            PlcClient(params).read_area(AreaType.DB, 1, 0, 4)


class TestReads:
    """Note: the Snap7 *simulator* is lenient about out-of-range reads (it pads
    with zeros where a real CPU returns an address error), so the tests here
    assert the gateway's own guards and the happy path rather than the
    simulator's error behaviour."""

    def test_read_db_returns_exact_bytes(self, client, fake_plc) -> None:
        assert bytes(client.read_area(AreaType.DB, 1, 0, 16)) == bytes(range(16))

    def test_read_with_offset(self, client) -> None:
        assert bytes(client.read_area(AreaType.DB, 1, 8, 4)) == bytes(range(8, 12))

    def test_read_merkers(self, client) -> None:
        assert bytes(client.read_area(AreaType.MERKER, 0, 0, 8)) == b"\x5A" * 8

    def test_zero_length_read_is_empty(self, client) -> None:
        assert client.read_area(AreaType.DB, 1, 0, 0) == bytearray()

    def test_oversized_read_is_refused_before_touching_the_plc(self, client) -> None:
        with pytest.raises(PlcError, match="refusing to read"):
            client.read_area(AreaType.DB, 1, 0, 99_000_000)

    def test_unknown_area_is_refused(self, client) -> None:
        with pytest.raises(PlcError, match="unknown area type"):
            client.read_area("ZZ", 0, 0, 4)

    def test_decoded_values_match_what_the_plc_holds(self, client, fake_plc) -> None:
        fake_plc.set_db(1, bytes([0x00, 0x2A, 0x42, 0xC8, 0x00, 0x00]))
        data = client.read_area(AreaType.DB, 1, 0, 8)
        assert decode(data, "INT", 0) == 42
        assert decode(data, "REAL", 2) == pytest.approx(100.0)


class TestBlockDiscovery:
    def test_list_db_numbers(self, client) -> None:
        assert client.list_db_numbers() == [1, 2]

    def test_db_sizes_are_the_real_sizes(self, client, fake_plc) -> None:
        assert client.get_db_size(1) == fake_plc.db_size(1) == 64
        assert client.get_db_size(2) == fake_plc.db_size(2) == 16

    def test_unknown_db_raises(self, client) -> None:
        with pytest.raises(PlcError):
            client.get_db_size(99)

    def test_probe_finds_a_readable_size_for_areas_without_block_info(self, client) -> None:
        # The simulator registers 64 merker bytes; probing from 1024 must land on
        # a size the CPU actually serves.
        size = client.probe_area_size(AreaType.MERKER, 1024)
        assert 0 < size <= 1024
        assert client.read_area(AreaType.MERKER, 0, 0, size)

    def test_full_discovery_pass(self, client, db) -> None:
        connection = make_connection(db)
        result = discover(client, connection)
        assert result.ok
        found = result.by_key()
        assert found["DB1"].size_bytes == 64
        assert found["DB2"].size_bytes == 16
        assert "M" in found
        assert result.duration_ms > 0

    def test_discovery_survives_a_dead_link(self, params, db) -> None:
        """A disconnected PLC yields an error list, not an exception."""
        connection = make_connection(db)
        plc = PlcClient(params)
        plc.connect()
        plc.disconnect()
        result = discover(plc, connection)
        assert not result.ok
        assert result.errors
