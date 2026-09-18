"""In-memory store of the newest good read for every polled area.

This is the seam between the two Snap7 roles: the client-role workers publish
here, the server-role sync task consumes from here. Two invariants matter:

* **Bounded.** One entry per configured area, replaced in place. There is no
  history and no queue, so memory is a function of configuration, not uptime
  (CLAUDE.md section 3.1).
* **Freshness is explicit.** Every snapshot carries the timestamp of the read
  that produced it and whether that read succeeded. The virtual PLC uses this to
  refuse to serve stale bytes (CLAUDE.md section 6).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Iterator

from ..db.models import AreaType


@dataclass(frozen=True, slots=True)
class AreaKey:
    """Identity of one polled area."""

    connection_id: int
    area_type: str
    db_number: int = 0

    @property
    def label(self) -> str:
        if self.area_type == AreaType.DB:
            return f"DB{self.db_number}"
        return str(self.area_type)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"conn{self.connection_id}/{self.label}"


@dataclass(frozen=True, slots=True)
class AreaSnapshot:
    """The newest read result for an area."""

    key: AreaKey
    data: bytes
    size: int
    read_at: float
    ok: bool
    error: str | None = None
    duration_ms: float = 0.0

    def age(self, now: float | None = None) -> float:
        """Seconds since this snapshot's read completed."""
        return max(0.0, (now if now is not None else time.time()) - self.read_at)

    def is_fresh(self, max_age_seconds: float, now: float | None = None) -> bool:
        """Whether the data is good *and* recent enough to serve downstream."""
        return self.ok and self.age(now) <= max_age_seconds


class ConnectionState(StrEnum):
    """Per-connection health shown on the web UI (CLAUDE.md section 4.1)."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(slots=True)
class ConnectionHealth:
    """Live status of one PLC connection."""

    connection_id: int
    name: str
    state: str = ConnectionState.STOPPED
    last_success_at: float | None = None
    last_attempt_at: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_polls: int = 0
    total_errors: int = 0
    connected_since: float | None = None
    next_retry_at: float | None = None
    last_poll_duration_ms: float = 0.0
    cpu: dict[str, str] = field(default_factory=dict)
    last_discovery_at: float | None = None
    last_discovery_summary: str = ""
    polled_areas: int = 0

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view for the status API and crash snapshots."""
        return {
            "connection_id": self.connection_id,
            "name": self.name,
            "state": str(self.state),
            "last_success_at": self.last_success_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_polls": self.total_polls,
            "total_errors": self.total_errors,
            "connected_since": self.connected_since,
            "next_retry_at": self.next_retry_at,
            "last_poll_duration_ms": round(self.last_poll_duration_ms, 2),
            "cpu": dict(self.cpu),
            "last_discovery_at": self.last_discovery_at,
            "last_discovery_summary": self.last_discovery_summary,
            "polled_areas": self.polled_areas,
        }


class DataStore:
    """Thread-safe latest-value store shared by the client and server roles."""

    def __init__(self) -> None:
        self._snapshots: dict[AreaKey, AreaSnapshot] = {}
        self._health: dict[int, ConnectionHealth] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def publish(
        self,
        key: AreaKey,
        data: bytes,
        *,
        duration_ms: float = 0.0,
        read_at: float | None = None,
    ) -> AreaSnapshot:
        """Record a successful read."""
        snapshot = AreaSnapshot(
            key=key,
            data=bytes(data),
            size=len(data),
            read_at=read_at if read_at is not None else time.time(),
            ok=True,
            error=None,
            duration_ms=duration_ms,
        )
        with self._lock:
            self._snapshots[key] = snapshot
        return snapshot

    def publish_failure(self, key: AreaKey, error: str) -> AreaSnapshot:
        """Record a failed read, keeping the previous payload but marking it bad.

        The payload is retained purely for diagnostics; ``ok=False`` means no
        consumer may serve it, and :meth:`fresh` will not return it.
        """
        with self._lock:
            previous = self._snapshots.get(key)
            snapshot = AreaSnapshot(
                key=key,
                data=previous.data if previous else b"",
                size=previous.size if previous else 0,
                read_at=previous.read_at if previous else 0.0,
                ok=False,
                error=error,
            )
            self._snapshots[key] = snapshot
        return snapshot

    def get(self, key: AreaKey) -> AreaSnapshot | None:
        with self._lock:
            return self._snapshots.get(key)

    def fresh(self, key: AreaKey, max_age_seconds: float) -> AreaSnapshot | None:
        """Return the snapshot only when it is good and within ``max_age``."""
        snapshot = self.get(key)
        if snapshot is None or not snapshot.is_fresh(max_age_seconds):
            return None
        return snapshot

    def keys(self) -> list[AreaKey]:
        with self._lock:
            return list(self._snapshots)

    def items(self) -> list[tuple[AreaKey, AreaSnapshot]]:
        with self._lock:
            return list(self._snapshots.items())

    def for_connection(self, connection_id: int) -> list[AreaSnapshot]:
        with self._lock:
            return [s for k, s in self._snapshots.items() if k.connection_id == connection_id]

    def drop(self, key: AreaKey) -> None:
        with self._lock:
            self._snapshots.pop(key, None)

    def retain(self, keys: set[AreaKey]) -> int:
        """Drop snapshots for areas that are no longer configured.

        Called after every config change and rescan so the store can never grow
        past the current configuration.
        """
        with self._lock:
            stale = [k for k in self._snapshots if k not in keys]
            for key in stale:
                del self._snapshots[key]
            return len(stale)

    def drop_connection(self, connection_id: int) -> None:
        with self._lock:
            for key in [k for k in self._snapshots if k.connection_id == connection_id]:
                del self._snapshots[key]
            self._health.pop(connection_id, None)

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def health(self, connection_id: int) -> ConnectionHealth | None:
        with self._lock:
            return self._health.get(connection_id)

    def set_health(self, health: ConnectionHealth) -> None:
        with self._lock:
            self._health[health.connection_id] = health

    def update_health(self, connection_id: int, **changes: object) -> ConnectionHealth | None:
        with self._lock:
            current = self._health.get(connection_id)
            if current is None:
                return None
            updated = replace(current, **changes)  # type: ignore[arg-type]
            self._health[connection_id] = updated
            return updated

    def all_health(self) -> list[ConnectionHealth]:
        with self._lock:
            return sorted(self._health.values(), key=lambda h: h.name.lower())

    def __iter__(self) -> Iterator[AreaSnapshot]:  # pragma: no cover - convenience
        return iter(list(self._snapshots.values()))

    def snapshot_summary(self) -> list[dict[str, object]]:
        """Compact view used by crash snapshots and the status API."""
        now = time.time()
        with self._lock:
            entries = list(self._snapshots.items())
        return [
            {
                "key": str(key),
                "size": snap.size,
                "ok": snap.ok,
                "age_seconds": round(snap.age(now), 3) if snap.read_at else None,
                "error": snap.error,
            }
            for key, snap in sorted(entries, key=lambda kv: str(kv[0]))
        ]
