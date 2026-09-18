"""Shared helper for rendering a page with the standard template context."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from . import deps


def render(request: Request, template: str, context: dict[str, Any] | None = None,
           *, status_code: int = 200) -> HTMLResponse:
    """Render ``template`` with the context every page needs.

    Supplies the translator, the current session, pending flash messages and the
    CSRF token, so no route has to remember them.
    """
    session = deps.get_session(request)
    payload: dict[str, Any] = {
        "t": deps.get_translator(request),
        "session": session,
        "user": session.user if session else None,
        "csrf_token": session.csrf_token if session else "",
        "flashes": deps.take_flashes(request),
        "runtime": deps.get_runtime(request),
        "current_path": request.url.path,
    }
    payload.update(context or {})
    return request.app.state.templates.TemplateResponse(
        request, template, payload, status_code=status_code
    )
