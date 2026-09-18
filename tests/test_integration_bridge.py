"""End-to-end test of the full bridge.

    [fake PLC] --Snap7 client (gateway polls)--> [gateway] --Snap7 server--> [test client]

The "test client" is a real ``python-snap7`` client doing exactly what
DeviceWise's Siemens driver does with ``Connection: Direct``: connect to the
gateway's IP on the S7 port, enumerate the data blocks, and read them. Both
directions of the bridge are therefore exercised against the real protocol
stack, not against mocks.
"""

from __future__ import annotations

import asyncio
import time

from snap7.client import Client
from snap7.type import Area, Block

from snap7_gateway.core.datastore import AreaKey
from snap7_gateway.core.runtime import GatewayRuntime
from snap7_gateway.db.models import AreaType, ExposureMode

from .conftest import free_port


def build_runtime(tmp_path, fake_plc, *, exposure_mode: str) -> GatewayRuntime:
    runtime = GatewayRuntime(tmp_path / "data", console_logging=False)
    runtime.db.set_settings(
        {
            "vplc_bind_ip": "127.0.0.1",
            "vplc_port": str(free_port()),
            "vplc_sync_interval_ms": "100",
            "vplc_stale_timeout_seconds": "5",
        }
    )
    runtime.vplc.bind_ip = "127.0.0.1"
    runtime.vplc.port = runtime.db.get_int("vplc_port")
    runtime.db.create_connection(
        {
            "name": "FakePLC",
            "host": "127.0.0.1",
            "rack": 0,
            "slot": 2,
            "tcp_port": fake_plc.port,
            "connection_type": "PG",
            "timeout_ms": 2000,
            "poll_interval_ms": 100,
            "exposure_mode": exposure_mode,
            "write_enabled": False,
            "enabled": True,
            "read_inputs": False,
            "read_outputs": False,
            "read_merkers": False,
            "max_input_bytes": 0,
            "max_output_bytes": 0,
            "max_merker_bytes": 0,
        }
    )
    return runtime


