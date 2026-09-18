"""Tag Mapping: discovered areas, exposure whitelist and named tags."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response

from ...core.datastore import AreaKey
from ...core.tag_import import MAX_IMPORT_TAGS, apply_import, plan_import
from ...core.validation import validate_tag
from ...db.models import AreaStatus, AreaType, ExposureMode
from ...plc.db_layout import DbSourceError, layout_to_symbols, parse_db_export
from ...plc.decoding import DataType, DecodeError, decode, format_value
from ...plc.symbols import SymbolImportError, parse_symbol_export
from .. import deps
from ..templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tags")


def _selected_connection(request: Request, connection_id: int | None):
    runtime = deps.get_runtime(request)
    connections = runtime.db.list_connections()
    if not connections:
        return None, connections
    if connection_id is None:
        return connections[0], connections
    match = next((c for c in connections if c.id == connection_id), None)
    return match or connections[0], connections


@router.get("")
async def tag_mapping(request: Request, connection_id: int | None = None) -> Response:
    runtime = deps.get_runtime(request)
    connection, connections = _selected_connection(request, connection_id)
    areas = runtime.db.list_areas(connection.id) if connection else []
    tags = runtime.db.list_tags(connection.id) if connection else []

    # Live view: current value per tag and registration state per area, so the
    # operator can see at a glance what DeviceWise is actually being served.
    registered = {
        (r.key.area_type, r.key.db_number)
        for r in runtime.vplc.registrations()
        if connection and r.key.connection_id == connection.id
    }
    snapshots = {}
    if connection:
        for area in areas:
            key = AreaKey(connection.id, str(area.area_type), area.db_number)
            snapshots[area.id] = runtime.store.get(key)

    tag_values = []
    for tag in tags:
        key = AreaKey(connection.id, str(tag.area_type), tag.db_number)
        snapshot = runtime.store.get(key)
        value = "-"
        note = ""
        if snapshot is None:
            note = "not polled"
        elif not snapshot.ok:
            note = snapshot.error or "read failed"
        else:
            try:
                raw = decode(
                    snapshot.data, tag.data_type, tag.byte_offset, tag.bit_offset, tag.length
                )
                value = format_value(raw, tag.data_type)
                note = f"{snapshot.age():.1f}s old"
            except DecodeError as exc:
                note = str(exc)
        tag_values.append({"tag": tag, "value": value, "note": note})

    return render(
        request,
        "tags.html",
        {
            "connection": connection,
            "connections": connections,
            "areas": areas,
            "snapshots": snapshots,
            "registered": registered,
            "tag_values": tag_values,
            "data_types": [t.value for t in DataType],
            "area_types": [t.value for t in AreaType],
            "max_import_tags": MAX_IMPORT_TAGS,
            "mirror_all": bool(connection and connection.exposure_mode == ExposureMode.MIRROR_ALL),
        },
    )


@router.post("/rescan", dependencies=[Depends(deps.require_csrf)])
async def rescan(
    request: Request, user: object = Depends(deps.require_admin)
) -> Response:
    """Queue a block rescan (CLAUDE.md section 4.7)."""
    runtime = deps.get_runtime(request)
    form = await request.form()
    raw_id = form.get("connection_id")
    connection_id = int(raw_id) if raw_id and str(raw_id).isdigit() else None

    signalled = runtime.connections.request_rescan(connection_id)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "discovery.rescan_requested",
        str(connection_id) if connection_id else "all connections",
        f"{len(signalled)} worker(s) signalled",
        deps.client_ip(request),
    )
    translator = deps.get_translator(request)
    if signalled:
        deps.flash(request, "ok", translator("tags.rescan_queued", count=len(signalled)))
    else:
        deps.flash(
            request,
            "error",
            "No running connection to rescan. Enable a connection and wait for it to connect.",
        )
    target = f"/tags?connection_id={connection_id}" if connection_id else "/tags"
    return deps.redirect(target)


@router.post("/areas/{area_id}/exposure", dependencies=[Depends(deps.require_csrf)])
async def set_exposure(
    request: Request, area_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    """Whitelist or un-whitelist one area.

    Exposure is enforced at registration time: un-exposing an area makes the
    sync task unregister it, after which DeviceWise cannot even enumerate it.
    """
    runtime = deps.get_runtime(request)
    area = runtime.db.get_area_by_id(area_id)
    if area is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/tags")

    form = await request.form()
    exposed = str(form.get("exposed", "")).lower() in {"1", "true", "on", "yes"}
    runtime.db.set_area_exposed(area_id, exposed)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "area.exposure",
        f"connection {area.connection_id}/{area.key}",
        f"exposed={exposed}",
        deps.client_ip(request),
    )
    runtime.sync.request_refresh()
    deps.flash(
        request,
        "ok",
        f"{area.key} is now {'exposed to' if exposed else 'hidden from'} DeviceWise.",
    )
    return deps.redirect(f"/tags?connection_id={area.connection_id}")


@router.post("/areas/{area_id}/confirm-shape", dependencies=[Depends(deps.require_csrf)])
async def confirm_shape(
    request: Request, area_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    """Accept a changed block size and let the virtual CPU re-register it.

    Until this is confirmed, a shrunk area keeps its previously registered size
    so a live DeviceWise consumer's tag shapes stay valid.
    """
    runtime = deps.get_runtime(request)
    area = runtime.db.get_area_by_id(area_id)
    if area is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/tags")

    runtime.db.set_area_status(area_id, AreaStatus.OK, None)
    key = AreaKey(area.connection_id, str(area.area_type), area.db_number)
    runtime.vplc.unregister(key, reason="operator confirmed the new size")
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "area.shape_confirmed",
        f"connection {area.connection_id}/{area.key}",
        f"size={area.size_bytes}",
        deps.client_ip(request),
    )
    runtime.sync.request_refresh()
    deps.flash(request, "ok", f"{area.key} will be re-registered at {area.size_bytes} bytes.")
    return deps.redirect(f"/tags?connection_id={area.connection_id}")


@router.post("/areas/{area_id}/delete", dependencies=[Depends(deps.require_csrf)])
async def delete_area(
    request: Request, area_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    """Forget an area that is gone from the PLC for good."""
    runtime = deps.get_runtime(request)
    area = runtime.db.get_area_by_id(area_id)
    if area is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/tags")

    key = AreaKey(area.connection_id, str(area.area_type), area.db_number)
    runtime.vplc.unregister(key, reason="area removed by operator")
    runtime.store.drop(key)
    runtime.db.delete_area(area_id)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "area.delete",
        f"connection {area.connection_id}/{area.key}",
        None,
        deps.client_ip(request),
    )
    runtime.sync.request_refresh()
    deps.flash(request, "ok", f"{area.key} removed from the tag map.")
    return deps.redirect(f"/tags?connection_id={area.connection_id}")


@router.post("/new", dependencies=[Depends(deps.require_csrf)])
async def create_tag(request: Request, user: object = Depends(deps.require_admin)) -> Response:
    runtime = deps.get_runtime(request)
    form = dict(await request.form())
    try:
        connection_id = int(str(form.get("connection_id", "")))
    except ValueError:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/tags")

    connection = runtime.db.get_connection(connection_id)
    if connection is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/tags")

    existing = {t.name.lower() for t in runtime.db.list_tags(connection_id)}
    values, errors = validate_tag(form, existing_names=existing)
    if errors:
        deps.flash_errors(request, errors.values())
        return deps.redirect(f"/tags?connection_id={connection_id}")

    tag = runtime.db.create_tag(connection_id, values)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "tag.create",
        f"{connection.name}/{tag.name}",
        f"{tag.address} {tag.data_type}",
        deps.client_ip(request),
    )
    deps.flash(request, "ok", f"Tag '{tag.name}' added at {tag.address}.")
    return deps.redirect(f"/tags?connection_id={connection_id}")


@router.post("/{tag_id}/delete", dependencies=[Depends(deps.require_csrf)])
async def delete_tag(
    request: Request, tag_id: int, user: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    tag = runtime.db.get_tag(tag_id)
    if tag is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/tags")
    runtime.db.delete_tag(tag_id)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "tag.delete",
        tag.name,
        tag.address,
        deps.client_ip(request),
    )
    deps.flash(request, "ok", f"Tag '{tag.name}' deleted.")
    return deps.redirect(f"/tags?connection_id={tag.connection_id}")


#: Extensions the import form accepts, and what each one is read as.
SYMBOL_SUFFIXES = {"sdf", "asc", "csv", "txt", "tsv", "seq", "dif", "xlsx", "xlsm"}
DB_SOURCE_SUFFIXES = {"db", "scl", "awl", "udt", "xml"}
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


@router.post("/import", dependencies=[Depends(deps.require_csrf)])
async def import_symbols(
    request: Request,
    file: UploadFile = File(...),
    user: object = Depends(deps.require_admin),
) -> Response:
    """Import names and comments from a STEP 7 / TIA export.

    Snap7 cannot read a symbol table off the PLC - a classic S7 CPU does not
    store one - so this is how an address stops being a bare number and starts
    being "Motor_Start, line 3 start pushbutton".

    Defaults to a preview: nothing is written unless the operator ticks
    "Apply". That keeps a mis-selected file from rewriting the tag table, and
    the preview report is identical to what applying would do.
    """
    runtime = deps.get_runtime(request)
    translator = deps.get_translator(request)
    form = await request.form()

    try:
        connection_id = int(str(form.get("connection_id", "")))
    except ValueError:
        deps.flash(request, "error", translator("msg.not_found"))
        return deps.redirect("/tags")

    connection = runtime.db.get_connection(connection_id)
    if connection is None:
        deps.flash(request, "error", translator("msg.not_found"))
        return deps.redirect("/tags")

    filename = (file.filename or "").strip()
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if not data:
        deps.flash(request, "error", "The uploaded file is empty.")
        return deps.redirect(f"/tags?connection_id={connection_id}")
    if len(data) > MAX_UPLOAD_BYTES:
        deps.flash(
            request,
            "error",
            f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.",
        )
        return deps.redirect(f"/tags?connection_id={connection_id}")

    kind = str(form.get("kind", "auto")).strip().lower()
    prefix = str(form.get("prefix", "")).strip()[:32]
    overwrite = str(form.get("overwrite", "")).lower() in {"1", "true", "on", "yes"}
    apply_changes = str(form.get("apply", "")).lower() in {"1", "true", "on", "yes"}

    db_number: int | None = None
    raw_db = str(form.get("db_number", "")).strip()
    if raw_db:
        if not raw_db.isdigit() or not 1 <= int(raw_db) <= 65535:
            deps.flash(request, "error", "DB number must be between 1 and 65535.")
            return deps.redirect(f"/tags?connection_id={connection_id}")
        db_number = int(raw_db)

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if kind == "auto":
        kind = "db_source" if suffix in DB_SOURCE_SUFFIXES else "symbols"

    try:
        if kind == "db_source":
            layout = parse_db_export(filename, data)
            symbols = layout_to_symbols(layout, db_number=db_number, prefix=prefix)
        else:
            symbols = parse_symbol_export(filename, data)
            if prefix:
                for entry in symbols.entries:
                    entry.name = f"{prefix}{entry.name}"[:64]
    except (SymbolImportError, DbSourceError) as exc:
        runtime.db.audit(
            user.username,  # type: ignore[attr-defined]
            "tags.import_failed",
            f"{connection.name}/{filename}",
            str(exc),
            deps.client_ip(request),
        )
        deps.flash(request, "error", f"{filename}: {exc}")
        return deps.redirect(f"/tags?connection_id={connection_id}")
    except Exception as exc:  # noqa: BLE001 - a malformed upload must not 500
        logger.exception("unexpected failure parsing %s", filename)
        deps.flash(request, "error", f"{filename} could not be read: {exc}")
        return deps.redirect(f"/tags?connection_id={connection_id}")

    plan = plan_import(runtime.db, connection_id, symbols, overwrite=overwrite)

    if apply_changes and plan.writes and plan.writes <= MAX_IMPORT_TAGS:
        apply_import(
            runtime.db,
            connection,
            plan,
            actor=user.username,  # type: ignore[attr-defined]
            ip=deps.client_ip(request),
        )
    elif apply_changes and not plan.writes:
        deps.flash(request, "warn", "Nothing to import from this file.")

    return render(
        request,
        "tag_import.html",
        {
            "connection": connection,
            "plan": plan,
            "filename": filename,
            "applied": plan.applied,
            "overwrite": overwrite,
            "kind": kind,
            "max_import_tags": MAX_IMPORT_TAGS,
        },
    )
