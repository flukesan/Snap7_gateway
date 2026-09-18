"""Server-side input validation (CLAUDE.md section 3.2).

Every value that reaches SQLite, the polling engine or the Snap7 server passes
through here. The web layer runs the same functions the CLI would, so client-
side checks are a convenience only - an attacker or a misconfigured device on
the same VLAN posting raw form data hits exactly these rules.

Each validator returns ``(clean_values, errors)`` where ``errors`` maps a field
name to an operator-readable reason. Nothing is written when ``errors`` is
non-empty.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Mapping

from ..db.models import AreaType, ConnectionType, ExposureMode, Role
from ..plc.decoding import MAX_STRING_LENGTH, DataType, DecodeError, size_of

#: Names are used in logs, audit entries and the UI, so keep them printable and
#: free of characters that complicate log parsing.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,63}$")
HOSTNAME_PATTERN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                             r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._\-]{3,32}$")

LIMITS = {
    "rack": (0, 7),
    "slot": (0, 31),
    "tcp_port": (1, 65535),
    "timeout_ms": (250, 60_000),
    "poll_interval_ms": (50, 3_600_000),
    "area_bytes": (0, 8 * 1024 * 1024),
    "db_number": (1, 65535),
    "byte_offset": (0, 8 * 1024 * 1024),
    "bit_offset": (0, 7),
    "read_size": (1, 65536),
}


class ValidationFailed(ValueError):
    """Raised by ``*_or_raise`` helpers when validation produced errors."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _int_field(
    values: Mapping[str, Any],
    field: str,
    errors: dict[str, str],
    *,
    minimum: int,
    maximum: int,
    default: int | None = None,
    label: str | None = None,
) -> int | None:
    raw = values.get(field, "")
    text = str(raw).strip()
    if text == "":
        if default is not None:
            return default
        errors[field] = f"{label or field} is required"
        return None
    try:
        number = int(text, 10)
    except ValueError:
        errors[field] = f"{label or field} must be a whole number"
        return None
    if not minimum <= number <= maximum:
        errors[field] = f"{label or field} must be between {minimum} and {maximum}"
        return None
    return number


def _bool_field(values: Mapping[str, Any], field: str) -> bool:
    raw = values.get(field)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def validate_host(host: str) -> str | None:
    """Return an error message, or ``None`` when the host is acceptable."""
    host = host.strip()
    if not host:
        return "PLC address is required"
    if len(host) > 253:
        return "PLC address is too long"
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if HOSTNAME_PATTERN.match(host):
        return None
    return "PLC address must be a valid IPv4/IPv6 address or hostname"


