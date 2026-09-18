"""Snap7 client role: PLC I/O, block discovery and data-type decoding."""

from .client import (
    AREA_MAP,
    ConnectionTestResult,
    CpuIdentity,
    PlcClient,
    PlcConnectionParams,
    PlcError,
)
from .decoding import DataType, DecodeError, decode, encode, format_value, size_of
from .discovery import (
    AreaChange,
    ChangeKind,
    DiscoveredArea,
    DiscoveryDiff,
    DiscoveryResult,
    apply_discovery,
    diff_discovery,
    discover,
)

__all__ = [
    "AREA_MAP",
    "ConnectionTestResult",
    "CpuIdentity",
    "PlcClient",
    "PlcConnectionParams",
    "PlcError",
    "DataType",
    "DecodeError",
    "decode",
    "encode",
    "format_value",
    "size_of",
    "AreaChange",
    "ChangeKind",
    "DiscoveredArea",
    "DiscoveryDiff",
    "DiscoveryResult",
    "apply_discovery",
    "diff_discovery",
    "discover",
]