async def await_until(predicate, timeout: float = 20.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def devicewise_client(port: int) -> Client:
    """A client configured the way DeviceWise's Siemens driver connects."""
    client = Client()
    client.connect("127.0.0.1", 0, 2, tcp_port=port)
    return client


class TestMirrorAll:
    def test_full_bridge(self, tmp_path, fake_plc) -> None:
        async def scenario() -> None:
            runtime = build_runtime(tmp_path, fake_plc, exposure_mode=ExposureMode.MIRROR_ALL)
            await runtime.start()
            try:
                key1 = AreaKey(1, AreaType.DB.value, 1)
                key2 = AreaKey(1, AreaType.DB.value, 2)

                # 1. Discovery found both DBs at their real sizes.
                assert await await_until(
                    lambda: len(runtime.db.list_areas()) >= 2
                ), "discovery never completed"
                sizes = {a.key: a.size_bytes for a in runtime.db.list_areas()}
                assert sizes["DB1"] == 64 and sizes["DB2"] == 16

                # 2. Polling filled the datastore from the real PLC.
                assert await await_until(lambda: runtime.store.fresh(key1, 10) is not None)

                # 3. The virtual CPU registered both, at the real sizes.
                assert await await_until(
                    lambda: runtime.vplc.registered_size(key1) == 64
                    and runtime.vplc.registered_size(key2) == 16
                ), "areas were never registered on the virtual CPU"

                port = runtime.vplc.port
                client = await asyncio.to_thread(devicewise_client, port)
                try:
                    # 4. DeviceWise-style enumeration sees the real block list.
                    blocks = await asyncio.to_thread(client.list_blocks)
                    assert int(blocks.DBCount) == 2
                    numbers = await asyncio.to_thread(client.list_blocks_of_type, Block.DB, 10)
                    assert sorted(int(n) for n in numbers) == [1, 2]

                    # 5. And the sizes it enumerates are the real ones - this is
                    #    what makes its tree read DB1 UINT1[64] instead of a guess.
                    info = await asyncio.to_thread(client.get_block_info, Block.DB, 1)
                    assert int(info.MC7Size) == 64

                    # 6. Reading through the gateway returns the PLC's data.
                    data = await asyncio.to_thread(client.read_area, Area.DB, 1, 0, 16)
                    assert bytes(data) == bytes(range(16))

                    # 7. A change on the real PLC propagates through the bridge.
                    fake_plc.set_db(1, b"\xDE\xAD\xBE\xEF" + bytes(60))
                    assert await await_until(
                        lambda: bytes(runtime.store.get(key1).data[:4]) == b"\xDE\xAD\xBE\xEF"
                    ), "the poll never picked up the new PLC data"
                    assert await await_until(
                        lambda: bytes(runtime.vplc.read_back(key1) or b"")[:4] == b"\xDE\xAD\xBE\xEF"
                    ), "the sync task never refreshed the virtual CPU buffer"

                    data = await asyncio.to_thread(client.read_area, Area.DB, 1, 0, 4)
                    assert bytes(data) == b"\xDE\xAD\xBE\xEF"
                finally:
                    await asyncio.to_thread(client.disconnect)
                    await asyncio.to_thread(client.destroy)
            finally:
                await runtime.stop()

        asyncio.run(scenario())


class TestWhitelist:
    def test_unexposed_areas_are_invisible_to_devicewise(self, tmp_path, fake_plc) -> None:
        """Whitelist enforcement happens at registration, so an unexposed DB
        does not exist on the virtual CPU rather than existing-but-hidden."""

        async def scenario() -> None:
            runtime = build_runtime(tmp_path, fake_plc, exposure_mode=ExposureMode.WHITELIST)
            await runtime.start()
            try:
                assert await await_until(lambda: len(runtime.db.list_areas()) >= 2)
                # Nothing is exposed yet, so nothing may be registered.
                await asyncio.sleep(0.5)
                assert runtime.vplc.registrations() == []

                port = runtime.vplc.port
                client = await asyncio.to_thread(devicewise_client, port)
                try:
                    blocks = await asyncio.to_thread(client.list_blocks)
                    assert int(blocks.DBCount) == 0, "an unexposed DB must not be enumerable"

                    # Operator whitelists DB2 only.
                    area = runtime.db.get_area(1, AreaType.DB, 2)
                    runtime.db.set_area_exposed(area.id, True)
                    runtime.sync.request_refresh()

                    key2 = AreaKey(1, AreaType.DB.value, 2)
                    assert await await_until(lambda: runtime.vplc.is_registered(key2))

                    blocks = await asyncio.to_thread(client.list_blocks)
                    assert int(blocks.DBCount) == 1
                    numbers = await asyncio.to_thread(client.list_blocks_of_type, Block.DB, 10)
                    assert [int(n) for n in numbers] == [2]

                    data = await asyncio.to_thread(client.read_area, Area.DB, 2, 0, 16)
                    assert bytes(data) == b"\xAA" * 16
                finally:
                    await asyncio.to_thread(client.disconnect)
                    await asyncio.to_thread(client.destroy)
            finally:
                await runtime.stop()

        asyncio.run(scenario())


class TestResilience:
    def test_a_dead_plc_does_not_stop_the_gateway_and_data_is_withheld(
        self, tmp_path, fake_plc
    ) -> None:
        """Losing the PLC must withdraw the area rather than serve stale bytes,
        and the gateway must keep running and reconnect."""

        async def scenario() -> None:
            runtime = build_runtime(tmp_path, fake_plc, exposure_mode=ExposureMode.MIRROR_ALL)
            runtime.db.set_setting("vplc_stale_timeout_seconds", "1")
            runtime.sync.stale_timeout_seconds = 1.0
            await runtime.start()
            try:
                key1 = AreaKey(1, AreaType.DB.value, 1)
                assert await await_until(lambda: runtime.vplc.is_registered(key1), timeout=25)

                # The PLC goes away.
                fake_plc.server.stop()
                assert await await_until(
                    lambda: not runtime.vplc.is_registered(key1), timeout=25
                ), "a stale area must be unregistered, not served with old data"

                # The gateway itself is still alive and trying to reconnect.
                health = runtime.store.health(1)
                assert health is not None
                assert health.state in {"connected", "reconnecting", "connecting", "failed"}
                assert runtime.sync.running
            finally:
                await runtime.stop()

        asyncio.run(scenario())

    def test_one_dead_connection_does_not_disturb_a_healthy_one(
        self, tmp_path, fake_plc
    ) -> None:
        async def scenario() -> None:
            runtime = build_runtime(tmp_path, fake_plc, exposure_mode=ExposureMode.MIRROR_ALL)
            # A second connection pointing at a port where nothing listens.
            runtime.db.create_connection(
                {
                    "name": "DeadPLC",
                    "host": "127.0.0.1",
                    "rack": 0,
                    "slot": 2,
                    "tcp_port": free_port(),
                    "connection_type": "PG",
                    "timeout_ms": 500,
                    "poll_interval_ms": 100,
                    "exposure_mode": ExposureMode.MIRROR_ALL.value,
                    "write_enabled": False,
                    "enabled": True,
                    "read_inputs": False,
                    "read_outputs": False,
                    "read_merkers": False,
                    "max_input_bytes": 0,
                    "max_output_bytes": 0,
                    "max_merker_bytes": 0,
                }
            )
            await runtime.start()
            try:
                good = runtime.db.get_connection_by_name("FakePLC")
                bad = runtime.db.get_connection_by_name("DeadPLC")
                key = AreaKey(good.id, AreaType.DB.value, 1)

                assert await await_until(
                    lambda: runtime.store.fresh(key, 10) is not None, timeout=25
                ), "the healthy connection stopped polling because of the dead one"

                bad_health = runtime.store.health(bad.id)
                assert bad_health is not None
                assert bad_health.state in {"reconnecting", "failed", "connecting"}
                assert bad_health.last_error
            finally:
                await runtime.stop()

        asyncio.run(scenario())
