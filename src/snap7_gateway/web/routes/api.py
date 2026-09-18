"""Small read-only JSON API.

Backs the live-refreshing parts of the UI and gives an integrator a scriptable
way to check gateway health. Read-only by design: every mutation goes through
the CSRF-protected form routes.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import deps

router = APIRouter(prefix="/api")


@router.post("/keepalive", dependencies=[Depends(deps.require_csrf)])
async def keepalive(request: Request) -> JSONResponse:
    """Extend the session and report how long it has left.

    Called by the browser only after real user activity - every authenticated
    request slides the idle expiry forward, so a page that polled on a timer
    would keep a session alive next to an empty chair, which is the exact
    opposite of what an idle timeout is for.

    The response lets the page re-sync its countdown with the server rather than
    trusting a timer that a background tab may have throttled.
    """
    deps.require_user(request)
    context = deps.get_session(request)
    assert context is not None
    return JSONResponse(
        {
            "expires_in": max(0, int(context.session.expires_at - time.time())),
            "idle_timeout_seconds": deps.get_runtime(request).auth.sessions.idle_timeout_seconds,
        }
    )


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    deps.require_user(request)
    return JSONResponse(deps.get_runtime(request).status())


@router.get("/connections")
async def connections(request: Request) -> JSONResponse:
    deps.require_user(request)
    runtime = deps.get_runtime(request)
    configured = {c.id: c for c in runtime.db.list_connections()}
    payload = []
    for health in runtime.store.all_health():
        connection = configured.get(health.connection_id)
        entry = health.as_dict()
        if connection is not None:
            entry.update(
                {
                    "host": connection.host,
                    "rack": connection.rack,
                    "slot": connection.slot,
                    "exposure_mode": connection.exposure_mode,
                    "poll_interval_ms": connection.poll_interval_ms,
                }
            )
        payload.append(entry)
    return JSONResponse({"connections": payload})


@router.get("/areas")
async def areas(request: Request, connection_id: int | None = None) -> JSONResponse:
    deps.require_user(request)
    runtime = deps.get_runtime(request)
    rows = runtime.db.list_areas(connection_id)
    registered = {
        (r.key.connection_id, r.key.area_type, r.key.db_number): r for r in runtime.vplc.registrations()
    }
    return JSONResponse(
        {
            "areas": [
                {
                    "id": area.id,
                    "connection_id": area.connection_id,
                    "area": area.key,
                    "size_bytes": area.size_bytes,
                    "exposed": area.exposed,
                    "status": area.status,
                    "status_detail": area.status_detail,
                    "registered": (area.connection_id, str(area.area_type), area.db_number)
                    in registered,
                }
                for area in rows
            ]
        }
    )
