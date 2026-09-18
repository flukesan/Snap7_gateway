"""Filesystem layout for gateway runtime data.

All mutable state (SQLite database, logs, crash snapshots, TLS material) lives
under a single data directory so that an operator can back up or wipe the
gateway by touching exactly one path. The location is resolved in this order:

1. explicit ``--data-dir`` / constructor argument,
2. the ``SNAP7_GATEWAY_DATA_DIR`` environment variable,
3. a platform default (``%PROGRAMDATA%\\Snap7Gateway`` on Windows,
   ``/var/lib/snap7-gateway`` on Linux when writable, else ``~/.snap7-gateway``).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_DATA_DIR = "SNAP7_GATEWAY_DATA_DIR"


def default_data_dir() -> Path:
    """Return the platform-appropriate default data directory."""
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE")
        if base:
            return Path(base) / "Snap7Gateway"
        return Path.home() / ".snap7-gateway"
    system = Path("/var/lib/snap7-gateway")
    try:
        system.mkdir(parents=True, exist_ok=True)
        probe = system / ".write-probe"
        probe.touch()
        probe.unlink()
        return system
    except OSError:
        # Unprivileged run (developer laptop, container without root).
        return Path.home() / ".snap7-gateway"


@dataclass(frozen=True)
class DataPaths:
    """Resolved on-disk locations used by every subsystem."""

    root: Path

    @classmethod
    def resolve(cls, data_dir: str | os.PathLike[str] | None = None) -> "DataPaths":
        root = Path(data_dir).expanduser() if data_dir else default_data_dir()
        return cls(root=root)

    @property
    def db_file(self) -> Path:
        return self.root / "gateway.db"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "gateway.log"

    @property
    def crash_dir(self) -> Path:
        return self.root / "crash"

    @property
    def cert_dir(self) -> Path:
        return self.root / "certs"

    @property
    def cert_file(self) -> Path:
        return self.cert_dir / "server.crt"

    @property
    def key_file(self) -> Path:
        return self.cert_dir / "server.key"

    @property
    def runtime_marker(self) -> Path:
        """Marker file used to detect an unclean previous shutdown."""
        return self.root / "running.marker"

    def ensure(self) -> "DataPaths":
        """Create every directory this layout needs. Idempotent."""
        for path in (self.root, self.log_dir, self.crash_dir, self.cert_dir):
            path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            # Credentials hashes and the TLS private key live here.
            try:
                self.root.chmod(0o750)
                self.cert_dir.chmod(0o700)
            except OSError:
                pass
        return self
