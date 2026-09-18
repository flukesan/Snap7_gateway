"""Windows Service wrapper (CLAUDE.md section 4.2).

Registers the gateway as a native Windows service with automatic restart on
failure. ``pywin32`` is imported lazily so the rest of the package - and the
whole Linux deployment - never depends on it.

Install (from an elevated prompt)::

    python -m snap7_gateway.service.windows_service install
    python -m snap7_gateway.service.windows_service start

Or through the CLI::

    snap7-gateway install-service --data-dir "C:\\ProgramData\\Snap7Gateway"
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

SERVICE_NAME = "Snap7Gateway"
SERVICE_DISPLAY_NAME = "Snap7 Industrial Gateway"
SERVICE_DESCRIPTION = (
    "Polls Siemens S7 PLCs and re-hosts their data as a virtual S7 CPU for DeviceWise."
)
ENV_DATA_DIR = "SNAP7_GATEWAY_DATA_DIR"

try:  # pragma: no cover - Windows only
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    PYWIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - the normal case on Linux
    PYWIN32_AVAILABLE = False
    win32serviceutil = object  # type: ignore[assignment]


if PYWIN32_AVAILABLE:  # pragma: no cover - Windows only

    class Snap7GatewayService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        """Service control handler that runs the gateway in a worker thread."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):  # type: ignore[no-untyped-def]
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._server = None
            self._thread: threading.Thread | None = None

        def SvcStop(self):  # type: ignore[no-untyped-def]
            """Asked to stop: flip uvicorn's flag and let the runtime unwind."""
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._server is not None:
                self._server.should_exit = True
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self):  # type: ignore[no-untyped-def]
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self._run()

        def _run(self) -> None:
            import asyncio

            from .server import _serve, build_server

            data_dir = os.environ.get(ENV_DATA_DIR)
            # A Windows service has no console, so console logging is off; the
            # rotating file handler and the Event Log carry the diagnostics.
            runtime, server = build_server(data_dir, console_logging=False)
            self._server = server

            def _target() -> None:
                try:
                    asyncio.run(_serve(server, handle_signals=False))
                except Exception:  # noqa: BLE001
                    logger.exception("gateway service thread failed")
                    runtime.crash.write_snapshot("windows_service_failure", sys.exc_info())

            self._thread = threading.Thread(target=_target, name="snap7-gateway", daemon=False)
            self._thread.start()
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
            self._thread.join(timeout=60)


def _require_pywin32() -> None:
    if not PYWIN32_AVAILABLE:
        raise SystemExit(
            "pywin32 is required for Windows service support. Install it with:\n"
            "    pip install \"snap7-gateway[windows]\""
        )


def install(data_dir: str | None = None, *, start_type: str = "auto") -> None:
    """Register the service and configure automatic restart on failure."""
    _require_pywin32()  # pragma: no cover - Windows only
    import subprocess  # pragma: no cover

    if data_dir:  # pragma: no cover
        # Persist the data directory for the service account, which does not
        # inherit the installing user's environment.
        subprocess.run(
            ["setx", "/M", ENV_DATA_DIR, str(data_dir)], check=False, capture_output=True
        )
        os.environ[ENV_DATA_DIR] = str(data_dir)

    win32serviceutil.InstallService(  # pragma: no cover
        f"{Snap7GatewayService.__module__}.Snap7GatewayService",
        SERVICE_NAME,
        SERVICE_DISPLAY_NAME,
        description=SERVICE_DESCRIPTION,
        startType=(
            win32service.SERVICE_AUTO_START
            if start_type == "auto"
            else win32service.SERVICE_DEMAND_START
        ),
    )
    # Restart after 5s, 10s, then every 30s; reset the counter after a day.
    subprocess.run(  # pragma: no cover
        [
            "sc", "failure", SERVICE_NAME,
            "reset=", "86400",
            "actions=", "restart/5000/restart/10000/restart/30000",
        ],
        check=False,
        capture_output=True,
    )
    print(f"Installed service '{SERVICE_NAME}'. Start it with: sc start {SERVICE_NAME}")


def uninstall() -> None:
    _require_pywin32()  # pragma: no cover - Windows only
    try:  # pragma: no cover
        win32serviceutil.StopService(SERVICE_NAME)
    except Exception:  # noqa: BLE001
        pass
    win32serviceutil.RemoveService(SERVICE_NAME)  # pragma: no cover
    print(f"Removed service '{SERVICE_NAME}'.")


if __name__ == "__main__":  # pragma: no cover - Windows only
    _require_pywin32()
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(Snap7GatewayService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(Snap7GatewayService)
