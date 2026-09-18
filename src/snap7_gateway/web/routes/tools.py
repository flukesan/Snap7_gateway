"""Test Read tool (CLAUDE.md section 4.5).

Reads a live value straight from the PLC without requiring the address to be
saved first - the operator is standing at the machine during commissioning and
needs an answer now. The read is dispatched onto the owning worker's dedicated
thread, so it queues behind the poll cycle instead of racing it over the same
Snap7 handle. When the connection has no running worker (disabled, or not yet
connected), a one-shot client is opened instead.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.validation import validate_area_address
from ...db.models import AreaType
from ...plc.client import PlcClient, PlcConnectionParams, PlcError
from ...plc.decoding import DataType, DecodeError, decode, format_value, size_of
from .. import deps
from ..templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools")


def _page_context(request: Request, **extra) -> dict:
    runtime = deps.get_runtime(request)
    context = {
        "connections": runtime.db.list_connections(),
        "area_types": [t.value for t in AreaType],
        "data_types": [t.value for t in DataType],
        "values": {
            "area_type": AreaType.DB.value,
            "db_number": 1,
            "byte_offset": 0,
            "bit_offset": 0,
            "data_type": DataType.INT.value,
            "length": 1,
        },
        "errors": {},
        "result": None,
    }
    context.update(extra)
    return context


@router.get("")
async def test_read_page(request: Request) -> Response:
    return render(request, "tools.html", _page_context(request))


@router.post("/read", dependencies=[Depends(deps.require_csrf)])
async def test_read(request: Request, user: object = Depends(deps.require_admin)) -> Response:
    runtime = deps.get_runtime(request)
    form = dict(await request.form())

    values, errors = validate_area_address(form)
    data_type = str(form.get("data_type", "")).strip().upper()
    if data_type not in {t.value for t in DataType}:
        errors["data_type"] = "Choose a supported data type"
    try:
        length = max(1, int(str(form.get("length", "1")) or 1))
    except ValueError:
        length = 1
        errors["length"] = "Length must be a whole number"
    try:
        bit_offset = int(str(form.get("bit_offset", "0")) or 0)
        if not 0 <= bit_offset <= 7:
            raise ValueError
    except ValueError:
        bit_offset = 0
        errors["bit_offset"] = "Bit offset must be 0-7"

    try:
        connection_id = int(str(form.get("connection_id", "")))
    except ValueError:
        connection_id = 0
        errors["connection_id"] = "Choose a connection"

    connection = runtime.db.get_connection(connection_id) if connection_id else None
    if connection_id and connection is None:
        errors["connection_id"] = "That connection no longer exists"

    if errors:
        return render(
            request,
            "tools.html",
            _page_context(request, values=form, errors=errors),
            status_code=400,
        )

    assert connection is not None
    try:
        width = size_of(data_type, length)
    except DecodeError as exc:
        return render(
            request,
            "tools.html",
            _page_context(request, values=form, errors={"length": str(exc)}),
            status_code=400,
        )

    area_type = values["area_type"]
    db_number = values.get("db_number", 0)
    offset = values.get("byte_offset", 0)

    worker = runtime.connections.worker(connection.id)
    result: dict[str, object]
    try:
        if worker is not None and worker.running:
            raw = await worker.read_once(area_type, db_number, offset, width)
            source = "live polling connection"
        else:
            # No running worker: open a throwaway client on a thread so a dead
            # PLC blocks only for the configured timeout.
            raw = await asyncio.to_thread(
                _one_shot_read,
                PlcConnectionParams.from_model(connection),
                area_type,
                db_number,
                offset,
                width,
            )
            source = "one-off connection"
        value = decode(raw, data_type, 0, bit_offset, length)
        result = {
            "ok": True,
            "value": format_value(value, data_type),
            "raw": bytes(raw).hex(" "),
            "bytes": len(raw),
            "source": source,
        }
    except (PlcError, DecodeError) as exc:
        detail = exc.detail if isinstance(exc, PlcError) else str(exc)
        result = {"ok": False, "error": detail}
    except Exception as exc:  # noqa: BLE001 - the tool must always answer
        logger.exception("test read failed unexpectedly")
        result = {"ok": False, "error": f"unexpected error: {exc}"}

    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "tools.test_read",
        f"{connection.name}/{area_type}{db_number}.{offset}",
        f"{data_type} -> {result.get('value') if result.get('ok') else result.get('error')}",
        deps.client_ip(request),
    )
    return render(
        request,
        "tools.html",
        _page_context(request, values=form, result=result, selected_connection=connection.id),
    )


def _one_shot_read(
    params: PlcConnectionParams, area_type: str, db_number: int, offset: int, size: int
) -> bytes:
    client = PlcClient(params)
    client.connect()
    try:
        return bytes(client.read_area(area_type, db_number, offset, size))
    finally:
        client.disconnect()
