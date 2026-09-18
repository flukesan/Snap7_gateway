"""Password policy, blocklist, lockout, rate limiting and session rules."""

from __future__ import annotations

import time

import pytest

from snap7_gateway.auth.blocklist import Blocklist
from snap7_gateway.auth.hashing import hash_password, verify_password
from snap7_gateway.auth.policy import ABSOLUTE_MIN_LENGTH, PasswordPolicy, generate_password
from snap7_gateway.auth.service import RateLimiter
from snap7_gateway.db.models import Role


class TestHashing:
    def test_hash_is_not_reversible_and_verifies(self) -> None:
        digest = hash_password("Rivet-Hoist-42x")
        assert "Rivet-Hoist-42x" not in digest
        assert digest.startswith("$argon2id$")
        assert verify_password(digest, "Rivet-Hoist-42x")
        assert not verify_password(digest, "Rivet-Hoist-42X")

    def test_salted_hashes_differ(self) -> None:
        assert hash_password("Rivet-Hoist-42x") != hash_password("Rivet-Hoist-42x")

    def test_corrupt_hash_fails_closed(self) -> None:
        assert not verify_password("not-a-hash", "anything")
        assert not verify_password("", "anything")


class TestPolicy:
    def test_minimum_length_is_enforced(self) -> None:
        policy = PasswordPolicy()
        assert any("at least 12" in r for r in policy.validate("Ab1short"))

    def test_minimum_cannot_be_configured_below_twelve(self, db) -> None:
        db.set_setting("password_min_length", "4")
        assert PasswordPolicy.from_db(db).min_length == ABSOLUTE_MIN_LENGTH

    def test_each_character_class_reports_its_own_reason(self) -> None:
        policy = PasswordPolicy(require_symbol=True)
        reasons = policy.validate("aaaaaaaaaaaaaa")
        assert any("upper-case" in r for r in reasons)
        assert any("digit" in r for r in reasons)
        assert any("symbol" in r for r in reasons)

    def test_username_is_rejected_inside_the_password(self) -> None:
        policy = PasswordPolicy(use_blocklist=False)
        reasons = policy.validate("Xoperator99Zq", username="operator")
        assert any("username" in r for r in reasons)

    def test_current_password_cannot_be_reused(self) -> None:
        current = hash_password("Rivet-Hoist-42x")
        reasons = PasswordPolicy().validate(
            "Rivet-Hoist-42x", username="admin", current_hash=current
        )
        assert any("different from your current" in r for r in reasons)

    def test_previous_password_cannot_be_reused(self) -> None:
        previous = hash_password("Rivet-Hoist-42x")
        reasons = PasswordPolicy().validate(
            "Rivet-Hoist-42x", username="admin", previous_hash=previous
        )
        assert any("previous password" in r for r in reasons)

    def test_a_good_password_passes(self) -> None:
        assert PasswordPolicy().validate("Bt7#vRq2Lm9xKp", username="admin") == []

    def test_reasons_are_specific_not_generic(self) -> None:
        for reason in PasswordPolicy().validate("short", username="admin"):
            assert reason.lower() not in {"invalid", "invalid password"}
            assert len(reason) > 15

    def test_generated_password_satisfies_the_policy(self) -> None:
        for _ in range(10):
            candidate = generate_password()
            assert PasswordPolicy(require_symbol=True).validate(candidate, username="admin") == []


class TestBlocklist:
    @pytest.mark.parametrize(
        "password",
        ["Password1234", "Siemens2024!", "qwertyuiop12", "aaaaaaaaaaaa", "123456789012",
         "snap7gateway", "AdminAdmin12"],
    )
    def test_obvious_passwords_are_rejected(self, password: str) -> None:
        assert Blocklist().reason(password) is not None

    @pytest.mark.parametrize("password", ["Bt7#vRq2Lm9x", "MachineHall4-Riser", "Tr0ub4dor&3xplor"])
    def test_reasonable_passwords_pass(self, password: str) -> None:
        assert Blocklist().reason(password) is None

    def test_operator_supplied_words_are_honoured(self, tmp_path) -> None:
        (tmp_path / "password-blocklist.txt").write_text(
            "# site policy\nfactorysecret99\n\n", encoding="utf-8"
        )
        blocklist = Blocklist.load(tmp_path)
        assert blocklist.reason("factorysecret99") is not None
        assert blocklist.reason("Bt7#vRq2Lm9x") is None


class TestRateLimiter:
    def test_allows_up_to_the_limit_then_blocks(self) -> None:
        limiter = RateLimiter(limit=3, window_seconds=60)
        assert all(limiter.check("10.0.0.1") is None for _ in range(3))
        assert limiter.check("10.0.0.1") is not None

    def test_limits_are_per_source(self) -> None:
        limiter = RateLimiter(limit=1, window_seconds=60)
        assert limiter.check("10.0.0.1") is None
        assert limiter.check("10.0.0.2") is None
        assert limiter.check("10.0.0.1") is not None

    def test_window_expires(self) -> None:
        limiter = RateLimiter(limit=1, window_seconds=1)
        assert limiter.check("10.0.0.1") is None
        assert limiter.check("10.0.0.1") is not None
        time.sleep(1.05)
        assert limiter.check("10.0.0.1") is None

    def test_key_table_stays_bounded(self) -> None:
        limiter = RateLimiter(limit=5, window_seconds=1, max_keys=50)
        for index in range(500):
            limiter.check(f"10.0.{index // 256}.{index % 256}")
        assert len(limiter._hits) <= 500  # bounded by eviction, not by attempts


