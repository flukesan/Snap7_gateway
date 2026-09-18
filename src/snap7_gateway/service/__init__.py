"""Service integration: process entrypoint, CLI and platform service wrappers."""

from .cli import main
from .server import build_server, run_gateway

__all__ = ["main", "build_server", "run_gateway"]
