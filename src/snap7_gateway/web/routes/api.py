"""Small read-only JSON API.

Backs the live-refreshing parts of the UI and gives an integrator a scriptable
way to check gateway health. Read-only by design: every mutation goes through
the CSRF-protected form routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import deps

router = APIRouter(prefix="/api")


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
