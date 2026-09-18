"""System Status page: uptime, connection health, virtual CPU, crash info."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from .. import deps
from ..templating import render

router = APIRouter()


@router.get("/")
async def status_page(request: Request) -> Response:
    runtime = deps.get_runtime(request)
    status = runtime.status()
    connections = {c.id: c for c in runtime.db.list_connections()}
    return render(
        request,
        "status.html",
        {
            "status": status,
            "connections": connections,
            "area_count": len(runtime.db.list_areas()),
            "bootstrap_password_pending": bool(runtime.bootstrap_password),
        },
    )


@router.get("/crash/{name}")
async def download_crash(request: Request, name: str) -> Response:
    """Download one crash snapshot.

    Only admins, and only files that are actually inside the crash directory -
    the resolved path is compared against it so a traversal attempt cannot reach
    the database or the TLS key.
    """
    deps.require_admin(request)
    runtime = deps.get_runtime(request)
    crash_dir = runtime.paths.crash_dir.resolve()
    candidate = (crash_dir / name).resolve()
    if candidate.parent != crash_dir or not candidate.is_file():
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/")
    runtime.db.audit(
        deps.require_user(request).username, "crash.download", name, None, deps.client_ip(request)
    )
    return FileResponse(candidate, media_type="application/json", filename=candidate.name)
