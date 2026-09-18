"""Request-scoped helpers: session lookup, access guards, CSRF and flash messages."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Iterable

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..auth.sessions import SESSION_COOKIE_NAME, SessionContext
from ..core.runtime import GatewayRuntime
from ..db.models import Role, User
from .i18n import Translator

logger = logging.getLogger(__name__)

#: Paths reachable without a session.
PUBLIC_PATHS = {"/login", "/health", "/favicon.ico"}

#: Paths reachable while a forced password change is outstanding.
PASSWORD_CHANGE_PATHS = {"/password-change", "/logout"}

FLASH_LIMIT = 200


class FlashStore:
    """Bounded, in-process flash messages keyed by session token hash.

    Kept server-side rather than in a cookie so a message can never be forged
    or replayed by a client, and bounded so it cannot grow on a box that runs
    for months.
    """

    def __init__(self, limit: int = FLASH_LIMIT) -> None:
        self._messages: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
        self.limit = limit

    def add(self, key: str | None, level: str, message: str) -> None:
        if not key:
            return
        bucket = self._messages.setdefault(key, [])
        bucket.append((level, message))
        del bucket[:-10]  # keep at most 10 pending messages per session
        self._messages.move_to_end(key)
        while len(self._messages) > self.limit:
            self._messages.popitem(last=False)

    def consume(self, key: str | None) -> list[tuple[str, str]]:
        if not key:
            return []
        return self._messages.pop(key, [])

    def discard(self, key: str | None) -> None:
        if key:
            self._messages.pop(key, None)


FLASH = FlashStore()


def get_runtime(request: Request) -> GatewayRuntime:
    runtime: GatewayRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - app is always built with a runtime
        raise HTTPException(status_code=500, detail="gateway runtime is not available")
    return runtime


def get_session(request: Request) -> SessionContext | None:
    return getattr(request.state, "session", None)


def get_translator(request: Request) -> Translator:
    translator: Translator | None = getattr(request.state, "translator", None)
    return translator or Translator()


def client_ip(request: Request) -> str:
    """Best-effort client address.

    ``X-Forwarded-For`` is honoured only when the gateway sits behind a reverse
    proxy the operator configured; by default the socket peer wins, because on a
    flat factory VLAN a spoofed header must not be able to defeat rate limiting.
    """
    if request.app.state.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def require_user(request: Request) -> User:
    """Dependency for routes that need any signed-in user."""
    context = get_session(request)
    if context is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    return context.user


def require_admin(request: Request) -> User:
    """Dependency for configuration routes - viewers are read-only."""
    user = require_user(request)
    if user.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_translator(request)("msg.forbidden"),
        )
    return user


async def require_csrf(request: Request) -> None:
    """Validate the per-session CSRF token on a state-changing request.

    Starlette caches the parsed form, so reading it here does not stop the route
    handler from reading it again.
    """
    context = get_session(request)
    submitted: str | None = request.headers.get("x-csrf-token")
    if submitted is None:
        try:
            form = await request.form()
            raw = form.get("csrf_token")
            submitted = str(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 - malformed body is simply a failed check
            submitted = None
    runtime = get_runtime(request)
    if not runtime.auth.sessions.check_csrf(context, submitted):
        logger.warning(
            "CSRF check failed for %s %s from %s",
            request.method,
            request.url.path,
            client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=get_translator(request)("msg.csrf")
        )


def flash(request: Request, level: str, message: str) -> None:
    """Queue a message to render on the next page the operator sees."""
    context = get_session(request)
    FLASH.add(context.token if context else None, level, message)


def flash_errors(request: Request, messages: Iterable[str]) -> None:
    for message in messages:
        flash(request, "error", message)


def take_flashes(request: Request) -> list[dict[str, str]]:
    context = get_session(request)
    return [{"level": level, "message": message} for level, message in FLASH.consume(
        context.token if context else None
    )]


def redirect(url: str, *, status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    """Post/Redirect/Get helper - 303 so the browser re-issues a GET."""
    return RedirectResponse(url=url, status_code=status_code)


def session_cookie_kwargs(request: Request, max_age: int) -> dict[str, Any]:
    """Cookie attributes for the session cookie.

    ``Secure`` follows the actual scheme so the cookie still works when an
    operator deliberately runs the UI over plain HTTP on an isolated bench,
    while HTTPS deployments - the default - always get it.
    """
    return {
        "key": SESSION_COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": request.url.scheme == "https",
        "max_age": max_age,
        "path": "/",
    }


def humanize_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "-"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_timestamp(value: float | None) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def relative_age(value: float | None) -> str:
    if not value:
        return "-"
    return humanize_duration(time.time() - value) + " ago"
