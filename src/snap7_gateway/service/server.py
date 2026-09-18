"""Process entrypoint: build the runtime and serve the web UI over HTTPS.

This is the one place that knows about uvicorn. Both the Linux systemd unit and
the Windows service wrapper call :func:`run_gateway`, so the two platforms run
byte-identical business logic and differ only in how the process is supervised
(CLAUDE.md section 4.2).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

import uvicorn

from ..core.runtime import GatewayRuntime
from ..web.app import create_app
from ..web.tls import ensure_certificate

logger = logging.getLogger(__name__)


def build_server(
    data_dir: str | Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    console_logging: bool = True,
    trust_proxy_headers: bool = False,
) -> tuple[GatewayRuntime, uvicorn.Server]:
    """Construct the runtime and a configured (not yet started) uvicorn server."""
    runtime = GatewayRuntime(data_dir, console_logging=console_logging)
    app = create_app(runtime, trust_proxy_headers=trust_proxy_headers)

    bind_host = host or runtime.db.get_setting("web_host", "0.0.0.0")
    bind_port = port or runtime.db.get_int("web_port", 8443)
    use_https = runtime.db.get_bool("https_enabled", True)

    ssl_kwargs: dict[str, Any] = {}
    if use_https:
        cert_path, key_path = ensure_certificate(
            runtime.paths.cert_dir,
            cert_override=runtime.db.get_setting("tls_cert_path", ""),
            key_override=runtime.db.get_setting("tls_key_path", ""),
        )
        ssl_kwargs = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
    else:
        logger.warning(
            "HTTPS is disabled - the configuration UI, including credentials, will be sent "
            "in clear text. Only acceptable on an isolated bench network."
        )

    config = uvicorn.Config(
        app,
        host=bind_host,
        port=bind_port,
        log_config=None,  # the gateway owns logging configuration
        access_log=False,
        server_header=False,
        date_header=True,
        timeout_graceful_shutdown=15,
        **ssl_kwargs,
    )
    server = uvicorn.Server(config)
    # The gateway installs its own signal handling where appropriate; on Windows
    # the service wrapper drives shutdown through should_exit instead.
    server.install_signal_handlers = False  # type: ignore[method-assign]
    logger.info(
        "web UI will listen on %s://%s:%s",
        "https" if use_https else "http",
        bind_host,
        bind_port,
    )
    return runtime, server


async def _serve(server: uvicorn.Server, *, handle_signals: bool = True) -> None:
    if handle_signals and sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
    await server.serve()


def run_gateway(
    data_dir: str | Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    console_logging: bool = True,
    trust_proxy_headers: bool = False,
) -> int:
    """Run the gateway until the process is asked to stop. Returns an exit code.

    The runtime's own lifespan (started by the ASGI app) owns subsystem startup
    and shutdown, so a failure to bind the web port still tears the polling
    workers down cleanly.
    """
    runtime, server = build_server(
        data_dir,
        host=host,
        port=port,
        console_logging=console_logging,
        trust_proxy_headers=trust_proxy_headers,
    )
    try:
        asyncio.run(_serve(server))
        return 0
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        logger.info("interrupted, shutting down")
        return 0
    except Exception as exc:  # noqa: BLE001 - last chance to record why we died
        logger.exception("gateway terminated with an error: %s", exc)
        runtime.crash.write_snapshot(
            "service_exit", (type(exc), exc, exc.__traceback__)
        )
        return 1
