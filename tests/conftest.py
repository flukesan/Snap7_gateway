"""Shared fixtures.

The integration fixtures run a real ``snap7.server.Server`` as a stand-in PLC.
Snap7 ships that server component precisely so client code can be tested without
hardware, and it exercises the same C library the field deployment uses.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

import pytest

from snap7.server import Server
from snap7.type import SrvArea

from snap7_gateway.auth.service import AuthService
from snap7_gateway.core.datastore import DataStore
from snap7_gateway.db.store import Database


def free_port() -> int:
    """Reserve an ephemeral TCP port, then release it for the server to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "gateway.db")
    yield database
    database.close()


@pytest.fixture()
def auth(db, tmp_path) -> AuthService:
    return AuthService(db, data_dir=tmp_path)


@pytest.fixture()
def store() -> DataStore:
    return DataStore()


@dataclass
class FakePlc:
    """A Snap7 server standing in for a real S7 CPU.

    Areas are registered as ``bytearray`` because python-snap7's server keeps
    that type by reference - the same contract the gateway's own server role
    relies on - so a test can change what the "PLC" holds at any time.
    """

    server: Server
    port: int
    buffers: dict[tuple[str, int], bytearray]

    def set_db(self, db_number: int, payload: bytes) -> None:
        buffer = self.buffers[("DB", db_number)]
        padded = bytes(payload)[: len(buffer)].ljust(len(buffer), b"\x00")
        buffer[:] = padded

    def db_size(self, db_number: int) -> int:
        return len(self.buffers[("DB", db_number)])


def _register(server: Server, buffers: dict, area: SrvArea, label: str, index: int,
              size: int, payload: bytes | None = None) -> None:
    buffer = bytearray(size)
    if payload:
        buffer[:] = bytes(payload)[:size].ljust(size, b"\x00")
    server.register_area(area, index, buffer)
    buffers[(label, index)] = buffer


@pytest.fixture()
def fake_plc() -> FakePlc:
    """A running fake PLC with DB1 (64 B), DB2 (16 B), plus I/Q/M areas."""
    server = Server(log=False)
    buffers: dict[tuple[str, int], bytearray] = {}
    _register(server, buffers, SrvArea.DB, "DB", 1, 64, bytes(range(64)))
    _register(server, buffers, SrvArea.DB, "DB", 2, 16, b"\xAA" * 16)
    _register(server, buffers, SrvArea.PE, "I", 0, 32)
    _register(server, buffers, SrvArea.PA, "Q", 0, 32)
    _register(server, buffers, SrvArea.MK, "M", 0, 64, b"\x5A" * 64)

    port = free_port()
    server.start_to("127.0.0.1", tcp_port=port)
    time.sleep(0.3)  # let the listener come up before the first connect
    try:
        yield FakePlc(server=server, port=port, buffers=buffers)
    finally:
        try:
            server.stop()
        finally:
            server.destroy()


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.1) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