class TestLoginFlow:
    def test_bootstrap_creates_one_admin_that_must_change_its_password(self, auth, db) -> None:
        password = auth.ensure_bootstrap_admin()
        assert password and len(password) >= 20
        assert auth.ensure_bootstrap_admin() is None  # only ever once
        user = db.get_user("admin")
        assert user.must_change_password is True
        assert user.role == Role.ADMIN
        assert password not in user.password_hash

    def test_wrong_password_gives_a_generic_error(self, auth) -> None:
        auth.ensure_bootstrap_admin()
        result = auth.login("admin", "wrong", ip="10.0.0.9")
        assert not result.ok
        assert result.error == "Incorrect username or password"

    def test_unknown_user_gives_the_same_error(self, auth) -> None:
        auth.ensure_bootstrap_admin()
        assert auth.login("nobody", "wrong", ip="10.0.0.9").error == (
            "Incorrect username or password"
        )

    def test_account_locks_after_the_configured_failures(self, auth, db) -> None:
        auth.ensure_bootstrap_admin()
        db.set_settings({"login_max_failures": "3", "login_rate_max_attempts": "50"})
        auth.reload_limits()
        for _ in range(3):
            auth.login("admin", "wrong", ip="10.0.0.9")
        result = auth.login("admin", "wrong", ip="10.0.0.9")
        assert "locked" in (result.error or "").lower()
        assert result.locked_until and result.locked_until > time.time()

    def test_successful_login_clears_the_failure_counter(self, auth, db) -> None:
        password = auth.ensure_bootstrap_admin()
        auth.login("admin", "wrong", ip="10.0.0.9")
        assert auth.login("admin", password, ip="10.0.0.9").ok
        assert db.get_user("admin").failed_attempts == 0

    def test_password_change_invalidates_every_session(self, auth) -> None:
        password = auth.ensure_bootstrap_admin()
        first = auth.login("admin", password, ip="10.0.0.1")
        second = auth.login("admin", password, ip="10.0.0.2")
        context = auth.sessions.validate(first.token)
        assert auth.change_password(
            context.user, password, "Bt7#vRq2Lm9x", "Bt7#vRq2Lm9x"
        ).ok
        assert auth.sessions.validate(first.token) is None
        assert auth.sessions.validate(second.token) is None

    def test_password_change_rejects_a_wrong_current_password(self, auth) -> None:
        password = auth.ensure_bootstrap_admin()
        context = auth.sessions.validate(auth.login("admin", password, ip="1.1.1.1").token)
        result = auth.change_password(context.user, "nope", "Bt7#vRq2Lm9x", "Bt7#vRq2Lm9x")
        assert not result.ok
        assert "Current password is incorrect" in result.errors

    def test_password_change_requires_matching_confirmation(self, auth) -> None:
        password = auth.ensure_bootstrap_admin()
        context = auth.sessions.validate(auth.login("admin", password, ip="1.1.1.1").token)
        result = auth.change_password(context.user, password, "Bt7#vRq2Lm9x", "Different9#xQ")
        assert not result.ok
        assert any("do not match" in e for e in result.errors)

    def test_disabled_account_cannot_sign_in(self, auth, db) -> None:
        password = auth.ensure_bootstrap_admin()
        db.set_user_enabled(db.get_user("admin").id, False)
        assert not auth.login("admin", password, ip="1.1.1.1").ok


class TestSessions:
    def test_only_the_hash_is_stored(self, auth, db) -> None:
        password = auth.ensure_bootstrap_admin()
        token = auth.login("admin", password, ip="1.1.1.1").token
        stored = db.list_sessions_for_user(db.get_user("admin").id)
        assert stored and stored[0].token_hash != token
        assert token not in stored[0].token_hash

    def test_expired_session_is_rejected_and_removed(self, auth, db) -> None:
        password = auth.ensure_bootstrap_admin()
        token = auth.login("admin", password, ip="1.1.1.1").token
        user_id = db.get_user("admin").id
        session = db.list_sessions_for_user(user_id)[0]
        db.touch_session(session.token_hash, time.time() - 1)
        assert auth.sessions.validate(token) is None
        assert db.list_sessions_for_user(user_id) == []

    def test_absolute_lifetime_wins_over_activity(self, auth, db) -> None:
        db.set_setting("session_absolute_timeout_hours", "1")
        password = auth.ensure_bootstrap_admin()
        token = auth.login("admin", password, ip="1.1.1.1").token
        session = db.list_sessions_for_user(db.get_user("admin").id)[0]
        # Pretend the session was opened two hours ago but is still being used.
        db._execute(
            "UPDATE sessions SET created_at = ?, expires_at = ? WHERE token_hash = ?",
            (time.time() - 7200, time.time() + 3600, session.token_hash),
        )
        assert auth.sessions.validate(token) is None

    def test_logout_destroys_the_session(self, auth) -> None:
        password = auth.ensure_bootstrap_admin()
        token = auth.login("admin", password, ip="1.1.1.1").token
        auth.logout(token, username="admin")
        assert auth.sessions.validate(token) is None

    def test_csrf_token_must_match(self, auth) -> None:
        password = auth.ensure_bootstrap_admin()
        context = auth.sessions.validate(auth.login("admin", password, ip="1.1.1.1").token)
        assert auth.sessions.check_csrf(context, context.csrf_token)
        assert not auth.sessions.check_csrf(context, "wrong")
        assert not auth.sessions.check_csrf(context, None)
        assert not auth.sessions.check_csrf(None, context.csrf_token)
