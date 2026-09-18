"""Web configuration UI: FastAPI application, routes, templates and TLS setup."""

from .app import create_app
from .i18n import SUPPORTED_LOCALES, Translator
from .tls import certificate_summary, ensure_certificate

__all__ = ["create_app", "SUPPORTED_LOCALES", "Translator", "ensure_certificate",
           "certificate_summary"]
