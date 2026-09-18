"""Crash snapshot capture (CLAUDE.md section 4.8).

A silent restart of a 24/7 service is the hardest class of field problem to
debug, so on any unhandled exception - on the main thread, on a worker thread,
or inside an asyncio task - the gateway writes a self-contained snapshot to
``<data-dir>/crash/``:

  * the full Python traceback,
  * the last N formatted log lines from the in-memory ring buffer,
  * a state dump from every registered state provider (connection health,
    virtual-PLC registration table, uptime),
  * interpreter/platform/thread context.

A ``running.marker`` file is also maintained: it is created at startup and
removed on a clean shutdown, so a marker still present at the next boot proves
the previous run died without unwinding (power loss, SIGKILL, OOM kill) even
though no Python-level exception was ever raised.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

from .ringbuffer import LogRingBuffer

logger = logging.getLogger(__name__)

StateProvider = Callable[[], Any]

MAX_SNAPSHOT_FILES = 50
DEFAULT_LOG_LINES = 500

#: Transport failures that mean "the peer went away", not "the gateway broke".
#: A browser closing a tab, a TLS warning page being dismissed, or DeviceWise
#: dropping a socket all raise these, and on Windows the Proactor event loop
#: reports them through the loop exception handler. Snapshotting them would
#: bury real crashes under routine noise - the snapshot folder is capped, so
#: junk entries actively push genuine ones out.
ROUTINE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)


class CrashReporter:
    """Writes crash snapshots and tracks unclean shutdowns."""

    def __init__(
        self,
        crash_dir: Path,
        buffer: LogRingBuffer,
        marker_file: Path | None = None,
        *,
        log_lines: int = DEFAULT_LOG_LINES,
        max_files: int = MAX_SNAPSHOT_FILES,
    ) -> None:
        self.crash_dir = Path(crash_dir)
        self.buffer = buffer
        self.marker_file = Path(marker_file) if marker_file else None
        self.log_lines = log_lines
        self.max_files = max_files
        self._providers: dict[str, StateProvider] = {}
        self._lock = threading.Lock()
        self._previous_unclean: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # State providers
    # ------------------------------------------------------------------
    def register_state_provider(self, name: str, provider: StateProvider) -> None:
        """Register a callable whose return value is included in snapshots.

        Providers run inside the crash path, so they must be cheap and must not
        raise; a provider that raises is recorded as an error string instead of
        aborting the snapshot.
        """
        with self._lock:
            self._providers[name] = provider

    def unregister_state_provider(self, name: str) -> None:
        with self._lock:
            self._providers.pop(name, None)

    def _collect_state(self) -> dict[str, Any]:
        with self._lock:
            providers = dict(self._providers)
        state: dict[str, Any] = {}
        for name, provider in providers.items():
            try:
                state[name] = provider()
            except Exception as exc:  # noqa: BLE001 - never fail a crash dump
                state[name] = f"<state provider failed: {exc!r}>"
        return state

    # ------------------------------------------------------------------
    # Unclean shutdown marker
    # ------------------------------------------------------------------
    def check_previous_shutdown(self) -> dict[str, Any] | None:
        """Inspect (and consume) the marker left by the previous run.

        Returns a dict describing the unclean shutdown, or ``None`` when the
        previous run exited cleanly. Must be called once at startup, before
        :meth:`mark_running`.
        """
        if self.marker_file is None or not self.marker_file.exists():
            return None
        info: dict[str, Any] = {"detected_at": _now_iso()}
        try:
            info.update(json.loads(self.marker_file.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - corrupt marker is still a signal
            info["marker_error"] = repr(exc)
        info["reason"] = "previous run did not shut down cleanly"
        self._previous_unclean = info
        try:
            self.write_snapshot(
                reason="unclean_shutdown_detected",
                exc_info=None,
                extra={"previous_run": info},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to write unclean-shutdown snapshot")
        try:
            self.marker_file.unlink()
        except OSError:
            pass
        return info

    @property
    def previous_unclean_shutdown(self) -> dict[str, Any] | None:
        """Details of the last detected unclean shutdown, for the Status page."""
        return self._previous_unclean

    def mark_running(self) -> None:
        if self.marker_file is None:
            return
        payload = {
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "executable": sys.executable,
            "platform": platform.platform(),
        }
        try:
            self.marker_file.parent.mkdir(parents=True, exist_ok=True)
            self.marker_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not write running marker: %s", exc)

    def mark_stopped(self) -> None:
        if self.marker_file is None:
            return
        try:
            self.marker_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not clear running marker: %s", exc)

    # ------------------------------------------------------------------
    # Snapshot writing
    # ------------------------------------------------------------------
    def write_snapshot(
        self,
        reason: str,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path | None:
        """Write one crash snapshot. Returns its path, or ``None`` on failure."""
        try:
            self.crash_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
            path = self.crash_dir / f"crash_{stamp}_{reason}.json"

            tb_text = None
            exc_type = exc_value = None
            if exc_info is not None:
                exc_type, exc_value, exc_tb = exc_info
                tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))

            payload = {
                "reason": reason,
                "written_at": _now_iso(),
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
                "python": sys.version,
                "platform": platform.platform(),
                "exception_type": exc_type.__name__ if exc_type else None,
                "exception": str(exc_value) if exc_value else None,
                "traceback": tb_text,
                "state": self._collect_state(),
                "threads": _thread_dump(),
                "recent_logs": self.buffer.formatted_tail(self.log_lines),
            }
            if extra:
                payload["extra"] = extra

            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            self._prune()
            logger.error("crash snapshot written: %s (reason=%s)", path, reason)
            return path
        except Exception:  # noqa: BLE001 - the crash path itself must not crash
            try:
                traceback.print_exc(file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
            return None

    def list_snapshots(self) -> list[Path]:
        """Newest-first list of snapshot files, for the Status page."""
        if not self.crash_dir.exists():
            return []
        files = [p for p in self.crash_dir.glob("crash_*.json") if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def _prune(self) -> None:
        """Keep the crash folder bounded - it lives on the same disk as logs."""
        files = self.list_snapshots()
        for stale in files[self.max_files :]:
            try:
                stale.unlink()
            except OSError:
                pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _thread_dump() -> list[dict[str, Any]]:
    """Best-effort stack dump of every live thread."""
    frames = sys._current_frames()  # noqa: SLF001 - documented CPython API
    dump: list[dict[str, Any]] = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident or -1)
        stack = traceback.format_stack(frame) if frame is not None else []
        dump.append(
            {
                "name": thread.name,
                "ident": thread.ident,
                "daemon": thread.daemon,
                "alive": thread.is_alive(),
                "stack": stack[-20:],
            }
        )
    return dump


def install_crash_handlers(reporter: CrashReporter) -> CrashReporter:
    """Route unhandled exceptions from every execution context to ``reporter``.

    Covers the main thread (:data:`sys.excepthook`), worker threads
    (:func:`threading.excepthook`) and un-awaited asyncio task failures
    (:meth:`asyncio.AbstractEventLoop.set_exception_handler`, installed later by
    the runtime via :func:`install_asyncio_handler`).
    """
    previous_hook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            reporter.write_snapshot("unhandled_exception", (exc_type, exc_value, exc_tb))
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    previous_thread_hook = threading.excepthook

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit:
            reporter.write_snapshot(
                "unhandled_thread_exception",
                (args.exc_type, args.exc_value, args.exc_traceback)
                if args.exc_value is not None
                else None,
                extra={"thread": getattr(args.thread, "name", "?")},
            )
        previous_thread_hook(args)

    threading.excepthook = _thread_excepthook
    return reporter


def _is_routine_disconnect(exception: BaseException | None) -> bool:
    """Whether an asyncio failure is just a peer hanging up."""
    if exception is None:
        return False
    if isinstance(exception, ROUTINE_TRANSPORT_ERRORS):
        return True
    # SSL is optional at import time only in the sense that a build without it
    # cannot raise these at all.
    try:
        import ssl

        if isinstance(exception, (ssl.SSLError, ssl.SSLEOFError)):
            return True
    except ImportError:  # pragma: no cover - CPython always ships ssl
        pass
    # Windows reports a reset as OSError with WinError 10053/10054 in some paths.
    return isinstance(exception, OSError) and getattr(exception, "winerror", None) in {
        10053,
        10054,
        10058,
    }


def install_asyncio_handler(loop: Any, reporter: CrashReporter) -> None:
    """Capture exceptions that escape asyncio tasks."""

    def _handler(_loop: Any, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        message = context.get("message")

        if _is_routine_disconnect(exception):
            # Expected on any network service: log it and move on.
            logger.debug(
                "client connection dropped (%s): %s", type(exception).__name__, message
            )
            return

        exc_info = (
            (type(exception), exception, exception.__traceback__) if exception else None
        )
        reporter.write_snapshot(
            "asyncio_task_exception",
            exc_info,
            extra={"message": message, "future": str(context.get("future"))},
        )
        logger.error("asyncio exception: %s", message, exc_info=exception)

    loop.set_exception_handler(_handler)
