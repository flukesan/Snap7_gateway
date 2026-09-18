"""Login, logout and the forced password change (CLAUDE.md section 4.4)."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ...auth.sessions import SESSION_COOKIE_NAME
from .. import deps
from ..templating import render

logger = logging.getLogger(__name__)

router = APIRouter()

#: Pre-session CSRF cookie. Before login there is no session to hang a token
#: on, so the login form uses the classic double-submit pattern instead.
LOGIN_CSRF_COOKIE = "s7gw_login_csrf"

#: How long an unsubmitted sign-in page stays valid. This has to cover an
#: operator opening the page, going to find the first-run password in the log or
#: the manual, and coming back - ten minutes was not enough and produced a
#: "could not be verified" error on a perfectly good password.
LOGIN_CSRF_TTL_SECONDS = 3600


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _render_login(
    request: Request,
    *,
    next_url: str,
    error: str | None = None,
    username: str = "",
    status_code: int = 200,
) -> Response:
    """Render the sign-in page and issue the matching CSRF cookie.

    Every path that shows this page goes through here. Rendering a fresh token
    into the form without also setting the cookie leaves the page permanently
    unsubmittable - the token can never match - which is exactly the trap that
    locked operators out of a first sign-in.
    """
    csrf = secrets.token_urlsafe(32)
    response = render(
        request,
        "login.html",
        {
            "next": _safe_next(next_url),
            "login_csrf": csrf,
            "error": error,
            "username": username,
            "hide_nav": True,
        },
        status_code=status_code,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        csrf,
        httponly=True,
        samesite="lax",
        # Secure follows the real scheme so the cookie still works on an
        # isolated bench running plain HTTP, and is always set over HTTPS.
        secure=request.url.scheme == "https",
        max_age=LOGIN_CSRF_TTL_SECONDS,
        path="/",
    )
    return response


@router.get("/login")
async def login_form(request: Request, next: str = "/") -> Response:
    context = deps.get_session(request)
    if context is not None and not context.user.must_change_password:
        return deps.redirect("/")
    return _render_login(request, next_url=next)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form("/"),
) -> Response:
    runtime = deps.get_runtime(request)
    translator = deps.get_translator(request)
    ip = deps.client_ip(request)

    expected = request.cookies.get(LOGIN_CSRF_COOKIE)
    if not expected:
        # The page sat open past the cookie's lifetime, or cookies are blocked.
        # Recoverable: hand back a fresh page with a fresh cookie and say so.
        logger.info("sign-in page expired before submission from %s", ip)
        return _render_login(
            request,
            next_url=next,
            error=translator("login.expired"),
            username=username,
            status_code=400,
        )
    if not secrets.compare_digest(expected, csrf_token or ""):
        logger.warning("login CSRF check failed from %s", ip)
        return _render_login(
            request, next_url=next, error=translator("msg.csrf"), status_code=400
        )

    result = runtime.auth.login(
        username,
        password,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    if not result.ok or result.token is None:
        return _render_login(
            request,
            next_url=next,
            error=result.error,
            username=username,
            status_code=401,
        )

    destination = "/password-change" if result.must_change_password else _safe_next(next)
    response = deps.redirect(destination)
    response.set_cookie(
        value=result.token,
        **deps.session_cookie_kwargs(request, runtime.auth.sessions.idle_timeout_seconds),
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")) -> Response:
    runtime = deps.get_runtime(request)
    context = deps.get_session(request)
    # A forged logout is only a nuisance, but the check is free and keeps the
    # rule "every state-changing POST is CSRF-checked" without exceptions.
    if context is not None and runtime.auth.sessions.check_csrf(context, csrf_token):
        deps.FLASH.discard(context.token)
        runtime.auth.logout(
            context.token, username=context.user.username, ip=deps.client_ip(request)
        )
    response: RedirectResponse = deps.redirect("/login")
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/password-change")
async def password_change_form(request: Request) -> Response:
    runtime = deps.get_runtime(request)
    context = deps.get_session(request)
    if context is None:
        return deps.redirect("/login")
    return render(
        request,
        "password_change.html",
        {
            "forced": context.user.must_change_password,
            "rules": runtime.auth.policy.describe(),
            "hide_nav": context.user.must_change_password,
        },
    )


@router.post("/password-change")
async def password_change_submit(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
) -> Response:
    runtime = deps.get_runtime(request)
    context = deps.get_session(request)
    if context is None:
        return deps.redirect("/login")
    if not runtime.auth.sessions.check_csrf(context, csrf_token):
        return render(
            request,
            "password_change.html",
            {
                "forced": context.user.must_change_password,
                "rules": runtime.auth.policy.describe(),
                "errors": [deps.get_translator(request)("msg.csrf")],
                "hide_nav": context.user.must_change_password,
            },
            status_code=403,
        )

    result = runtime.auth.change_password(
        context.user,
        current_password,
        new_password,
        confirm_password,
        ip=deps.client_ip(request),
    )
    if not result.ok:
        return render(
            request,
            "password_change.html",
            {
                "forced": context.user.must_change_password,
                "rules": runtime.auth.policy.describe(),
                "errors": result.errors,
                "hide_nav": context.user.must_change_password,
            },
            status_code=400,
        )

    # Every session was invalidated, including this one: the operator signs in
    # again with the new password.
    deps.FLASH.discard(context.token)
    response = deps.redirect("/login?changed=1")
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
