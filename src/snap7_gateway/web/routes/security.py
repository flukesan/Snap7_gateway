"""Users & Security page: accounts, password policy, sessions, audit trail."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.validation import validate_role, validate_settings, validate_username
from ...db.models import Role
from .. import deps
from ..templating import render
from ..tls import certificate_summary

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_SETTINGS = (
    "locale",
    "session_idle_timeout_minutes",
    "session_absolute_timeout_hours",
    "login_max_failures",
    "login_lockout_minutes",
    "login_rate_window_seconds",
    "login_rate_max_attempts",
    "password_min_length",
    "password_require_upper",
    "password_require_lower",
    "password_require_digit",
    "password_require_symbol",
    "password_forbid_username",
    "password_forbid_previous",
    "password_use_blocklist",
    "web_host",
    "web_port",
    "https_enabled",
    "tls_cert_path",
    "tls_key_path",
)
BOOL_SETTINGS = (
    "password_require_upper",
    "password_require_lower",
    "password_require_digit",
    "password_require_symbol",
    "password_forbid_username",
    "password_forbid_previous",
    "password_use_blocklist",
    "https_enabled",
)


def _context(request: Request, **extra) -> dict:
    runtime = deps.get_runtime(request)
    cert_path = runtime.paths.cert_file
    override = runtime.db.get_setting("tls_cert_path", "")
    context = {
        "users": runtime.db.list_users(),
        "settings": runtime.db.get_settings(),
        "policy_rules": runtime.auth.policy.describe(),
        "roles": [r.value for r in Role],
        "certificate": certificate_summary(override or cert_path),
        "errors": {},
    }
    context.update(extra)
    return context


@router.get("/security")
async def security_page(request: Request) -> Response:
    return render(request, "security.html", _context(request))


@router.post("/security", dependencies=[Depends(deps.require_csrf)])
async def save_security(request: Request, user: object = Depends(deps.require_admin)) -> Response:
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
            "security.html",
            _context(request, settings={**runtime.db.get_settings(), **payload}, errors=errors),
            status_code=400,
        )

    before = runtime.db.get_settings()
    changed = {k: v for k, v in clean.items() if before.get(k) != v}
    runtime.db.set_settings(clean)
    runtime.db.audit(
        user.username,  # type: ignore[attr-defined]
        "settings.security",
        None,
        changed or "no change",
        deps.client_ip(request),
    )
    await runtime.apply_settings()

    if any(k in changed for k in ("web_host", "web_port", "https_enabled", "tls_cert_path",
                                  "tls_key_path")):
        deps.flash(
            request,
            "warn",
            "Web server address, port or TLS settings changed - restart the service for "
            "them to take effect.",
        )
    deps.flash(request, "ok", deps.get_translator(request)("msg.saved"))
    return deps.redirect("/security")


@router.post("/users/new", dependencies=[Depends(deps.require_csrf)])
async def create_user(request: Request, actor: object = Depends(deps.require_admin)) -> Response:
    runtime = deps.get_runtime(request)
    form = dict(await request.form())
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    role = str(form.get("role", Role.VIEWER.value)).strip()

    existing = {u.username.lower() for u in runtime.db.list_users()}
    problems = [
        message
        for message in (validate_username(username, existing=existing), validate_role(role))
        if message
    ]
    if problems:
        deps.flash_errors(request, problems)
        return deps.redirect("/security")

    result = runtime.auth.create_user(
        actor, username, password, role, ip=deps.client_ip(request)  # type: ignore[arg-type]
    )
    if not result.ok:
        deps.flash_errors(request, result.errors)
    else:
        deps.flash(
            request,
            "ok",
            f"User '{username}' created with role '{role}'. They must change the password "
            "at first sign-in.",
        )
    return deps.redirect("/security")


@router.post("/users/{user_id}/reset", dependencies=[Depends(deps.require_csrf)])
async def reset_password(
    request: Request, user_id: int, actor: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    target = runtime.db.get_user_by_id(user_id)
    if target is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/security")

    form = await request.form()
    new_password = str(form.get("new_password", ""))
    result = runtime.auth.admin_reset_password(
        actor, target, new_password, ip=deps.client_ip(request)  # type: ignore[arg-type]
    )
    if result.ok:
        deps.flash(
            request,
            "ok",
            f"Password reset for '{target.username}'. All their sessions were signed out and "
            "they must change it at next sign-in.",
        )
    else:
        deps.flash_errors(request, result.errors)
    return deps.redirect("/security")


@router.post("/users/{user_id}/role", dependencies=[Depends(deps.require_csrf)])
async def change_role(
    request: Request, user_id: int, actor: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    target = runtime.db.get_user_by_id(user_id)
    if target is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/security")

    form = await request.form()
    role = str(form.get("role", "")).strip()
    problem = validate_role(role)
    if problem:
        deps.flash(request, "error", problem)
        return deps.redirect("/security")

    if _would_remove_last_admin(runtime, target, new_role=role):
        deps.flash(request, "error", "At least one enabled administrator must remain.")
        return deps.redirect("/security")

    runtime.db.set_user_role(user_id, role)
    runtime.db.audit(
        actor.username,  # type: ignore[attr-defined]
        "user.role",
        target.username,
        f"{target.role} -> {role}",
        deps.client_ip(request),
    )
    deps.flash(request, "ok", f"Role for '{target.username}' changed to '{role}'.")
    return deps.redirect("/security")


@router.post("/users/{user_id}/enabled", dependencies=[Depends(deps.require_csrf)])
async def set_enabled(
    request: Request, user_id: int, actor: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    target = runtime.db.get_user_by_id(user_id)
    if target is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/security")

    form = await request.form()
    enabled = str(form.get("enabled", "")).lower() in {"1", "true", "on", "yes"}
    if not enabled and _would_remove_last_admin(runtime, target, disabling=True):
        deps.flash(request, "error", "At least one enabled administrator must remain.")
        return deps.redirect("/security")

    runtime.db.set_user_enabled(user_id, enabled)
    if not enabled:
        runtime.auth.sessions.destroy_all_for_user(user_id)
    runtime.db.audit(
        actor.username,  # type: ignore[attr-defined]
        "user.enabled",
        target.username,
        f"enabled={enabled}",
        deps.client_ip(request),
    )
    deps.flash(
        request, "ok", f"User '{target.username}' {'enabled' if enabled else 'disabled'}."
    )
    return deps.redirect("/security")


@router.post("/users/{user_id}/delete", dependencies=[Depends(deps.require_csrf)])
async def delete_user(
    request: Request, user_id: int, actor: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    target = runtime.db.get_user_by_id(user_id)
    if target is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/security")
    if target.id == actor.id:  # type: ignore[attr-defined]
        deps.flash(request, "error", "You cannot delete the account you are signed in with.")
        return deps.redirect("/security")
    if _would_remove_last_admin(runtime, target, disabling=True):
        deps.flash(request, "error", "At least one enabled administrator must remain.")
        return deps.redirect("/security")

    runtime.auth.sessions.destroy_all_for_user(user_id)
    runtime.db.delete_user(user_id)
    runtime.db.audit(
        actor.username,  # type: ignore[attr-defined]
        "user.delete",
        target.username,
        None,
        deps.client_ip(request),
    )
    deps.flash(request, "ok", f"User '{target.username}' deleted.")
    return deps.redirect("/security")


@router.post("/users/{user_id}/unlock", dependencies=[Depends(deps.require_csrf)])
async def unlock_user(
    request: Request, user_id: int, actor: object = Depends(deps.require_admin)
) -> Response:
    runtime = deps.get_runtime(request)
    target = runtime.db.get_user_by_id(user_id)
    if target is None:
        deps.flash(request, "error", deps.get_translator(request)("msg.not_found"))
        return deps.redirect("/security")
    runtime.db.unlock_user(user_id)
    runtime.db.audit(
        actor.username,  # type: ignore[attr-defined]
        "user.unlock",
        target.username,
        None,
        deps.client_ip(request),
    )
    deps.flash(request, "ok", f"Lockout cleared for '{target.username}'.")
    return deps.redirect("/security")


@router.get("/audit")
async def audit_page(request: Request, page: int = 1) -> Response:
    runtime = deps.get_runtime(request)
    page = max(1, page)
    per_page = 100
    entries = runtime.db.list_audit(limit=per_page, offset=(page - 1) * per_page)
    return render(
        request,
        "audit.html",
        {"entries": entries, "page": page, "has_next": len(entries) == per_page},
    )


def _would_remove_last_admin(runtime, target, *, new_role: str | None = None,
                             disabling: bool = False) -> bool:
    """Guard against locking every administrator out of the gateway."""
    admins = [
        u for u in runtime.db.list_users() if u.role == Role.ADMIN and u.enabled
    ]
    if target.role != Role.ADMIN or not target.enabled:
        return False
    if new_role is not None and new_role == Role.ADMIN:
        return False
    return len(admins) <= 1
