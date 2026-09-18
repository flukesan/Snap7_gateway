"""SQLite schema and forward-only migrations.

The gateway must run standalone on an edge device, so all configuration,
credentials and tag mapping live in one SQLite file (CLAUDE.md section 2).
Migrations are a plain ordered list of DDL steps; ``user_version`` records how
far the file has been migrated. Never edit an existing migration - append a new
one, because a deployed gateway may already have run the old version.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password_hash        TEXT    NOT NULL,
            previous_hash        TEXT,
            role                 TEXT    NOT NULL DEFAULT 'admin',
            must_change_password INTEGER NOT NULL DEFAULT 0,
            enabled              INTEGER NOT NULL DEFAULT 1,
            failed_attempts      INTEGER NOT NULL DEFAULT 0,
            locked_until         REAL,
            last_login_at        REAL,
            password_changed_at  REAL,
            created_at           REAL    NOT NULL,
            updated_at           REAL    NOT NULL
        );

        CREATE TABLE sessions (
            token_hash   TEXT    PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token   TEXT    NOT NULL,
            created_at   REAL    NOT NULL,
            last_seen_at REAL    NOT NULL,
            expires_at   REAL    NOT NULL,
            ip           TEXT,
            user_agent   TEXT
        );
        CREATE INDEX idx_sessions_user ON sessions(user_id);
        CREATE INDEX idx_sessions_expiry ON sessions(expires_at);

        CREATE TABLE settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE plc_connections (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE,
            host             TEXT    NOT NULL,
            rack             INTEGER NOT NULL DEFAULT 0,
            slot             INTEGER NOT NULL DEFAULT 2,
            tcp_port         INTEGER NOT NULL DEFAULT 102,
            connection_type  TEXT    NOT NULL DEFAULT 'PG',
            timeout_ms       INTEGER NOT NULL DEFAULT 3000,
            poll_interval_ms INTEGER NOT NULL DEFAULT 1000,
            exposure_mode    TEXT    NOT NULL DEFAULT 'whitelist',
            write_enabled    INTEGER NOT NULL DEFAULT 0,
            enabled          INTEGER NOT NULL DEFAULT 1,
            read_inputs      INTEGER NOT NULL DEFAULT 1,
            read_outputs     INTEGER NOT NULL DEFAULT 1,
            read_merkers     INTEGER NOT NULL DEFAULT 1,
            max_input_bytes  INTEGER NOT NULL DEFAULT 1024,
            max_output_bytes INTEGER NOT NULL DEFAULT 1024,
            max_merker_bytes INTEGER NOT NULL DEFAULT 8192,
            created_at       REAL    NOT NULL,
            updated_at       REAL    NOT NULL
        );

        -- One row per memory area discovered on a real PLC. This table is the
        -- registration whitelist consumed by the Snap7 server role: an area
        -- with exposed=0 is never registerArea()'d, so DeviceWise cannot even
        -- enumerate it (CLAUDE.md section 4.6).
        CREATE TABLE plc_areas (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL REFERENCES plc_connections(id) ON DELETE CASCADE,
            area_type     TEXT    NOT NULL,          -- DB | I | Q | M
            db_number     INTEGER NOT NULL DEFAULT 0,
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            friendly_name TEXT,
            exposed       INTEGER NOT NULL DEFAULT 0,
            status        TEXT    NOT NULL DEFAULT 'new',  -- new|ok|grown|shrunk|missing
            status_detail TEXT,
            first_seen_at REAL    NOT NULL,
            last_seen_at  REAL    NOT NULL,
            UNIQUE(connection_id, area_type, db_number)
        );
        CREATE INDEX idx_areas_connection ON plc_areas(connection_id);

        -- Address-level mapping used for decoding, the Test Read tool and the
        -- operator-facing tag list. Exposure to DeviceWise is decided by the
        -- owning area's exposed flag, not by this table.
        CREATE TABLE plc_tags (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL REFERENCES plc_connections(id) ON DELETE CASCADE,
            name          TEXT    NOT NULL,
            area_type     TEXT    NOT NULL,
            db_number     INTEGER NOT NULL DEFAULT 0,
            byte_offset   INTEGER NOT NULL DEFAULT 0,
            bit_offset    INTEGER NOT NULL DEFAULT 0,
            data_type     TEXT    NOT NULL,
            length        INTEGER NOT NULL DEFAULT 1,
            description   TEXT,
            created_at    REAL    NOT NULL,
            updated_at    REAL    NOT NULL,
            UNIQUE(connection_id, name)
        );
        CREATE INDEX idx_tags_connection ON plc_tags(connection_id);

        CREATE TABLE audit_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       REAL NOT NULL,
            username TEXT NOT NULL,
            action   TEXT NOT NULL,
            target   TEXT,
            detail   TEXT,
            ip       TEXT
        );
        CREATE INDEX idx_audit_ts ON audit_log(ts);
        """,
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION`. Returns the new version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, ddl in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(ddl)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current = version
    return current
