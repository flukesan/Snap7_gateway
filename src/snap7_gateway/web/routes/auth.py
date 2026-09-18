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


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login")
async def login_form(request: Request, next: str = "/") -> Response:
    context = deps.get_session(request)
    if context is not None and not context.user.must_change_password:
        return deps.redirect("/")

    csrf = secrets.token_urlsafe(32)
    response = render(
        request,
        "login.html",
        {"next": _safe_next(next), "login_csrf": csrf, "hide_nav": True},
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        csrf,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=600,
        path="/",
    )
    return response


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
    if not expected or not secrets.compare_digest(expected, csrf_token or ""):
        logger.warning("login CSRF check failed from %s", ip)
        return render(
            request,
            "login.html",
            {
                "next": _safe_next(next),
                "login_csrf": secrets.token_urlsafe(32),
                "error": translator("msg.csrf"),
                "hide_nav": True,
            },
            status_code=400,
        )

    result = runtime.auth.login(
        username,
        password,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    if not result.ok or result.token is None:
        csrf = secrets.token_urlsafe(32)
        response = render(
            request,
            "login.html",
            {
                "next": _safe_next(next),
                "login_csrf": csrf,
                "error": result.error,
                "username": username,
                "hide_nav": True,
            },
            status_code=401,
        )
        response.set_cookie(
            LOGIN_CSRF_COOKIE, csrf, httponly=True, samesite="lax",
            secure=request.url.scheme == "https", max_age=600, path="/",
        )
        return response

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
