"""FastAPI application factory for the configuration UI.

Framework choice (recorded in ``docs/architecture.md``): FastAPI + uvicorn.
The gateway is asynchronous at its core - supervised polling tasks, the virtual
CPU sync loop - and running the UI on the same event loop means the web layer
reads live state directly instead of through a second process or a shared file.

The UI itself is deliberately plain: server-rendered Jinja2, a single hand-
written stylesheet, a few lines of vanilla JS. No icon fonts, no imagery, no
CDN - factory networks are air-gapped and the brief calls for text labels only.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..auth.sessions import SESSION_COOKIE_NAME
from ..core.runtime import GatewayRuntime
from . import deps
from .i18n import LOCALE_NAMES, SUPPORTED_LOCALES, Translator
from .routes import api, connections, devicewise, logs, security, status_page, tags, tools
from .routes import auth as auth_routes

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    runtime: GatewayRuntime,
    *,
    manage_runtime: bool = True,
    trust_proxy_headers: bool = False,
) -> FastAPI:
    """Build the ASGI application around an existing :class:`GatewayRuntime`.

    ``manage_runtime`` controls whether the app starts and stops the runtime in
    its lifespan. The service entrypoint leaves it on; tests that drive the
    runtime themselves turn it off.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if manage_runtime:
            await runtime.start()
        try:
            yield
        finally:
            if manage_runtime:
                await runtime.stop()

    app = FastAPI(
        title="Snap7 Industrial Gateway",
        docs_url=None,  # no interactive API docs on an industrial network
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.trust_proxy_headers = trust_proxy_headers

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals.update(
        fmt_ts=deps.format_timestamp,
        rel_age=deps.relative_age,
        duration=deps.humanize_duration,
        locale_names=LOCALE_NAMES,
        supported_locales=SUPPORTED_LOCALES,
    )
    # Autoescaping is on by default in Jinja2Templates; every value rendered
    # from PLC data or operator input therefore gets escaped.
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # middleware
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def session_and_guards(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Resolve the session, enforce the access rules, add security headers.

        The forced-password-change rule from CLAUDE.md section 4.4 is enforced
        here rather than per-route, so a newly added page cannot accidentally be
        reachable before the first-login change is done.
        """
        runtime_ = request.app.state.runtime
        token = request.cookies.get(SESSION_COOKIE_NAME)
        context = runtime_.auth.sessions.validate(token)
        request.state.session = context
        request.state.translator = Translator(runtime_.db.get_setting("locale", "en"))

        path = request.url.path
        is_static = path.startswith("/static/")

        if not is_static and path not in deps.PUBLIC_PATHS:
            if context is None:
                if path.startswith("/api/"):
                    return JSONResponse({"error": "not signed in"}, status_code=401)
                return deps.redirect(f"/login?next={path}")
            if context.user.must_change_password and path not in deps.PASSWORD_CHANGE_PATHS:
                return deps.redirect("/password-change")

        response = await call_next(request)

        # Conservative headers: everything is same-origin and self-hosted, so a
        # strict CSP costs nothing and blocks injected third-party content.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    # ------------------------------------------------------------------
    # error handling
    # ------------------------------------------------------------------
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return deps.redirect("/login")
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            deps.flash(request, "error", str(exc.detail))
            referer = request.headers.get("referer")
            return deps.redirect(referer if referer and referer.startswith("/") else "/")
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {
                "t": deps.get_translator(request),
                "session": deps.get_session(request),
                "flashes": [],
                "code": exc.status_code,
                "detail": exc.detail,
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        """Log, snapshot and show a plain page - never a stack trace in the browser."""
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        try:
            request.app.state.runtime.crash.write_snapshot(
                "web_unhandled_exception",
                (type(exc), exc, exc.__traceback__),
                extra={"path": request.url.path, "method": request.method},
            )
        except Exception:  # noqa: BLE001
            pass
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "internal error"}, status_code=500)
        return HTMLResponse(
            "<!doctype html><html><head><title>Gateway error</title></head><body>"
            "<h1>Internal error</h1><p>The request could not be completed. "
            "See the Logs page or the crash folder for details.</p>"
            "<p><a href=\"/\">Back to System Status</a></p></body></html>",
            status_code=500,
        )

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    @app.get("/health", response_class=PlainTextResponse, include_in_schema=False)
    async def health() -> str:
        """Unauthenticated liveness probe for service supervisors."""
        return "ok"

    app.include_router(auth_routes.router)
    app.include_router(status_page.router)
    app.include_router(connections.router)
    app.include_router(tags.router)
    app.include_router(devicewise.router)
    app.include_router(tools.router)
    app.include_router(security.router)
    app.include_router(logs.router)
    app.include_router(api.router)

    return app
