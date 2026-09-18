"""Command-line entrypoint (``snap7-gateway``).

Kept deliberately small: run the service, register/remove the platform service,
and the two recovery commands an operator needs when they are locked out of a
box on the shop floor.
"""

from __future__ import annotations

import argparse
import sys

from .. import __version__


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory for the database, logs, crash snapshots and TLS material "
             "(default: platform-specific; see docs/MANUAL.md)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snap7-gateway",
        description="Snap7 Industrial Gateway - S7 PLC to DeviceWise bridge",
    )
    parser.add_argument("--version", action="version", version=f"snap7-gateway {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the gateway in the foreground")
    _add_common(run)
    run.add_argument("--host", default=None, help="Override the web UI bind address")
    run.add_argument("--port", type=int, default=None, help="Override the web UI port")
    run.add_argument(
        "--no-console-log", action="store_true", help="Log to file only, not to stdout"
    )
    run.add_argument(
        "--trust-proxy-headers",
        action="store_true",
        help="Honour X-Forwarded-For (only when running behind a reverse proxy you control)",
    )

    install = subparsers.add_parser(
        "install-service", help="Register the Windows service (Windows only)"
    )
    _add_common(install)
    install.add_argument(
        "--manual", action="store_true", help="Install for manual start instead of automatic"
    )

    subparsers.add_parser("uninstall-service", help="Remove the Windows service (Windows only)")

    reset = subparsers.add_parser(
        "reset-password",
        help="Set a new password for an account from the console (recovery)",
    )
    _add_common(reset)
    reset.add_argument("username", help="Account to reset")

    unlock = subparsers.add_parser("unlock", help="Clear an account lockout (recovery)")
    _add_common(unlock)
    unlock.add_argument("username", help="Account to unlock")

    show = subparsers.add_parser("show-config", help="Print the resolved paths and settings")
    _add_common(show)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(build_parser().parse_args(argv))
    except BrokenPipeError:
        # e.g. `snap7-gateway show-config | head` - not an error worth a traceback.
        return 0
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace) -> int:

    if args.command == "run":
        from .server import run_gateway

        return run_gateway(
            args.data_dir,
            host=args.host,
            port=args.port,
            console_logging=not args.no_console_log,
            trust_proxy_headers=args.trust_proxy_headers,
        )

    if args.command == "install-service":
        from .windows_service import install

        if sys.platform != "win32":
            print("install-service is Windows-only. On Linux use install/linux/install.sh.",
                  file=sys.stderr)
            return 2
        install(args.data_dir, start_type="manual" if args.manual else "auto")
        return 0

    if args.command == "uninstall-service":
        from .windows_service import uninstall

        if sys.platform != "win32":
            print("uninstall-service is Windows-only.", file=sys.stderr)
            return 2
        uninstall()
        return 0

    if args.command in {"reset-password", "unlock", "show-config"}:
        return _offline_command(args)

    return 2  # pragma: no cover - argparse rejects unknown commands first


def _offline_command(args: argparse.Namespace) -> int:
    """Commands that touch the database directly, without starting the service.

    Intended to be run on the box itself, by someone who already has shell
    access - which is why they do not require a login.
    """
    from ..auth.hashing import hash_password
    from ..auth.policy import PasswordPolicy
    from ..auth.blocklist import Blocklist
    from ..db.store import Database
    from ..paths import DataPaths

    paths = DataPaths.resolve(args.data_dir).ensure()
    db = Database(paths.db_file)
    try:
        if args.command == "show-config":
            print(f"data directory : {paths.root}")
            print(f"database       : {paths.db_file}")
            print(f"log file       : {paths.log_file}")
            print(f"crash folder   : {paths.crash_dir}")
            print(f"certificates   : {paths.cert_dir}")
            print("\nsettings:")
            for key, value in sorted(db.get_settings().items()):
                print(f"  {key:34} {value}")
            return 0

        user = db.get_user(args.username)
        if user is None:
            print(f"No such user: {args.username}", file=sys.stderr)
            return 1

        if args.command == "unlock":
            db.unlock_user(user.id)
            db.audit("console", "user.unlock", user.username, "via CLI")
            print(f"Lockout cleared for '{user.username}'.")
            return 0

        import getpass

        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1

        errors = PasswordPolicy.from_db(db).validate(
            password, username=user.username, blocklist=Blocklist.load(paths.root)
        )
        if errors:
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1

        db.update_password(user.id, hash_password(password), user.password_hash)
        db.set_must_change_password(user.id, True)
        db.delete_sessions_for_user(user.id)
        db.audit("console", "user.password_reset", user.username, "via CLI")
        print(
            f"Password updated for '{user.username}'. All sessions were signed out and the "
            "password must be changed again at next sign-in."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
