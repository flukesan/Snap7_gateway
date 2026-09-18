"""Typed row objects for the SQLite tables.

Plain dataclasses rather than an ORM: the schema is small and fixed, and an
edge deployment benefits from having zero optional dependencies in the data
path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AreaType(StrEnum):
    """Memory areas the gateway can read and re-host."""

    DB = "DB"
    INPUT = "I"
    OUTPUT = "Q"
    MERKER = "M"


class ExposureMode(StrEnum):
    """How a connection's discovered areas reach DeviceWise."""

    MIRROR_ALL = "mirror_all"
    WHITELIST = "whitelist"


class AreaStatus(StrEnum):
    """Lifecycle of a discovered area, surfaced in the Tag Mapping table."""

    NEW = "new"          # discovered, awaiting operator decision (whitelist mode)
    OK = "ok"            # present on the PLC at the recorded size
    GROWN = "grown"      # present but larger than last discovery
    SHRUNK = "shrunk"    # present but smaller - flagged, never auto-deregistered
    MISSING = "missing"  # gone from the PLC - flagged, never auto-deregistered


class ConnectionType(StrEnum):
    """S7 connection resource requested at TSAP negotiation time."""

    PG = "PG"
    OP = "OP"
    S7_BASIC = "S7_BASIC"

    @property
    def snap7_value(self) -> int:
        return {"PG": 1, "OP": 2, "S7_BASIC": 3}[self.value]


class Role(StrEnum):
    """Operator roles (CLAUDE.md section 4.4)."""

    ADMIN = "admin"    # full configuration rights
    VIEWER = "viewer"  # read-only: status, tags, logs; no writes, no config


@dataclass(slots=True)
class User:
    id: int
    username: str
    password_hash: str
    previous_hash: str | None
    role: str
    must_change_password: bool
    enabled: bool
    failed_attempts: int
    locked_until: float | None
    last_login_at: float | None
    password_changed_at: float | None
    created_at: float
    updated_at: float

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            previous_hash=row["previous_hash"],
            role=row["role"],
            must_change_password=bool(row["must_change_password"]),
            enabled=bool(row["enabled"]),
            failed_attempts=row["failed_attempts"],
            locked_until=row["locked_until"],
            last_login_at=row["last_login_at"],
            password_changed_at=row["password_changed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Session:
    token_hash: str
    user_id: int
    csrf_token: str
    created_at: float
    last_seen_at: float
    expires_at: float
    ip: str | None
    user_agent: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Session":
        return cls(
            token_hash=row["token_hash"],
            user_id=row["user_id"],
            csrf_token=row["csrf_token"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            ip=row["ip"],
            user_agent=row["user_agent"],
        )


@dataclass(slots=True)
class PlcConnection:
    """One configured link to a real PLC."""

    id: int
    name: str
    host: str
    rack: int
    slot: int
    tcp_port: int
    connection_type: str
    timeout_ms: int
    poll_interval_ms: int
    exposure_mode: str
    write_enabled: bool
    enabled: bool
    read_inputs: bool
    read_outputs: bool
    read_merkers: bool
    max_input_bytes: int
    max_output_bytes: int
    max_merker_bytes: int
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PlcConnection":
        return cls(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            rack=row["rack"],
            slot=row["slot"],
            tcp_port=row["tcp_port"],
            connection_type=row["connection_type"],
            timeout_ms=row["timeout_ms"],
            poll_interval_ms=row["poll_interval_ms"],
            exposure_mode=row["exposure_mode"],
            write_enabled=bool(row["write_enabled"]),
            enabled=bool(row["enabled"]),
            read_inputs=bool(row["read_inputs"]),
            read_outputs=bool(row["read_outputs"]),
            read_merkers=bool(row["read_merkers"]),
            max_input_bytes=row["max_input_bytes"],
            max_output_bytes=row["max_output_bytes"],
            max_merker_bytes=row["max_merker_bytes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def identity(self) -> tuple[Any, ...]:
        """Fields whose change requires tearing down the live Snap7 client."""
        return (
            self.host,
            self.rack,
            self.slot,
            self.tcp_port,
            self.connection_type,
            self.timeout_ms,
            self.enabled,
        )


@dataclass(slots=True)
class PlcArea:
    """A discovered memory area and its DeviceWise exposure decision."""

    id: int
    connection_id: int
    area_type: str
    db_number: int
    size_bytes: int
    friendly_name: str | None
    exposed: bool
    status: str
    status_detail: str | None
    first_seen_at: float
    last_seen_at: float

    @property
    def key(self) -> str:
        """Stable human-readable key, e.g. ``DB12`` or ``M``."""
        return f"DB{self.db_number}" if self.area_type == AreaType.DB else str(self.area_type)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PlcArea":
        return cls(
            id=row["id"],
            connection_id=row["connection_id"],
            area_type=row["area_type"],
            db_number=row["db_number"],
            size_bytes=row["size_bytes"],
            friendly_name=row["friendly_name"],
            exposed=bool(row["exposed"]),
            status=row["status"],
            status_detail=row["status_detail"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )


@dataclass(slots=True)
class PlcTag:
    """An operator-named address inside an area."""

    id: int
    connection_id: int
    name: str
    area_type: str
    db_number: int
    byte_offset: int
    bit_offset: int
    data_type: str
    length: int
    description: str | None
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def address(self) -> str:
        """Siemens-style address text, e.g. ``DB10.DBX4.2`` or ``MW20``."""
        if self.area_type == AreaType.DB:
            base = f"DB{self.db_number}.DBB{self.byte_offset}"
            if self.data_type == "BOOL":
                base = f"DB{self.db_number}.DBX{self.byte_offset}.{self.bit_offset}"
            return base
        prefix = {"I": "I", "Q": "Q", "M": "M"}.get(str(self.area_type), "?")
        if self.data_type == "BOOL":
            return f"{prefix}{self.byte_offset}.{self.bit_offset}"
        return f"{prefix}B{self.byte_offset}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PlcTag":
        return cls(
            id=row["id"],
            connection_id=row["connection_id"],
            name=row["name"],
            area_type=row["area_type"],
            db_number=row["db_number"],
            byte_offset=row["byte_offset"],
            bit_offset=row["bit_offset"],
            data_type=row["data_type"],
            length=row["length"],
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class AuditEntry:
    id: int
    ts: float
    username: str
    action: str
    target: str | None
    detail: str | None
    ip: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AuditEntry":
        return cls(
            id=row["id"],
            ts=row["ts"],
            username=row["username"],
            action=row["action"],
            target=row["target"],
            detail=row["detail"],
            ip=row["ip"],
        )
