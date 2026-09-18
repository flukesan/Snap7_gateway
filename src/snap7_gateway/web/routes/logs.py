"""Logs page: severity-filtered view of the in-memory tail plus file download."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response

from ...core.validation import validate_settings
from .. import deps
from ..templating import render

router = APIRouter(prefix="/logs")

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MAX_LINES = 2000


@router.get("")
async def logs_page(request: Request, level: str = "INFO", lines: int = 300) -> Response:
    runtime = deps.get_runtime(request)
    level = level.upper() if level.upper() in LEVELS else "INFO"
    lines = max(10, min(int(lines or 300), MAX_LINES))
    threshold = logging.getLevelNamesMapping().get(level, logging.INFO)

    entries = runtime.log_buffer.tail(limit=lines, min_level=threshold)
    entries.reverse()  # newest first reads better on a status screen
    return render(
        request,
        "logs.html",
        {
            "entries": entries,
            "levels": LEVELS,
            "level": level,
            "lines": lines,
            "log_file": str(runtime.paths.log_file),
            "current_level": runtime.db.get_setting("log_level", "INFO"),
        },
    )


@router.post("/level", dependencies=[Depends(deps.require_csrf)])
async def set_level(request: Request, user: object = Depends(deps.require_admin)) -> Response:
    """Change the service-wide log level without a restart."""
    runtime = deps.get_runtime(request)
    form = await request.form()
    payload = {"log_level": str(form.get("log_level", "INFO"))}
    clean, errors = validate_settings(payload)
    if errors:
        deps.flash_errors(request, errors.values())
        return deps.redirect("/logs")

    runtime.db.set_settings(clean)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "settings.log_level",
        None,
        clean.get("log_level"),
        deps.client_ip(request),
    )
    await runtime.apply_settings()
    deps.flash(request, "ok", f"Log level set to {clean.get('log_level')}.")
    return deps.redirect("/logs")


@router.get("/download")
async def download_log(request: Request) -> Response:
    """Download the current rotating log file."""
    runtime = deps.get_runtime(request)
    user = deps.require_user(request)
    log_file = runtime.paths.log_file
    if not log_file.exists():
        deps.flash(request, "error", "No log file has been written yet.")
        return deps.redirect("/logs")
    runtime.db.audit(user.username, "logs.download", log_file.name, None, deps.client_ip(request))
    return FileResponse(log_file, media_type="text/plain", filename=log_file.name)