def validate_connection(
    values: Mapping[str, Any], *, existing_names: set[str] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate a PLC connection form.

    ``existing_names`` should contain the lower-cased names of *other*
    connections so duplicates are rejected before hitting the UNIQUE index.
    """
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}

    name = str(values.get("name", "")).strip()
    if not NAME_PATTERN.match(name):
        errors["name"] = (
            "Name must be 1-64 characters, start with a letter or digit, and contain only "
            "letters, digits, spaces, dots, underscores or hyphens"
        )
    elif existing_names and name.lower() in existing_names:
        errors["name"] = "Another connection already uses this name"
    else:
        clean["name"] = name

    host = str(values.get("host", "")).strip()
    host_error = validate_host(host)
    if host_error:
        errors["host"] = host_error
    else:
        clean["host"] = host

    for field, label in (
        ("rack", "Rack"),
        ("slot", "Slot"),
        ("tcp_port", "TCP port"),
        ("timeout_ms", "Timeout (ms)"),
        ("poll_interval_ms", "Poll interval (ms)"),
    ):
        limit_key = field if field in LIMITS else field
        low, high = LIMITS[limit_key]
        default = {"rack": 0, "slot": 2, "tcp_port": 102, "timeout_ms": 3000,
                   "poll_interval_ms": 1000}[field]
        number = _int_field(
            values, field, errors, minimum=low, maximum=high, default=default, label=label
        )
        if number is not None:
            clean[field] = number

    conn_type = str(values.get("connection_type", ConnectionType.PG)).strip().upper()
    if conn_type not in {t.value for t in ConnectionType}:
        errors["connection_type"] = "Connection type must be PG, OP or S7_BASIC"
    else:
        clean["connection_type"] = conn_type

    mode = str(values.get("exposure_mode", ExposureMode.WHITELIST)).strip().lower()
    if mode not in {m.value for m in ExposureMode}:
        errors["exposure_mode"] = "Exposure mode must be 'mirror_all' or 'whitelist'"
    else:
        clean["exposure_mode"] = mode

    for field, label, default in (
        ("max_input_bytes", "Max input bytes", 1024),
        ("max_output_bytes", "Max output bytes", 1024),
        ("max_merker_bytes", "Max merker bytes", 8192),
    ):
        low, high = LIMITS["area_bytes"]
        number = _int_field(
            values, field, errors, minimum=low, maximum=high, default=default, label=label
        )
        if number is not None:
            clean[field] = number

    clean["write_enabled"] = _bool_field(values, "write_enabled")
    clean["enabled"] = _bool_field(values, "enabled")
    clean["read_inputs"] = _bool_field(values, "read_inputs")
    clean["read_outputs"] = _bool_field(values, "read_outputs")
    clean["read_merkers"] = _bool_field(values, "read_merkers")

    return clean, errors


def validate_area_address(
    values: Mapping[str, Any], *, require_size: bool = False
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate an area reference (used by the Test Read tool)."""
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}

    area_type = str(values.get("area_type", "")).strip().upper()
    if area_type not in {t.value for t in AreaType}:
        errors["area_type"] = "Area must be DB, I, Q or M"
    else:
        clean["area_type"] = area_type

    if clean.get("area_type") == AreaType.DB:
        low, high = LIMITS["db_number"]
        number = _int_field(
            values, "db_number", errors, minimum=low, maximum=high, label="DB number"
        )
        if number is not None:
            clean["db_number"] = number
    else:
        clean["db_number"] = 0

    low, high = LIMITS["byte_offset"]
    offset = _int_field(
        values, "byte_offset", errors, minimum=low, maximum=high, default=0, label="Byte offset"
    )
    if offset is not None:
        clean["byte_offset"] = offset

    if require_size:
        low, high = LIMITS["read_size"]
        size = _int_field(values, "size", errors, minimum=low, maximum=high, default=1, label="Size")
        if size is not None:
            clean["size"] = size

    return clean, errors


