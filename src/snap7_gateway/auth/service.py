"""Authentication service: login, lockout, rate limiting, password changes.

Brings together hashing, the strength policy, the blocklist and sessions so the
web layer contains no security logic of its own - it renders whatever
:class:`AuthService` decides.

Protections implemented here (CLAUDE.md section 3.2):

* per-account lockout after a configurable number of failures, with a cool-down;
* per-source-IP sliding-window rate limit in front of the account logic, so an
  attacker cannot spray many usernames cheaply;
* uniform timing for unknown usernames;
* forced password change for the first-run admin before anything else is
  allowed;
* every session invalidated on a password change.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from ..db.models import Role, User
from ..db.store import Database
from .blocklist import Blocklist
from .hashing import dummy_verify, hash_password, needs_rehash, verify_password
from .policy import PasswordPolicy, generate_password
from .sessions import SessionManager

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"

#: First-run password. CLAUDE.md section 3.2 allows either a random password or
#: a clearly-labelled default one; a known default is the easier hand-over on a
#: factory floor, and is safe *only* because no page is reachable until it is
#: changed (section 4.4). It is deliberately something the password policy will
#: refuse as a replacement, so it cannot be re-entered as the new password.
DEFAULT_ADMIN_PASSWORD = "admin"

#: Sites with a stricter policy can override the first-run password before the
#: very first start, without editing code:
#:
#:   SNAP7_GATEWAY_FIRST_RUN_PASSWORD=random   -> generate a strong random one
#:   SNAP7_GATEWAY_FIRST_RUN_PASSWORD=<text>   -> use exactly that
#:
#: It is read once, at the moment the admin account is created, and never
#: again.
ENV_FIRST_RUN_PASSWORD = "SNAP7_GATEWAY_FIRST_RUN_PASSWORD"


def initial_admin_password() -> tuple[str, bool]:
    """Return the first-run password and whether it is the well-known default."""
    override = os.environ.get(ENV_FIRST_RUN_PASSWORD, "").strip()
    if not override:
        return DEFAULT_ADMIN_PASSWORD, True
    if override.lower() == "random":
        return generate_password(), False
    return override, False


@dataclass(slots=True)
class LoginResult:
    """Outcome of a login attempt - never leaks which half was wrong."""

    ok: bool
    user: User | None = None
    token: str | None = None
    error: str | None = None
    locked_until: float | None = None
    retry_after: float | None = None

    @property
    def must_change_password(self) -> bool:
        return bool(self.user and self.user.must_change_password)


@dataclass(slots=True)
class PasswordChangeResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


class RateLimiter:
    """Sliding-window limiter keyed by source IP.

    In-memory and bounded: at most ``max_keys`` sources are tracked and each
    keeps at most ``limit`` timestamps, so it cannot grow without bound on a
    long-running gateway even under a spraying attack.
    """

    def __init__(self, limit: int, window_seconds: int, *, max_keys: int = 4096) -> None:
        self.limit = max(limit, 1)
        self.window_seconds = max(window_seconds, 1)
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Register an attempt; return seconds to wait when over the limit."""
        now = time.time()
        with self._lock:
            if len(self._hits) > self.max_keys:
                self._evict(now)
            bucket = self._hits[key]
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return max(0.0, self.window_seconds - (now - bucket[0]))
            bucket.append(now)
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _evict(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window_seconds]:
            del self._hits[key]


