"""PLC Connections: list, create, edit, delete and per-connection test."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.validation import validate_connection
from ...db.models import ConnectionType, ExposureMode
from ...plc.client import PlcClient, PlcConnectionParams
from .. import deps
from ..templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections")

_DEFAULTS = {
    "name": "",
    "host": "",
    "rack": 0,
    "slot": 2,
    "tcp_port": 102,
    "connection_type": ConnectionType.PG.value,
    "timeout_ms": 3000,
    "poll_interval_ms": 1000,
    "exposure_mode": ExposureMode.WHITELIST.value,
    "write_enabled": False,
    "enabled": True,
    "read_inputs": True,
    "read_outputs": True,
    "read_merkers": True,
    "max_input_bytes": 1024,
    "max_output_bytes": 1024,
    "max_merker_bytes": 8192,
}


def _form_context(request: Request, values: dict, errors: dict, *, connection=None) -> dict:
    return {
        "values": values,
        "errors": errors,
        "connection": connection,
        "connection_types": [t.value for t in ConnectionType],
        "exposure_modes": [m.value for m in ExposureMode],
    }


@router.get("")
async def list_connections(request: Request) -> Response:
    runtime = deps.get_runtime(request)
    connections = runtime.db.list_connections()
    health = {h.connection_id: h for h in runtime.store.all_health()}
    return render(
        request,
        "connections.html",
        {"connections": connections, "health": health},
    )


@router.get("/new")
async def new_connection(request: Request, _: object = Depends(deps.require_admin)) -> Response:
    return render(request, "connection_form.html", _form_context(request, dict(_DEFAULTS), {}))


@router.post("/new", dependencies=[Depends(deps.require_csrf)])
async def create_connection(
    request: Request, user: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    form = dict(await request.form())
    existing = {c.name.lower() for c in runtime.db.list_connections()}
    values, errors = validate_connection(form, existing_names=existing)
    if errors:
        deps.flash(request, "error", deps.get_translator(request)("msg.invalid"))
        return render(
            request, "connection_form.html", _form_context(request, form, errors), status_code=400
        )

    connection = runtime.db.create_connection(values)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "connection.create",
        connection.name,
        {"host": connection.host, "rack": connection.rack, "slot": connection.slot},
        deps.client_ip(request),
    )
    await runtime.connections.apply_config()
    deps.flash(request, "ok", deps.get_translator(request)("conn.created"))
    return deps.redirect("/connections")


@router.get("/{connection_id}/edit")
async def edit_connection(
    request: Request, connection_id: int, _: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    connection = runtime.db.get_connection(connection_id)
    if connection is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/connections")
    values = {key: getattr(connection, key) for key in _DEFAULTS}
    return render(
        request, "connection_form.html", _form_context(request, values, {}, connection=connection)
    )


@router.post("/{connection_id}/edit", dependencies=[Depends(deps.require_csrf)])
async def update_connection(
    request: Request, connection_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    connection = runtime.db.get_connection(connection_id)
    if connection is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/connections")

    form = dict(await request.form())
    existing = {
        c.name.lower() for c in runtime.db.list_connections() if c.id != connection_id
    }
    values, errors = validate_connection(form, existing_names=existing)
    if errors:
        deps.flash(request, "error", deps.get_translator(request)("msg.invalid"))
        return render(
            request,
            "connection_form.html",
            _form_context(request, form, errors, connection=connection),
            status_code=400,
        )

    changed = {
        key: value
        for key, value in values.items()
        if getattr(connection, key, None) != value
    }
    runtime.db.update_connection(connection_id, values)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "connection.update",
        connection.name,
        changed or "no field changed",
        deps.client_ip(request),
    )
    # Hot reload: only the affected worker restarts, others keep polling.
    await runtime.connections.apply_config()
    runtime.sync.request_refresh()
    deps.flash(request, "ok", deps.get_translator(request)("conn.updated"))
    return deps.redirect("/connections")


@router.post("/{connection_id}/delete", dependencies=[Depends(deps.require_csrf)])
async def delete_connection(
    request: Request, connection_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    connection = runtime.db.get_connection(connection_id)
    if connection is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/connections")

    runtime.db.delete_connection(connection_id)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "connection.delete",
        connection.name,
        None,
        deps.client_ip(request),
    )
    await runtime.connections.apply_config()
    runtime.store.drop_connection(connection_id)
    runtime.sync.request_refresh()
    deps.flash(request, "ok", deps.get_translator(request)("conn.deleted"))
    return deps.redirect("/connections")


@router.post("/{connection_id}/test", dependencies=[Depends(deps.require_csrf)])
async def test_connection(
    request: Request, connection_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    """Connect + disconnect, reporting latency and the real Snap7 error text."""
    import asyncio

    runtime = deps.get_runtime(request)
    connection = runtime.db.get_connection(connection_id)
    if connection is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/connections")

    params = PlcConnectionParams.from_model(connection)
    # Runs off the event loop: a dead PLC blocks for the configured timeout.
    result = await asyncio.to_thread(PlcClient.test_connection, params)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "connection.test",
        connection.name,
        f"ok={result.ok} latency={result.latency_ms:.0f}ms {result.message}",
        deps.client_ip(request),
    )
    if result.ok:
        cpu = result.cpu.module_type if result.cpu else ""
        extra = f", {result.details.get('db_count')} DB(s) visible" if result.details else ""
        deps.flash(
            request,
            "ok",
            f"{connection.name}: connected in {result.latency_ms:.0f} ms"
            + (f" - {cpu}" if cpu else "")
            + extra,
        )
    else:
        deps.flash(request, "error", f"{connection.name}: {result.message}")
    return deps.redirect("/connections")