def validate_tag(
    values: Mapping[str, Any], *, existing_names: set[str] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate a tag-mapping row: address, type, length and name."""
    clean, errors = validate_area_address(values)

    name = str(values.get("name", "")).strip()
    if not NAME_PATTERN.match(name):
        errors["name"] = (
            "Tag name must be 1-64 characters using letters, digits, spaces, dots, "
            "underscores or hyphens"
        )
    elif existing_names and name.lower() in existing_names:
        errors["name"] = "Another tag on this connection already uses this name"
    else:
        clean["name"] = name

    data_type = str(values.get("data_type", "")).strip().upper()
    if data_type not in {t.value for t in DataType}:
        errors["data_type"] = "Data type must be one of: " + ", ".join(t.value for t in DataType)
    else:
        clean["data_type"] = data_type

    length_default = 1
    length = _int_field(
        values, "length", errors, minimum=1, maximum=MAX_STRING_LENGTH,
        default=length_default, label="Length",
    )
    if length is not None:
        clean["length"] = length

    bit = _int_field(
        values, "bit_offset", errors, minimum=0, maximum=7, default=0, label="Bit offset"
    )
    if bit is not None:
        clean["bit_offset"] = bit
    if bit and data_type != DataType.BOOL:
        errors["bit_offset"] = "Bit offset only applies to BOOL tags"

    if data_type and data_type != DataType.STRING and length not in (None, 1):
        errors["length"] = "Length only applies to STRING tags"

    if data_type and not errors.get("data_type"):
        try:
            size_of(data_type, length or 1)
        except DecodeError as exc:
            errors["length"] = str(exc)

    description = str(values.get("description", "")).strip()
    clean["description"] = description[:255] or None

    return clean, errors


def validate_username(username: str, *, existing: set[str] | None = None) -> str | None:
    username = username.strip()
    if not USERNAME_PATTERN.match(username):
        return "Username must be 3-32 characters (letters, digits, dot, underscore, hyphen)"
    if existing and username.lower() in existing:
        return "That username already exists"
    return None


def validate_role(role: str) -> str | None:
    if role not in {r.value for r in Role}:
        return "Role must be 'admin' or 'viewer'"
    return None


#: Settings that may be changed through the web UI, with their validators.
_SETTING_RULES: dict[str, tuple[str, Any]] = {
    "locale": ("choice", ("en", "th")),
    "session_idle_timeout_minutes": ("int", (1, 1440)),
    "session_absolute_timeout_hours": ("int", (1, 168)),
    "login_max_failures": ("int", (1, 100)),
    "login_lockout_minutes": ("int", (1, 1440)),
    "login_rate_window_seconds": ("int", (5, 3600)),
    "login_rate_max_attempts": ("int", (1, 1000)),
    "password_min_length": ("int", (12, 128)),
    "password_require_upper": ("bool", None),
    "password_require_lower": ("bool", None),
    "password_require_digit": ("bool", None),
    "password_require_symbol": ("bool", None),
    "password_forbid_username": ("bool", None),
    "password_forbid_previous": ("bool", None),
    "password_use_blocklist": ("bool", None),
    "log_level": ("choice", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
    "log_max_bytes": ("int", (64 * 1024, 1024 * 1024 * 1024)),
    "log_backup_count": ("int", (1, 100)),
    "log_ring_capacity": ("int", (100, 100_000)),
    "crash_log_lines": ("int", (10, 10_000)),
    "vplc_enabled": ("bool", None),
    "vplc_bind_ip": ("host", None),
    "vplc_port": ("int", (1, 65535)),
    "vplc_rack": ("int", (0, 7)),
    "vplc_slot": ("int", (0, 31)),
    "vplc_sync_interval_ms": ("int", (50, 60_000)),
    "vplc_stale_timeout_seconds": ("int", (1, 3600)),
    "vplc_max_clients": ("int", (1, 1024)),
    "discovery_interval_minutes": ("int", (0, 10_080)),
    "discovery_max_db_count": ("int", (1, 65535)),
    "web_host": ("host", None),
    "web_port": ("int", (1, 65535)),
    "https_enabled": ("bool", None),
    "tls_cert_path": ("path", None),
    "tls_key_path": ("path", None),
}


def validate_settings(values: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Validate a settings form, ignoring keys that are not operator-editable."""
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}

    for key, raw in values.items():
        rule = _SETTING_RULES.get(key)
        if rule is None:
            continue  # unknown or read-only key - never written
        kind, spec = rule
        text = str(raw).strip()
        if kind == "int":
            low, high = spec
            try:
                number = int(text, 10)
            except ValueError:
                errors[key] = f"{key} must be a whole number"
                continue
            if not low <= number <= high:
                errors[key] = f"{key} must be between {low} and {high}"
                continue
            clean[key] = str(number)
        elif kind == "bool":
            clean[key] = "true" if text.lower() in {"1", "true", "yes", "on"} else "false"
        elif kind == "choice":
            candidate = text if text in spec else text.upper() if text.upper() in spec else None
            if candidate is None:
                errors[key] = f"{key} must be one of: {', '.join(spec)}"
                continue
            clean[key] = candidate
        elif kind == "host":
            if text in {"0.0.0.0", "::"}:
                clean[key] = text
                continue
            host_error = validate_host(text)
            if host_error:
                errors[key] = f"{key}: {host_error}"
                continue
            clean[key] = text
        elif kind == "path":
            if len(text) > 4096:
                errors[key] = f"{key} is too long"
                continue
            clean[key] = text

    # Checkbox inputs are absent from the POST body when unticked, so any bool
    # setting the form owns but did not submit must be written as false.
    return clean, errors


def bool_settings_in(form_keys: set[str]) -> set[str]:
    """Bool-typed settings, so the caller can default missing checkboxes."""
    return {k for k, (kind, _) in _SETTING_RULES.items() if kind == "bool"} & form_keys