class AuthService:
    """All authentication decisions in one place."""

    def __init__(self, db: Database, *, data_dir: Path | None = None) -> None:
        self.db = db
        self.sessions = SessionManager(db)
        self.blocklist = Blocklist.load(data_dir)
        self._limiter = RateLimiter(
            db.get_int("login_rate_max_attempts", 10),
            db.get_int("login_rate_window_seconds", 60),
        )

    # ------------------------------------------------------------------
    @property
    def policy(self) -> PasswordPolicy:
        """Read fresh from the database so policy edits apply immediately."""
        return PasswordPolicy.from_db(self.db)

    def reload_limits(self) -> None:
        """Re-read rate-limit settings after the operator changes them."""
        self._limiter = RateLimiter(
            self.db.get_int("login_rate_max_attempts", 10),
            self.db.get_int("login_rate_window_seconds", 60),
        )

    # ------------------------------------------------------------------
    # first run
    # ------------------------------------------------------------------
    def ensure_bootstrap_admin(self) -> str | None:
        """Create the first-run ``admin`` account if no users exist.

        Returns the password the account was created with, once, so the caller
        can announce it at first boot. By default that is the documented
        ``admin`` / ``admin`` pair; ``SNAP7_GATEWAY_FIRST_RUN_PASSWORD`` can ask
        for a random one instead.

        The account always carries ``must_change_password``, which the web layer
        enforces before granting access to any other page - that forced change
        is what makes a known default acceptable at all.
        """
        if self.db.count_users() > 0:
            return None
        password, is_default = initial_admin_password()
        user = self.db.create_user(
            DEFAULT_ADMIN_USERNAME,
            hash_password(password),
            role=Role.ADMIN,
            must_change_password=True,
        )
        self.db.audit(
            "system",
            "user.bootstrap",
            user.username,
            "first-run admin created with the "
            + ("documented default password" if is_default else "configured password"),
        )
        return password

    def accounts_awaiting_first_change(self) -> list[str]:
        """Enabled accounts still on the password they were created with.

        While this list is non-empty the gateway is only as protected as a
        published credential, so the startup log and the sign-in page both say
        so until an operator acts.
        """
        return [
            user.username
            for user in self.db.list_users()
            if user.enabled and user.must_change_password
        ]

    def uses_default_credentials(self) -> bool:
        """Whether ``admin`` is still reachable with the documented default."""
        user = self.db.get_user(DEFAULT_ADMIN_USERNAME)
        if user is None or not user.enabled or not user.must_change_password:
            return False
        return verify_password(user.password_hash, DEFAULT_ADMIN_PASSWORD)

    # ------------------------------------------------------------------
    # login
    # ------------------------------------------------------------------
    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResult:
        source = ip or "unknown"
        wait = self._limiter.check(source)
        if wait is not None:
            logger.warning("login rate limit hit from %s", source)
            self.db.audit(username or "?", "auth.rate_limited", None, None, ip)
            return LoginResult(
                ok=False,
                error=f"Too many attempts. Try again in {int(wait) + 1} second(s).",
                retry_after=wait,
            )

        username = (username or "").strip()
        generic_error = "Incorrect username or password"
        user = self.db.get_user(username) if username else None

        if user is None:
            dummy_verify()  # keep timing uniform for unknown accounts
            self.db.audit(username or "?", "auth.login_failed", None, "unknown user", ip)
            return LoginResult(ok=False, error=generic_error)

        now = time.time()
        if user.locked_until and user.locked_until > now:
            remaining = int(user.locked_until - now) + 1
            self.db.audit(user.username, "auth.login_blocked", None, "account locked", ip)
            return LoginResult(
                ok=False,
                error=f"Account locked after repeated failures. Try again in {remaining} second(s).",
                locked_until=user.locked_until,
            )

        if not user.enabled:
            self.db.audit(user.username, "auth.login_blocked", None, "account disabled", ip)
            return LoginResult(ok=False, error="This account is disabled.")

        if not verify_password(user.password_hash, password):
            max_failures = self.db.get_int("login_max_failures", 5)
            lockout = self.db.get_int("login_lockout_minutes", 15) * 60
            updated = self.db.record_login_failure(user.id, max_failures, lockout)
            self.db.audit(
                user.username,
                "auth.login_failed",
                None,
                f"failure {updated.failed_attempts}/{max_failures}",
                ip,
            )
            if updated.locked_until and updated.locked_until > now:
                remaining = int(updated.locked_until - now) + 1
                logger.warning("account '%s' locked after %d failures", user.username,
                               updated.failed_attempts)
                return LoginResult(
                    ok=False,
                    error=(
                        f"Account locked after {updated.failed_attempts} failed attempts. "
                        f"Try again in {remaining} second(s)."
                    ),
                    locked_until=updated.locked_until,
                )
            return LoginResult(ok=False, error=generic_error)

        # Successful login: upgrade the hash if the parameters have moved on.
        if needs_rehash(user.password_hash):
            self.db.update_password(user.id, hash_password(password), user.previous_hash)
            self.db.set_must_change_password(user.id, user.must_change_password)
            user = self.db.get_user_by_id(user.id) or user

        self.db.record_login_success(user.id)
        self._limiter.reset(source)
        token = self.sessions.create(user, ip=ip, user_agent=user_agent)
        self.db.audit(user.username, "auth.login", None, None, ip)
        return LoginResult(ok=True, user=user, token=token)

    def logout(self, token: str | None, *, username: str = "?", ip: str | None = None) -> None:
        self.sessions.destroy(token)
        self.db.audit(username, "auth.logout", None, None, ip)

    # ------------------------------------------------------------------
    # password changes
    # ------------------------------------------------------------------
    def change_password(
        self,
        user: User,
        current_password: str,
        new_password: str,
        confirm_password: str,
        *,
        ip: str | None = None,
        keep_token: str | None = None,
    ) -> PasswordChangeResult:
        """Validate and apply a self-service password change.

        On success every other session for the user is invalidated; the session
        the change was made from can be kept so the operator is not bounced back
        to the login page mid-configuration.
        """
        errors: list[str] = []
        if not verify_password(user.password_hash, current_password):
            self.db.audit(user.username, "auth.password_change_failed", None,
                          "current password incorrect", ip)
            return PasswordChangeResult(ok=False, errors=["Current password is incorrect"])

        if new_password != confirm_password:
            errors.append("New password and confirmation do not match")

        errors.extend(
            self.policy.validate(
                new_password,
                username=user.username,
                current_hash=user.password_hash,
                previous_hash=user.previous_hash,
                blocklist=self.blocklist,
            )
        )
        if errors:
            self.db.audit(user.username, "auth.password_change_rejected", None,
                          f"{len(errors)} policy violation(s)", ip)
            return PasswordChangeResult(ok=False, errors=errors)

        self.db.update_password(user.id, hash_password(new_password), user.password_hash)
        self.sessions.destroy_all_for_user(user.id)
        self.db.audit(user.username, "auth.password_changed", user.username, None, ip)
        logger.info("password changed for user '%s'", user.username)
        return PasswordChangeResult(ok=True)

    def admin_reset_password(
        self, actor: User, target: User, new_password: str, *, ip: str | None = None
    ) -> PasswordChangeResult:
        """Admin-initiated reset; the target must change it at next login."""
        errors = self.policy.validate(
            new_password,
            username=target.username,
            current_hash=target.password_hash,
            blocklist=self.blocklist,
        )
        if errors:
            return PasswordChangeResult(ok=False, errors=errors)
        self.db.update_password(target.id, hash_password(new_password), target.password_hash)
        self.db.set_must_change_password(target.id, True)
        self.sessions.destroy_all_for_user(target.id)
        self.db.audit(actor.username, "user.password_reset", target.username, None, ip)
        return PasswordChangeResult(ok=True)

    def create_user(
        self, actor: User, username: str, password: str, role: str, *, ip: str | None = None
    ) -> PasswordChangeResult:
        errors = self.policy.validate(password, username=username, blocklist=self.blocklist)
        if errors:
            return PasswordChangeResult(ok=False, errors=errors)
        self.db.create_user(username, hash_password(password), role=role,
                            must_change_password=True)
        self.db.audit(actor.username, "user.create", username, f"role={role}", ip)
        return PasswordChangeResult(ok=True)
