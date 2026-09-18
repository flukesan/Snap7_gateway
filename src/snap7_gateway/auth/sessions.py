"""Server-side session handling (CLAUDE.md section 3.2).

Design points that matter for an industrial box on a hostile VLAN:

* the cookie carries a 256-bit random token; only its SHA-256 **hash** is
  stored, so a stolen database file does not yield usable sessions;
* two independent expiries - an idle timeout and an absolute lifetime - so a
  forgotten browser on the shop floor cannot hold a session open forever;
* every session carries its own CSRF token, checked on every state-changing
  POST;
* sessions are invalidated on logout, on password change and when a user is
  disabled or deleted.

Tokens are never logged, not even truncated, anywhere in this module.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

from ..db.models import Session, User
from ..db.store import Database

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "s7gw_session"
TOKEN_BYTES = 32
CSRF_BYTES = 32


def hash_token(token: str) -> str:
    """SHA-256 of the raw token - what actually lands in SQLite."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionContext:
    """A validated session together with its user."""

    session: Session
    user: User
    token: str

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


class SessionManager:
    """Creates, validates and revokes sessions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    @property
    def idle_timeout_seconds(self) -> int:
        return max(self.db.get_int("session_idle_timeout_minutes", 30), 1) * 60

    @property
    def absolute_timeout_seconds(self) -> int:
        return max(self.db.get_int("session_absolute_timeout_hours", 12), 1) * 3600

    # ------------------------------------------------------------------
    def create(self, user: User, *, ip: str | None = None, user_agent: str | None = None) -> str:
        """Start a session and return the raw cookie token (never stored)."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        csrf = secrets.token_urlsafe(CSRF_BYTES)
        now = time.time()
        expires_at = min(now + self.idle_timeout_seconds, now + self.absolute_timeout_seconds)
        self.db.create_session(
            hash_token(token),
            user.id,
            csrf,
            expires_at,
            ip=ip,
            user_agent=(user_agent or "")[:255] or None,
        )
        logger.info("session started for user '%s' from %s", user.username, ip or "unknown")
        return token

    def validate(self, token: str | None) -> SessionContext | None:
        """Return the session context, or ``None`` when it is absent/expired.

        Sliding idle expiry is applied here: a valid request pushes the expiry
        forward, but never past the absolute lifetime.
        """
        if not token:
            return None
        token_hash = hash_token(token)
        session = self.db.get_session(token_hash)
        if session is None:
            return None

        now = time.time()
        if session.expires_at <= now:
            self.db.delete_session(token_hash)
            return None
        if now - session.created_at >= self.absolute_timeout_seconds:
            self.db.delete_session(token_hash)
            logger.info("session for user id %s hit its absolute lifetime", session.user_id)
            return None

        user = self.db.get_user_by_id(session.user_id)
        if user is None or not user.enabled:
            self.db.delete_session(token_hash)
            return None

        new_expiry = min(
            now + self.idle_timeout_seconds, session.created_at + self.absolute_timeout_seconds
        )
        # Only write when it actually moves, to keep SQLite writes off the hot path.
        if new_expiry - session.expires_at > 30:
            self.db.touch_session(token_hash, new_expiry)
            session = self.db.get_session(token_hash) or session

        return SessionContext(session=session, user=user, token=token)

    def destroy(self, token: str | None) -> None:
        if token:
            self.db.delete_session(hash_token(token))

    def destroy_all_for_user(self, user_id: int) -> int:
        """Invalidate every session of a user (password change, disable, delete)."""
        count = self.db.delete_sessions_for_user(user_id)
        if count:
            logger.info("invalidated %d session(s) for user id %s", count, user_id)
        return count

    def purge_expired(self) -> int:
        return self.db.purge_expired_sessions()

    def check_csrf(self, context: SessionContext | None, submitted: str | None) -> bool:
        """Constant-time CSRF comparison."""
        if context is None or not submitted:
            return False
        return secrets.compare_digest(context.csrf_token, submitted)
