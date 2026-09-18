"""Structured logging configuration for the gateway.

Format (CLAUDE.md section 4.8): timestamp, level, module, connection id, message.

    2026-01-31T09:12:44.512+0000 | INFO     | plc.client      | conn=3 | connected

Handlers installed:
  * ``RotatingFileHandler`` - size + backup capped so the disk can never fill.
  * ``StreamHandler``       - console, useful under systemd/journald and for
    first-boot credential output.
  * ``RingBufferHandler``   - in-memory tail for the web Logs page and crash
    snapshots.

Nothing here ever logs credentials or session tokens; callers are responsible
for redaction, and :func:`redact` is provided for the few places that need it.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .ringbuffer import LogRingBuffer, RingBufferHandler, get_ring_buffer

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | conn=%(connection_id)-8s | %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per file
DEFAULT_BACKUP_COUNT = 10  # -> at most ~110 MiB of logs on disk

_configured = False


class GatewayLogRecord(logging.Filter):
    """Guarantee every record carries a ``connection_id`` attribute.

    Records emitted by modules that know nothing about PLC connections (the web
    layer, service bootstrap) still have to render under the same format
    string, so a placeholder is injected when the attribute is absent.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "connection_id"):
            record.connection_id = "-"
        return True


class _IsoFormatter(logging.Formatter):
    """Formatter emitting ISO-8601 timestamps with milliseconds and offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        base = time.strftime(datefmt or DATE_FORMAT, time.localtime(record.created))
        offset = time.strftime("%z", time.localtime(record.created)) or "+0000"
        return f"{base}.{int(record.msecs):03d}{offset}"


def redact(value: str | None, keep: int = 4) -> str:
    """Render a secret safe for logging: ``abcd...`` with the tail dropped.

    Used for things like session token identifiers where correlating two log
    lines is useful but the full value must never reach disk.
    """
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 8}"


def configure_logging(
    log_file: Path,
    *,
    level: int | str = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    console: bool = True,
    ring_capacity: int = 2000,
    force: bool = False,
) -> LogRingBuffer:
    """Install the gateway's handlers on the root logger.

    Returns the in-memory ring buffer so callers can hand it to the web layer
    and the crash reporter. Calling twice is a no-op unless ``force`` is set,
    which keeps test runs and service restarts from stacking handlers.
    """
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        return get_ring_buffer(ring_capacity)
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - closing must not break startup
                pass

    if isinstance(level, str):
        level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = _IsoFormatter(LOG_FORMAT, DATE_FORMAT)
    record_filter = GatewayLogRecord()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max(max_bytes, 64 * 1024),
        backupCount=max(backup_count, 1),
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(record_filter)
    root.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(record_filter)
        root.addHandler(stream_handler)

    buffer = get_ring_buffer(ring_capacity)
    ring_handler = RingBufferHandler(buffer)
    ring_handler.setFormatter(formatter)
    ring_handler.addFilter(record_filter)
    root.addHandler(ring_handler)

    root.setLevel(level)

    # Third-party chatter that would otherwise drown the operator's own logs.
    logging.getLogger("uvicorn.access").setLevel(max(level, logging.WARNING))
    logging.getLogger("multipart").setLevel(logging.WARNING)

    _configured = True
    return buffer


def set_level(level: int | str) -> None:
    """Change the effective log level at runtime (web UI Logs page)."""
    if isinstance(level, str):
        level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)


def connection_logger(name: str, connection_id: int | str) -> logging.LoggerAdapter:
    """Return a logger that stamps every record with a connection id."""
    extra: Mapping[str, Any] = {"connection_id": connection_id}
    return logging.LoggerAdapter(logging.getLogger(name), extra)
