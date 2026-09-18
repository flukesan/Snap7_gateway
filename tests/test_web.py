"""Web layer: access control, CSRF, the forced first-login change, and the pages.

Driven through the real ASGI app with httpx, so routing, middleware, dependency
guards and template rendering are all exercised.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager

import httpx

from snap7_gateway.core.runtime import GatewayRuntime
from snap7_gateway.db.models import Role
from snap7_gateway.web.app import create_app

from .conftest import free_port

STRONG_PASSWORD = "Bt7#vRq2Lm9xKp"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_of(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "no CSRF token in the rendered page"
    return match.group(1)


@asynccontextmanager
async def gateway(tmp_path):
    """A started gateway plus an httpx client bound to its ASGI app."""
    runtime = GatewayRuntime(tmp_path / "data", console_logging=False)
    runtime.db.set_settings({"vplc_port": str(free_port()), "vplc_bind_ip": "127.0.0.1"})
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        async with app.router.lifespan_context(app):
            yield client, runtime


async def sign_in(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    page = await client.get("/login")
    return await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_of(page.text),
            "next": "/",
        },
    )


async def first_run_login(client: httpx.AsyncClient, runtime: GatewayRuntime) -> None:
    """Complete the first-run flow: sign in, change the password, sign in again."""
    initial = runtime.bootstrap_password
    assert initial, "the first run must generate an admin password"
    await sign_in(client, "admin", initial)
    page = await client.get("/password-change")
    await client.post(
        "/password-change",
        data={
            "current_password": initial,
            "new_password": STRONG_PASSWORD,
            "confirm_password": STRONG_PASSWORD,
            "csrf_token": csrf_of(page.text),
        },
    )
    await sign_in(client, "admin", STRONG_PASSWORD)


def run(coro_factory, tmp_path):
    async def main():
        async with gateway(tmp_path) as (client, runtime):
            await coro_factory(client, runtime)

    asyncio.run(main())


class TestAccessControl:
    def test_pages_require_a_session(self, tmp_path) -> None:
        async def scenario(client, runtime):
            for path in ("/", "/connections", "/tags", "/devicewise", "/security", "/logs"):
                response = await client.get(path)
                assert response.status_code == 303
                assert response.headers["location"].startswith("/login")

        run(scenario, tmp_path)

    def test_api_returns_401_rather_than_a_redirect(self, tmp_path) -> None:
        async def scenario(client, runtime):
            response = await client.get("/api/status")
            assert response.status_code == 401

        run(scenario, tmp_path)

    def test_health_is_public(self, tmp_path) -> None:
        async def scenario(client, runtime):
            response = await client.get("/health")
            assert response.status_code == 200 and response.text == "ok"

        run(scenario, tmp_path)

    def test_security_headers_are_set(self, tmp_path) -> None:
        async def scenario(client, runtime):
            response = await client.get("/login")
            assert "default-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-store"

        run(scenario, tmp_path)

    def test_session_cookie_is_httponly(self, tmp_path) -> None:
        async def scenario(client, runtime):
            page = await client.get("/login")
            response = await client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": runtime.bootstrap_password,
                    "csrf_token": csrf_of(page.text),
                    "next": "/",
                },
            )
            cookie = response.headers.get("set-cookie", "")
            assert "s7gw_session=" in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=lax" in cookie.replace("samesite", "SameSite")

        run(scenario, tmp_path)


class TestFirstRunFlow:
    def test_login_without_a_csrf_token_is_rejected(self, tmp_path) -> None:
        async def scenario(client, runtime):
            response = await client.post(
                "/login",
                data={"username": "admin", "password": runtime.bootstrap_password, "next": "/"},
            )
            assert response.status_code == 400

        run(scenario, tmp_path)

    def test_every_page_is_blocked_until_the_password_is_changed(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await sign_in(client, "admin", runtime.bootstrap_password)
            for path in ("/", "/connections", "/tags", "/devicewise", "/security", "/logs",
                         "/tools", "/audit", "/api/status"):
                response = await client.get(path)
                assert response.status_code == 303, path
                assert response.headers["location"] == "/password-change", path

        run(scenario, tmp_path)

    def test_a_weak_new_password_is_rejected_with_a_reason(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await sign_in(client, "admin", runtime.bootstrap_password)
            page = await client.get("/password-change")
            response = await client.post(
                "/password-change",
                data={
                    "current_password": runtime.bootstrap_password,
                    "new_password": "Password1234",
                    "confirm_password": "Password1234",
                    "csrf_token": csrf_of(page.text),
                },
            )
            assert response.status_code == 400
            assert "blocked-password list" in response.text
            assert runtime.db.get_user("admin").must_change_password is True

        run(scenario, tmp_path)

    def test_changing_the_password_unlocks_the_ui_after_signing_in_again(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            response = await client.get("/")
            assert response.status_code == 200
            assert "System Status" in response.text
            assert runtime.db.get_user("admin").must_change_password is False

        run(scenario, tmp_path)

    def test_the_change_signs_every_session_out(self, tmp_path) -> None:
        async def scenario(client, runtime):
            initial = runtime.bootstrap_password
            await sign_in(client, "admin", initial)
            page = await client.get("/password-change")
            response = await client.post(
                "/password-change",
                data={
                    "current_password": initial,
                    "new_password": STRONG_PASSWORD,
                    "confirm_password": STRONG_PASSWORD,
                    "csrf_token": csrf_of(page.text),
                },
            )
            assert response.headers["location"] == "/login?changed=1"
            assert (await client.get("/")).headers["location"].startswith("/login")

        run(scenario, tmp_path)


class TestCsrf:
    def test_a_post_without_a_token_is_refused(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            response = await client.post(
                "/connections/new", data={"name": "X", "host": "10.0.0.1"}
            )
            assert response.status_code == 303  # redirected by the 403 handler
            assert runtime.db.list_connections() == []

        run(scenario, tmp_path)

    def test_a_forged_token_is_refused(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            response = await client.post(
                "/connections/new",
                data={"name": "X", "host": "10.0.0.1", "csrf_token": "forged"},
            )
            assert response.status_code == 303
            assert runtime.db.list_connections() == []

        run(scenario, tmp_path)


class TestConnectionsPage:
    def test_create_edit_and_delete(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/connections/new")
            token = csrf_of(page.text)

            response = await client.post(
                "/connections/new",
                data={
                    "name": "Line 3",
                    "host": "10.20.30.40",
                    "rack": "0",
                    "slot": "2",
                    "tcp_port": "102",
                    "connection_type": "PG",
                    "timeout_ms": "3000",
                    "poll_interval_ms": "1000",
                    "exposure_mode": "whitelist",
                    "enabled": "1",
                    "csrf_token": token,
                },
            )
            assert response.status_code == 303
            created = runtime.db.get_connection_by_name("Line 3")
            assert created is not None and created.write_enabled is False

            response = await client.post(
                f"/connections/{created.id}/edit",
                data={
                    "name": "Line 3",
                    "host": "10.20.30.41",
                    "rack": "0",
                    "slot": "3",
                    "tcp_port": "102",
                    "connection_type": "OP",
                    "timeout_ms": "5000",
                    "poll_interval_ms": "500",
                    "exposure_mode": "mirror_all",
                    "enabled": "1",
                    "csrf_token": token,
                },
            )
            assert response.status_code == 303
            updated = runtime.db.get_connection(created.id)
            assert updated.host == "10.20.30.41" and updated.exposure_mode == "mirror_all"

            response = await client.post(
                f"/connections/{created.id}/delete", data={"csrf_token": token}
            )
            assert response.status_code == 303
            assert runtime.db.get_connection(created.id) is None

        run(scenario, tmp_path)

    def test_invalid_input_is_rejected_server_side(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/connections/new")
            response = await client.post(
                "/connections/new",
                data={
                    "name": "Bad;Name",
                    "host": "not a host!",
                    "rack": "99",
                    "tcp_port": "70000",
                    "csrf_token": csrf_of(page.text),
                },
            )
            assert response.status_code == 400
            assert runtime.db.list_connections() == []
            assert "must be between 0 and 7" in response.text

        run(scenario, tmp_path)

    def test_a_dead_plc_test_reports_the_reason_without_raising(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/connections/new")
            token = csrf_of(page.text)
            await client.post(
                "/connections/new",
                data={
                    "name": "Dead",
                    "host": "127.0.0.1",
                    "tcp_port": str(free_port()),
                    "timeout_ms": "500",
                    "poll_interval_ms": "1000",
                    "rack": "0",
                    "slot": "2",
                    "connection_type": "PG",
                    "exposure_mode": "whitelist",
                    "csrf_token": token,
                },
            )
            connection = runtime.db.get_connection_by_name("Dead")
            response = await client.post(
                f"/connections/{connection.id}/test", data={"csrf_token": token}
            )
            assert response.status_code == 303
            page = await client.get("/connections")
            assert "Dead:" in page.text  # the flash carries the real error text

        run(scenario, tmp_path)


class TestRoles:
    def test_a_viewer_cannot_change_configuration(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/security")
            token = csrf_of(page.text)
            await client.post(
                "/users/new",
                data={
                    "username": "viewer1",
                    "password": "Zr4$kQp8Nv2wLd",
                    "role": Role.VIEWER.value,
                    "csrf_token": token,
                },
            )
            assert runtime.db.get_user("viewer1") is not None

            # Sign in as the viewer (its own forced password change first).
            viewer = httpx.AsyncClient(transport=client._transport, base_url="http://gw")
            try:
                await sign_in(viewer, "viewer1", "Zr4$kQp8Nv2wLd")
                page = await viewer.get("/password-change")
                await viewer.post(
                    "/password-change",
                    data={
                        "current_password": "Zr4$kQp8Nv2wLd",
                        "new_password": "Hx9!mTz3Qb6vRn",
                        "confirm_password": "Hx9!mTz3Qb6vRn",
                        "csrf_token": csrf_of(page.text),
                    },
                )
                await sign_in(viewer, "viewer1", "Hx9!mTz3Qb6vRn")

                # Read-only pages work.
                assert (await viewer.get("/")).status_code == 200
                assert (await viewer.get("/connections")).status_code == 200

                # Configuration is refused.
                page = await viewer.get("/connections")
                response = await viewer.post(
                    "/connections/new",
                    data={
                        "name": "Sneaky",
                        "host": "10.0.0.1",
                        "csrf_token": csrf_of(page.text),
                    },
                )
                assert response.status_code == 303
                assert runtime.db.get_connection_by_name("Sneaky") is None
                assert (await viewer.get("/connections/new")).status_code == 303
            finally:
                await viewer.aclose()

        run(scenario, tmp_path)

    def test_the_last_admin_cannot_be_demoted(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/security")
            admin = runtime.db.get_user("admin")
            await client.post(
                f"/users/{admin.id}/role",
                data={"role": Role.VIEWER.value, "csrf_token": csrf_of(page.text)},
            )
            assert runtime.db.get_user("admin").role == Role.ADMIN

        run(scenario, tmp_path)


class TestLockout:
    def test_repeated_failures_lock_the_account(self, tmp_path) -> None:
        async def scenario(client, runtime):
            runtime.db.set_settings(
                {"login_max_failures": "3", "login_rate_max_attempts": "50"}
            )
            runtime.auth.reload_limits()
            for _ in range(4):
                page = await client.get("/login")
                response = await client.post(
                    "/login",
                    data={
                        "username": "admin",
                        "password": "wrong",
                        "csrf_token": csrf_of(page.text),
                        "next": "/",
                    },
                )
            assert response.status_code == 401
            assert "locked" in response.text.lower()

        run(scenario, tmp_path)


class TestOtherPages:
    def test_test_read_tool_reads_a_live_value(self, tmp_path, fake_plc) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/connections/new")
            token = csrf_of(page.text)
            await client.post(
                "/connections/new",
                data={
                    "name": "Sim",
                    "host": "127.0.0.1",
                    "tcp_port": str(fake_plc.port),
                    "rack": "0",
                    "slot": "2",
                    "connection_type": "PG",
                    "timeout_ms": "2000",
                    "poll_interval_ms": "1000",
                    "exposure_mode": "whitelist",
                    "csrf_token": token,
                },
            )
            connection = runtime.db.get_connection_by_name("Sim")
            fake_plc.set_db(1, bytes([0x00, 0x2A]) + bytes(62))

            response = await client.post(
                "/tools/read",
                data={
                    "connection_id": str(connection.id),
                    "area_type": "DB",
                    "db_number": "1",
                    "byte_offset": "0",
                    "bit_offset": "0",
                    "data_type": "INT",
                    "length": "1",
                    "csrf_token": token,
                },
            )
            assert response.status_code == 200
            assert ">42<" in response.text or "42" in response.text
            assert "00 2a" in response.text.lower()

        run(scenario, tmp_path)

    def test_test_read_reports_errors_instead_of_crashing(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/connections/new")
            token = csrf_of(page.text)
            await client.post(
                "/connections/new",
                data={
                    "name": "Dead",
                    "host": "127.0.0.1",
                    "tcp_port": str(free_port()),
                    "rack": "0",
                    "slot": "2",
                    "connection_type": "PG",
                    "timeout_ms": "500",
                    "poll_interval_ms": "1000",
                    "exposure_mode": "whitelist",
                    "enabled": "",
                    "csrf_token": token,
                },
            )
            connection = runtime.db.get_connection_by_name("Dead")
            response = await client.post(
                "/tools/read",
                data={
                    "connection_id": str(connection.id),
                    "area_type": "DB",
                    "db_number": "1",
                    "byte_offset": "0",
                    "bit_offset": "0",
                    "data_type": "INT",
                    "length": "1",
                    "csrf_token": token,
                },
            )
            assert response.status_code == 200
            assert "connecting to 127.0.0.1" in response.text

        run(scenario, tmp_path)

    def test_audit_trail_records_configuration_changes(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/connections/new")
            await client.post(
                "/connections/new",
                data={
                    "name": "Audited",
                    "host": "10.0.0.1",
                    "rack": "0",
                    "slot": "2",
                    "tcp_port": "102",
                    "connection_type": "PG",
                    "timeout_ms": "3000",
                    "poll_interval_ms": "1000",
                    "exposure_mode": "whitelist",
                    "csrf_token": csrf_of(page.text),
                },
            )
            response = await client.get("/audit")
            assert "connection.create" in response.text
            assert "Audited" in response.text

        run(scenario, tmp_path)

    def test_logs_page_filters_by_severity(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            import logging

            logging.getLogger("test.marker").error("MARKER-ERROR-LINE")
            logging.getLogger("test.marker").debug("MARKER-DEBUG-LINE")

            response = await client.get("/logs?level=ERROR")
            assert "MARKER-ERROR-LINE" in response.text
            assert "MARKER-DEBUG-LINE" not in response.text

        run(scenario, tmp_path)

    def test_crash_download_rejects_path_traversal(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            response = await client.get("/crash/..%2F..%2Fgateway.db")
            assert response.status_code in (303, 404)
            assert b"SQLite" not in response.content

        run(scenario, tmp_path)

    def test_settings_changes_apply_without_a_restart(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            page = await client.get("/devicewise")
            response = await client.post(
                "/devicewise",
                data={
                    "vplc_enabled": "1",
                    "vplc_bind_ip": "127.0.0.1",
                    "vplc_port": runtime.db.get_setting("vplc_port"),
                    "vplc_rack": "0",
                    "vplc_slot": "2",
                    "vplc_sync_interval_ms": "500",
                    "vplc_stale_timeout_seconds": "42",
                    "vplc_max_clients": "8",
                    "discovery_interval_minutes": "15",
                    "discovery_max_db_count": "100",
                    "csrf_token": csrf_of(page.text),
                },
            )
            assert response.status_code == 303
            assert runtime.sync.stale_timeout_seconds == 42
            assert runtime.sync.interval_ms == 500
            assert runtime.db.get_int("discovery_interval_minutes") == 15

        run(scenario, tmp_path)

    def test_thai_locale_renders(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            runtime.db.set_setting("locale", "th")
            response = await client.get("/")
            assert "สถานะระบบ" in response.text

        run(scenario, tmp_path)


class TestSymbolImport:
    """Uploading a STEP 7 / TIA export through the Tag Mapping page."""

    SDF = (
        '"Motor_Start","E      0.0","BOOL","Start pushbutton"\n'
        '"Speed_SP","MW    20","INT","Speed setpoint"\n'
        '"Cycle_Timer","T      5","TIMER","Cycle time"\n'
    )

    async def _connection(self, client, runtime, token):
        await client.post(
            "/connections/new",
            data={
                "name": "Line3",
                "host": "10.0.0.5",
                "rack": "0",
                "slot": "2",
                "tcp_port": "102",
                "connection_type": "PG",
                "timeout_ms": "3000",
                "poll_interval_ms": "1000",
                "exposure_mode": "whitelist",
                "csrf_token": token,
            },
        )
        return runtime.db.get_connection_by_name("Line3")

    def test_preview_is_the_default_and_writes_nothing(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/connections/new")).text)
            connection = await self._connection(client, runtime, token)

            response = await client.post(
                "/tags/import",
                data={"connection_id": str(connection.id), "csrf_token": token},
                files={"file": ("line3.sdf", self.SDF.encode(), "text/plain")},
            )
            assert response.status_code == 200
            assert "Import preview" in response.text
            assert "Nothing has been saved" in response.text
            assert "Motor_Start" in response.text
            assert runtime.db.list_tags(connection.id) == []

        run(scenario, tmp_path)

    def test_applying_creates_the_tags(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/connections/new")).text)
            connection = await self._connection(client, runtime, token)

            response = await client.post(
                "/tags/import",
                data={
                    "connection_id": str(connection.id),
                    "apply": "1",
                    "csrf_token": token,
                },
                files={"file": ("line3.sdf", self.SDF.encode(), "text/plain")},
            )
            assert response.status_code == 200
            assert "Symbols imported" in response.text
            tags = {t.name: t for t in runtime.db.list_tags(connection.id)}
            assert set(tags) == {"Motor_Start", "Speed_SP"}
            assert tags["Speed_SP"].address == "MW20"
            assert tags["Motor_Start"].description == "Start pushbutton"

        run(scenario, tmp_path)

    def test_unusable_rows_are_reported_with_a_reason(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/connections/new")).text)
            connection = await self._connection(client, runtime, token)

            response = await client.post(
                "/tags/import",
                data={"connection_id": str(connection.id), "csrf_token": token},
                files={"file": ("line3.sdf", self.SDF.encode(), "text/plain")},
            )
            assert "Cycle_Timer" in response.text
            assert "timers are not mirrored" in response.text

        run(scenario, tmp_path)

    def test_a_data_block_source_is_imported_with_computed_offsets(self, tmp_path) -> None:
        source = (
            'DATA_BLOCK "Recipes"\n'
            "{ S7_Optimized_Access := 'FALSE' }\n"
            "   STRUCT\n"
            "      Recipe_No : Int;   // active recipe\n"
            "      Running : Bool;   // line running\n"
            "   END_STRUCT;\n"
            "BEGIN\n"
            "END_DATA_BLOCK\n"
        )

        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/connections/new")).text)
            connection = await self._connection(client, runtime, token)

            response = await client.post(
                "/tags/import",
                data={
                    "connection_id": str(connection.id),
                    "kind": "db_source",
                    "db_number": "12",
                    "apply": "1",
                    "csrf_token": token,
                },
                files={"file": ("recipes.db", source.encode(), "text/plain")},
            )
            assert response.status_code == 200
            tags = {t.name: t for t in runtime.db.list_tags(connection.id)}
            assert tags["Recipe_No"].address == "DB12.DBW0"
            assert tags["Running"].address == "DB12.DBX2.0"
            assert tags["Recipe_No"].description == "active recipe"

        run(scenario, tmp_path)

    def test_an_optimized_block_is_refused_with_advice(self, tmp_path) -> None:
        source = (
            'DATA_BLOCK "Recipes"\n'
            "{ S7_Optimized_Access := 'TRUE' }\n"
            "   STRUCT\n      Recipe_No : Int;\n   END_STRUCT;\nBEGIN\nEND_DATA_BLOCK\n"
        )

        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/connections/new")).text)
            connection = await self._connection(client, runtime, token)

            response = await client.post(
                "/tags/import",
                data={
                    "connection_id": str(connection.id),
                    "kind": "db_source",
                    "apply": "1",
                    "csrf_token": token,
                },
                files={"file": ("recipes.db", source.encode(), "text/plain")},
            )
            assert response.status_code == 303
            page = await client.get(f"/tags?connection_id={connection.id}")
            assert "optimized block access" in page.text
            assert runtime.db.list_tags(connection.id) == []

        run(scenario, tmp_path)

    def test_a_junk_upload_is_reported_not_a_500(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/connections/new")).text)
            connection = await self._connection(client, runtime, token)

            response = await client.post(
                "/tags/import",
                data={"connection_id": str(connection.id), "csrf_token": token},
                files={"file": ("junk.sdf", b"\x00\x01 not a symbol table", "text/plain")},
            )
            assert response.status_code in (200, 303)
            assert runtime.db.list_tags(connection.id) == []

        run(scenario, tmp_path)

    def test_import_requires_admin(self, tmp_path) -> None:
        async def scenario(client, runtime):
            await first_run_login(client, runtime)
            token = csrf_of((await client.get("/security")).text)
            await client.post(
                "/users/new",
                data={
                    "username": "viewer2",
                    "password": "Zr4$kQp8Nv2wLd",
                    "role": Role.VIEWER.value,
                    "csrf_token": token,
                },
            )
            connection = await self._connection(
                client, runtime, csrf_of((await client.get("/connections/new")).text)
            )

            viewer = httpx.AsyncClient(transport=client._transport, base_url="http://gw")
            try:
                await sign_in(viewer, "viewer2", "Zr4$kQp8Nv2wLd")
                page = await viewer.get("/password-change")
                await viewer.post(
                    "/password-change",
                    data={
                        "current_password": "Zr4$kQp8Nv2wLd",
                        "new_password": "Hx9!mTz3Qb6vRn",
                        "confirm_password": "Hx9!mTz3Qb6vRn",
                        "csrf_token": csrf_of(page.text),
                    },
                )
                await sign_in(viewer, "viewer2", "Hx9!mTz3Qb6vRn")
                viewer_token = csrf_of((await viewer.get("/tags")).text)
                response = await viewer.post(
                    "/tags/import",
                    data={"connection_id": str(connection.id), "apply": "1",
                          "csrf_token": viewer_token},
                    files={"file": ("line3.sdf", self.SDF.encode(), "text/plain")},
                )
                assert response.status_code == 303
                assert runtime.db.list_tags(connection.id) == []
            finally:
                await viewer.aclose()

        run(scenario, tmp_path)
