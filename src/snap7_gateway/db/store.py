"""SQLite-backed configuration store.

One connection is shared process-wide behind a re-entrant lock. SQLite handles
the concurrency fine in WAL mode, and a single guarded connection avoids both
the "same thread" restriction and the file-handle churn of connection-per-call
on an edge device. Every write commits immediately (CLAUDE.md section 4.3:
settings are persisted on save, not batched).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .defaults import DEFAULT_SETTINGS
from .models import (
    AreaStatus,
    AreaType,
    AuditEntry,
    PlcArea,
    PlcConnection,
    PlcTag,
    Session,
    User,
)
from .schema import apply_migrations

logger = logging.getLogger(__name__)

AUDIT_RETENTION_ROWS = 20_000


class Database:
    """Thread-safe accessor for the gateway's SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # explicit transaction control
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = FULL")  # power-loss safety
        with self._lock:
            apply_migrations(self._conn)
        self._seed_settings()
        if self.path.name != ":memory:":
            try:
                self.path.chmod(0o600)  # contains password hashes
            except OSError:
                pass

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def _seed_settings(self) -> None:
        now = time.time()
        with self._lock:
            for key, value in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
            self._conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str:
        row = self._query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is not None:
            return row["value"]
        if default is not None:
            return default
        return DEFAULT_SETTINGS.get(key, "")

    def get_int(self, key: str, default: int | None = None) -> int:
        raw = self.get_setting(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            if default is not None:
                return default
            return int(DEFAULT_SETTINGS.get(key, "0") or 0)

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = (self.get_setting(key) or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default

    def get_settings(self) -> dict[str, str]:
        rows = self._query_all("SELECT key, value FROM settings")
        merged = dict(DEFAULT_SETTINGS)
        merged.update({row["key"]: row["value"] for row in rows})
        return merged

    def set_setting(self, key: str, value: str) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, str]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for key, value in values.items():
                    self._conn.execute(
                        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = excluded.updated_at",
                        (key, str(value), now),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        role: str = "admin",
        must_change_password: bool = True,
    ) -> User:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO users(username, password_hash, role, must_change_password, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (username, password_hash, role, int(must_change_password), now, now),
            )
            self._conn.commit()
            user_id = cur.lastrowid
        user = self.get_user_by_id(int(user_id))
        assert user is not None
        return user

    def get_user_by_id(self, user_id: int) -> User | None:
        row = self._query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        return User.from_row(row) if row else None

    def get_user(self, username: str) -> User | None:
        row = self._query_one("SELECT * FROM users WHERE username = ?", (username,))
        return User.from_row(row) if row else None

    def list_users(self) -> list[User]:
        return [User.from_row(r) for r in self._query_all("SELECT * FROM users ORDER BY username")]

    def count_users(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS c FROM users")
        return int(row["c"]) if row else 0

    def update_password(self, user_id: int, new_hash: str, previous_hash: str | None) -> None:
        now = time.time()
        self._commit(
            "UPDATE users SET password_hash = ?, previous_hash = ?, must_change_password = 0, "
            "password_changed_at = ?, updated_at = ?, failed_attempts = 0, locked_until = NULL "
            "WHERE id = ?",
            (new_hash, previous_hash, now, now, user_id),
        )

    def set_user_role(self, user_id: int, role: str) -> None:
        self._commit(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, time.time(), user_id)
        )

    def set_user_enabled(self, user_id: int, enabled: bool) -> None:
        self._commit(
            "UPDATE users SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), time.time(), user_id),
        )

    def set_must_change_password(self, user_id: int, must_change: bool) -> None:
        self._commit(
            "UPDATE users SET must_change_password = ?, updated_at = ? WHERE id = ?",
            (int(must_change), time.time(), user_id),
        )

    def delete_user(self, user_id: int) -> None:
        self._commit("DELETE FROM users WHERE id = ?", (user_id,))

    def record_login_success(self, user_id: int) -> None:
        self._commit(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ?, "
            "updated_at = ? WHERE id = ?",
            (time.time(), time.time(), user_id),
        )

    def record_login_failure(self, user_id: int, max_failures: int, lockout_seconds: int) -> User:
        """Increment the failure counter and lock the account past the threshold."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE users SET failed_attempts = failed_attempts + 1, updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            row = self._conn.execute(
                "SELECT failed_attempts FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            attempts = int(row["failed_attempts"]) if row else 0
            if max_failures > 0 and attempts >= max_failures:
                self._conn.execute(
                    "UPDATE users SET locked_until = ? WHERE id = ?",
                    (now + lockout_seconds, user_id),
                )
            self._conn.commit()
        user = self.get_user_by_id(user_id)
        assert user is not None
        return user

    def unlock_user(self, user_id: int) -> None:
        self._commit(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (time.time(), user_id),
        )

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def create_session(
        self,
        token_hash: str,
        user_id: int,
        csrf_token: str,
        expires_at: float,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        now = time.time()
        self._commit(
            "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, last_seen_at, "
            "expires_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token_hash, user_id, csrf_token, now, now, expires_at, ip, user_agent),
        )

    def get_session(self, token_hash: str) -> Session | None:
        row = self._query_one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
        return Session.from_row(row) if row else None

    def touch_session(self, token_hash: str, expires_at: float) -> None:
        self._commit(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
            (time.time(), expires_at, token_hash),
        )

    def delete_session(self, token_hash: str) -> None:
        self._commit("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_sessions_for_user(self, user_id: int) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._conn.commit()
            return cur.rowcount

    def purge_expired_sessions(self, now: float | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (now or time.time(),)
            )
            self._conn.commit()
            return cur.rowcount

    def list_sessions_for_user(self, user_id: int) -> list[Session]:
        rows = self._query_all(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC", (user_id,)
        )
        return [Session.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # PLC connections
    # ------------------------------------------------------------------
    _CONN_FIELDS = (
        "name",
        "host",
        "rack",
        "slot",
        "tcp_port",
        "connection_type",
        "timeout_ms",
        "poll_interval_ms",
        "exposure_mode",
        "write_enabled",
        "enabled",
        "read_inputs",
        "read_outputs",
        "read_merkers",
        "max_input_bytes",
        "max_output_bytes",
        "max_merker_bytes",
    )

    def create_connection(self, values: dict[str, Any]) -> PlcConnection:
        now = time.time()
        payload = {k: values[k] for k in self._CONN_FIELDS if k in values}
        columns = ", ".join([*payload, "created_at", "updated_at"])
        markers = ", ".join(["?"] * (len(payload) + 2))
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO plc_connections({columns}) VALUES ({markers})",
                (*[_coerce(v) for v in payload.values()], now, now),
            )
            self._conn.commit()
            new_id = int(cur.lastrowid or 0)
        conn = self.get_connection(new_id)
        assert conn is not None
        return conn

    def update_connection(self, connection_id: int, values: dict[str, Any]) -> PlcConnection | None:
        payload = {k: values[k] for k in self._CONN_FIELDS if k in values}
        if not payload:
            return self.get_connection(connection_id)
        assignments = ", ".join(f"{k} = ?" for k in payload)
        self._commit(
            f"UPDATE plc_connections SET {assignments}, updated_at = ? WHERE id = ?",
            (*[_coerce(v) for v in payload.values()], time.time(), connection_id),
        )
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id: int) -> None:
        self._commit("DELETE FROM plc_connections WHERE id = ?", (connection_id,))

    def get_connection(self, connection_id: int) -> PlcConnection | None:
        row = self._query_one("SELECT * FROM plc_connections WHERE id = ?", (connection_id,))
        return PlcConnection.from_row(row) if row else None

    def get_connection_by_name(self, name: str) -> PlcConnection | None:
        row = self._query_one("SELECT * FROM plc_connections WHERE name = ?", (name,))
        return PlcConnection.from_row(row) if row else None

    def list_connections(self, *, enabled_only: bool = False) -> list[PlcConnection]:
        sql = "SELECT * FROM plc_connections"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        return [PlcConnection.from_row(r) for r in self._query_all(sql)]

    # ------------------------------------------------------------------
    # discovered areas
    # ------------------------------------------------------------------
    def list_areas(self, connection_id: int | None = None) -> list[PlcArea]:
        if connection_id is None:
            rows = self._query_all(
                "SELECT * FROM plc_areas ORDER BY connection_id, area_type, db_number"
            )
        else:
            rows = self._query_all(
                "SELECT * FROM plc_areas WHERE connection_id = ? ORDER BY area_type, db_number",
                (connection_id,),
            )
        return [PlcArea.from_row(r) for r in rows]

    def get_area(self, connection_id: int, area_type: str, db_number: int) -> PlcArea | None:
        row = self._query_one(
            "SELECT * FROM plc_areas WHERE connection_id = ? AND area_type = ? AND db_number = ?",
            (connection_id, str(area_type), db_number),
        )
        return PlcArea.from_row(row) if row else None

    def get_area_by_id(self, area_id: int) -> PlcArea | None:
        row = self._query_one("SELECT * FROM plc_areas WHERE id = ?", (area_id,))
        return PlcArea.from_row(row) if row else None

    def upsert_area(
        self,
        connection_id: int,
        area_type: str,
        db_number: int,
        size_bytes: int,
        *,
        status: str,
        status_detail: str | None = None,
        default_exposed: bool = False,
    ) -> PlcArea:
        """Insert or refresh a discovered area.

        ``default_exposed`` only applies on first insert: an operator's later
        whitelist decision is never overwritten by a rescan (CLAUDE.md 4.7 -
        never silently expose new data).
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO plc_areas(connection_id, area_type, db_number, size_bytes, exposed, "
                "status, status_detail, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(connection_id, area_type, db_number) DO UPDATE SET "
                "size_bytes = excluded.size_bytes, status = excluded.status, "
                "status_detail = excluded.status_detail, last_seen_at = excluded.last_seen_at",
                (
                    connection_id,
                    str(area_type),
                    db_number,
                    size_bytes,
                    int(default_exposed),
                    str(status),
                    status_detail,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        area = self.get_area(connection_id, area_type, db_number)
        assert area is not None
        return area

    def set_area_status(
        self, area_id: int, status: str, status_detail: str | None = None
    ) -> None:
        self._commit(
            "UPDATE plc_areas SET status = ?, status_detail = ? WHERE id = ?",
            (str(status), status_detail, area_id),
        )

    def set_area_exposed(self, area_id: int, exposed: bool) -> None:
        self._commit("UPDATE plc_areas SET exposed = ? WHERE id = ?", (int(exposed), area_id))

    def set_area_name(self, area_id: int, friendly_name: str | None) -> None:
        self._commit(
            "UPDATE plc_areas SET friendly_name = ? WHERE id = ?", (friendly_name, area_id)
        )

    def delete_area(self, area_id: int) -> None:
        self._commit("DELETE FROM plc_areas WHERE id = ?", (area_id,))

    # ------------------------------------------------------------------
    # tags
    # ------------------------------------------------------------------
    _TAG_FIELDS = (
        "name",
        "area_type",
        "db_number",
        "byte_offset",
        "bit_offset",
        "data_type",
        "length",
        "description",
    )

    def create_tag(self, connection_id: int, values: dict[str, Any]) -> PlcTag:
        now = time.time()
        payload = {k: values[k] for k in self._TAG_FIELDS if k in values}
        columns = ", ".join(["connection_id", *payload, "created_at", "updated_at"])
        markers = ", ".join(["?"] * (len(payload) + 3))
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO plc_tags({columns}) VALUES ({markers})",
                (connection_id, *[_coerce(v) for v in payload.values()], now, now),
            )
            self._conn.commit()
            tag_id = int(cur.lastrowid or 0)
        tag = self.get_tag(tag_id)
        assert tag is not None
        return tag

    def update_tag(self, tag_id: int, values: dict[str, Any]) -> PlcTag | None:
        payload = {k: values[k] for k in self._TAG_FIELDS if k in values}
        if not payload:
            return self.get_tag(tag_id)
        assignments = ", ".join(f"{k} = ?" for k in payload)
        self._commit(
            f"UPDATE plc_tags SET {assignments}, updated_at = ? WHERE id = ?",
            (*[_coerce(v) for v in payload.values()], time.time(), tag_id),
        )
        return self.get_tag(tag_id)

    def get_tag(self, tag_id: int) -> PlcTag | None:
        row = self._query_one("SELECT * FROM plc_tags WHERE id = ?", (tag_id,))
        return PlcTag.from_row(row) if row else None

    def list_tags(self, connection_id: int | None = None) -> list[PlcTag]:
        if connection_id is None:
            rows = self._query_all("SELECT * FROM plc_tags ORDER BY connection_id, name")
        else:
            rows = self._query_all(
                "SELECT * FROM plc_tags WHERE connection_id = ? ORDER BY name", (connection_id,)
            )
        return [PlcTag.from_row(r) for r in rows]

    def delete_tag(self, tag_id: int) -> None:
        self._commit("DELETE FROM plc_tags WHERE id = ?", (tag_id,))

    # ------------------------------------------------------------------
    # audit trail
    # ------------------------------------------------------------------
    def audit(
        self,
        username: str,
        action: str,
        target: str | None = None,
        detail: str | dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        """Append one audit row (CLAUDE.md section 4.8: who, what, when).

        Callers must never pass secrets in ``detail``; helpers that touch
        credentials record the action only.
        """
        if isinstance(detail, dict):
            detail = json.dumps(detail, default=str, ensure_ascii=False)
        self._commit(
            "INSERT INTO audit_log(ts, username, action, target, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), username, action, target, detail, ip),
        )
        logger.info("audit: user=%s action=%s target=%s", username, action, target)

    def list_audit(self, limit: int = 200, offset: int = 0) -> list[AuditEntry]:
        rows = self._query_all(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [AuditEntry.from_row(r) for r in rows]

    def prune_audit(self, keep_rows: int = AUDIT_RETENTION_ROWS) -> int:
        """Bound audit growth - an edge device's disk is finite."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM audit_log WHERE id NOT IN "
                "(SELECT id FROM audit_log ORDER BY ts DESC LIMIT ?)",
                (keep_rows,),
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    def _commit(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()


def _coerce(value: Any) -> Any:
    """SQLite has no bool type; store flags as 0/1 and enums as their text."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (AreaType, AreaStatus)):
        return str(value)
    return value


def iter_area_keys(areas: Iterable[PlcArea]) -> list[str]:
    return [a.key for a in areas]
