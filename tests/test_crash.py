"""Crash snapshots: capture real failures, ignore routine disconnects."""

from __future__ import annotations

import asyncio
import ssl

import pytest

from snap7_gateway.logging_.crash import (
    CrashReporter,
    _is_routine_disconnect,
    install_asyncio_handler,
)
from snap7_gateway.logging_.ringbuffer import LogRingBuffer


@pytest.fixture()
def reporter(tmp_path) -> CrashReporter:
    return CrashReporter(tmp_path / "crash", LogRingBuffer(50), tmp_path / "running.marker")


class TestRoutineDisconnects:
    """A peer hanging up is not a crash.

    Regression: on Windows the Proactor loop reports every dropped client
    connection through the asyncio exception handler, which wrote a crash
    snapshot each time. The snapshot folder is capped, so that noise pushed
    genuine crashes out of it.
    """

    @pytest.mark.parametrize(
        "exception",
        [
            ConnectionResetError(10054, "An existing connection was forcibly closed"),
            ConnectionAbortedError(),
            BrokenPipeError(),
            TimeoutError(),
            ssl.SSLError("handshake abandoned"),
        ],
    )
    def test_recognised_as_routine(self, exception: BaseException) -> None:
        assert _is_routine_disconnect(exception)

    def test_windows_socket_errors_by_code(self) -> None:
        error = OSError("connection reset")
        error.winerror = 10054  # type: ignore[attr-defined]
        assert _is_routine_disconnect(error)

    @pytest.mark.parametrize("exception", [ValueError("a real bug"), KeyError("missing"), None])
    def test_real_failures_are_not_routine(self, exception) -> None:
        assert not _is_routine_disconnect(exception)


class TestAsyncioHandler:
    def _handle(self, reporter: CrashReporter, exception: BaseException | None) -> int:
        """Run the installed handler once and return the snapshot count."""

        async def main() -> None:
            loop = asyncio.get_running_loop()
            install_asyncio_handler(loop, reporter)
            loop.call_exception_handler(
                {"message": "Exception in callback", "exception": exception}
            )

        asyncio.run(main())
        return len(reporter.list_snapshots())

    def test_a_dropped_client_writes_no_snapshot(self, reporter: CrashReporter) -> None:
        assert self._handle(reporter, ConnectionResetError(10054, "forcibly closed")) == 0

    def test_a_real_exception_still_writes_one(self, reporter: CrashReporter) -> None:
        assert self._handle(reporter, ValueError("genuine failure")) == 1


class TestSnapshotContents:
    def test_state_providers_are_included(self, reporter: CrashReporter) -> None:
        reporter.register_state_provider("connections", lambda: [{"name": "Line3"}])
        path = reporter.write_snapshot("test", None)
        assert path is not None
        assert "Line3" in path.read_text(encoding="utf-8")

    def test_a_failing_provider_does_not_break_the_snapshot(self, reporter: CrashReporter) -> None:
        def boom():
            raise RuntimeError("provider is broken")

        reporter.register_state_provider("bad", boom)
        path = reporter.write_snapshot("test", None)
        assert path is not None
        assert "state provider failed" in path.read_text(encoding="utf-8")

    def test_snapshots_are_pruned_to_the_cap(self, reporter: CrashReporter) -> None:
        reporter.max_files = 3
        for _ in range(6):
            reporter.write_snapshot("test", None)
        assert len(reporter.list_snapshots()) == 3

    def test_unclean_shutdown_is_detected_once(self, reporter: CrashReporter) -> None:
        reporter.mark_running()
        assert reporter.check_previous_shutdown() is not None
        assert reporter.previous_unclean_shutdown is not None
        # The marker is consumed, so a clean next boot reports nothing.
        assert reporter.check_previous_shutdown() is None

    def test_a_clean_shutdown_leaves_no_marker(self, reporter: CrashReporter) -> None:
        reporter.mark_running()
        reporter.mark_stopped()
        assert reporter.check_previous_shutdown() is None
