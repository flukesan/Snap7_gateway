"""Bounded in-memory tail of recent log records.

Two consumers need "the last N log lines" without touching disk: the web UI
Logs page (fast severity-filtered view) and the crash snapshot writer. A
``collections.deque`` with a hard ``maxlen`` gives both, and satisfies the
"no unbounded memory growth" requirement in CLAUDE.md section 3.1 - the buffer
can never exceed ``maxlen`` entries no matter how noisy the process gets.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterable

DEFAULT_CAPACITY = 2000


@dataclass(frozen=True)
class LogEntry:
    """One formatted log record retained in memory."""

    created: float
    level: str
    levelno: int
    module: str
    connection_id: str
    message: str
    formatted: str


class LogRingBuffer:
    """Thread-safe fixed-capacity ring buffer of :class:`LogEntry`."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._entries.maxlen or DEFAULT_CAPACITY

    def append(self, entry: LogEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def tail(self, limit: int = 200, min_level: int = logging.NOTSET) -> list[LogEntry]:
        """Return up to ``limit`` most recent entries at or above ``min_level``.

        Entries are returned oldest-first so the UI can render them in natural
        reading order.
        """
        with self._lock:
            snapshot: Iterable[LogEntry] = list(self._entries)
        filtered = [e for e in snapshot if e.levelno >= min_level]
        if limit <= 0:
            return []
        return filtered[-limit:]

    def formatted_tail(self, limit: int = 200) -> list[str]:
        return [e.formatted for e in self.tail(limit=limit)]


class RingBufferHandler(logging.Handler):
    """Logging handler that feeds a :class:`LogRingBuffer`."""

    def __init__(self, buffer: LogRingBuffer) -> None:
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            entry = LogEntry(
                created=record.created,
                level=record.levelname,
                levelno=record.levelno,
                module=record.name,
                connection_id=str(getattr(record, "connection_id", "-")),
                message=record.getMessage(),
                formatted=self.format(record),
            )
            self.buffer.append(entry)
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            self.handleError(record)


_GLOBAL_BUFFER: LogRingBuffer | None = None
_GLOBAL_LOCK = threading.Lock()


def get_ring_buffer(capacity: int = DEFAULT_CAPACITY) -> LogRingBuffer:
    """Return the process-wide ring buffer, creating it on first use."""
    global _GLOBAL_BUFFER
    with _GLOBAL_LOCK:
        if _GLOBAL_BUFFER is None:
            _GLOBAL_BUFFER = LogRingBuffer(capacity)
        return _GLOBAL_BUFFER
