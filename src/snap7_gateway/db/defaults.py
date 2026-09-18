"""Default values for every configurable setting.

CLAUDE.md section 6 requires each configurable behavior to ship with a sane
default, server-side validation and a manual entry. This module owns the
defaults; validation lives in :mod:`snap7_gateway.db.validation` and the manual
entries in ``docs/MANUAL.md``.
"""

from __future__ import annotations

DEFAULT_SETTINGS: dict[str, str] = {
    # --- Presentation -------------------------------------------------
    "locale": "en",  # en | th - language of operator-facing UI/log text
    # --- Web server ---------------------------------------------------
    "web_host": "0.0.0.0",
    "web_port": "8443",
    "https_enabled": "true",
    "tls_cert_path": "",  # blank -> generated self-signed cert in <data>/certs
    "tls_key_path": "",
    # --- Sessions -----------------------------------------------------
    "session_idle_timeout_minutes": "30",
    "session_absolute_timeout_hours": "12",
    # --- Login protection ---------------------------------------------
    "login_max_failures": "5",
    "login_lockout_minutes": "15",
    "login_rate_window_seconds": "60",
    "login_rate_max_attempts": "10",
    # --- Password policy ----------------------------------------------
    "password_min_length": "12",
    "password_require_upper": "true",
    "password_require_lower": "true",
    "password_require_digit": "true",
    "password_require_symbol": "false",
    "password_forbid_username": "true",
    "password_forbid_previous": "true",
    "password_use_blocklist": "true",
    # --- Logging ------------------------------------------------------
    "log_level": "INFO",
    "log_max_bytes": str(10 * 1024 * 1024),
    "log_backup_count": "10",
    "log_ring_capacity": "2000",
    "crash_log_lines": "500",
    # --- Virtual S7 PLC (DeviceWise-facing server role) ---------------
    "vplc_enabled": "true",
    "vplc_bind_ip": "0.0.0.0",
    "vplc_port": "102",
    "vplc_rack": "0",
    "vplc_slot": "2",
    "vplc_sync_interval_ms": "250",
    # An area whose newest good read is older than this is unregistered so a
    # DeviceWise read fails loudly instead of returning stale bytes
    # (CLAUDE.md section 6).
    "vplc_stale_timeout_seconds": "30",
    "vplc_max_clients": "64",
    # --- Discovery ----------------------------------------------------
    "discovery_interval_minutes": "0",  # 0 = manual Rescan only
    "discovery_max_db_count": "2048",
}
