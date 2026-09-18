"""Device / Output Side page - the virtual S7 CPU DeviceWise connects to."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.validation import validate_settings
from .. import deps
from ..templating import render

router = APIRouter(prefix="/devicewise")

#: Settings this page owns. Checkboxes that are unticked are simply absent from
#: the POST body, so the page has to state which keys it is responsible for.
PAGE_SETTINGS = (
    "vplc_enabled",
    "vplc_bind_ip",
    "vplc_port",
    "vplc_rack",
    "vplc_slot",
    "vplc_sync_interval_ms",
    "vplc_stale_timeout_seconds",
    "vplc_max_clients",
    "discovery_interval_minutes",
    "discovery_max_db_count",
)
BOOL_SETTINGS = ("vplc_enabled",)


@router.get("")
async def devicewise_page(request: Request) -> Response:
    runtime = deps.get_runtime(request)
    connections = {c.id: c for c in runtime.db.list_connections()}
    return render(
        request,
        "devicewise.html",
        {
            "settings": runtime.db.get_settings(),
            "vplc": runtime.vplc.status(),
            "sync": runtime.sync.status(),
            "connections": connections,
        },
    )


@router.post("", dependencies=[Depends(deps.require_csrf)])
async def save_devicewise(
    request: Request, user: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    form = dict(await request.form())
    payload = {key: form.get(key, "") for key in PAGE_SETTINGS}
    for key in BOOL_SETTINGS:
        payload[key] = "true" if key in form else "false"

    clean, errors = validate_settings(payload)
    if errors:
        deps.flash_errors(request, errors.values())
        return render(
            request,
            "devicewise.html",
            {
                "settings": {**runtime.db.get_settings(), **payload},
                "vplc": runtime.vplc.status(),
                "sync": runtime.sync.status(),
                "connections": {c.id: c for c in runtime.db.list_connections()},
                "errors": errors,
            },
            status_code=400,
        )

    before = runtime.db.get_settings()
    changed = {k: v for k, v in clean.items() if before.get(k) != v}
    runtime.db.set_settings(clean)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "settings.devicewise",
        None,
        changed or "no change",
        deps.client_ip(request),
    )
    await runtime.apply_settings()

    if runtime.db.get_bool("vplc_enabled", True) and not runtime.vplc.running:
        deps.flash(
            request,
            "error",
            f"Virtual S7 CPU could not start: {runtime.vplc.last_error or 'unknown error'}",
        )
    else:
        deps.flash(request, "ok", deps.get_translator(request)("msg.saved"))
    return deps.redirect("/devicewise")
