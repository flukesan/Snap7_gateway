"""Configurable password strength policy (CLAUDE.md section 3.2).

Every rule is configurable with a sane default, and every rejection comes back
as a specific reason - "Password must contain at least one digit", never just
"invalid". The same policy object is used by the forced first-login change, by
later voluntary changes and by admin-initiated resets, so the rules cannot
drift apart between entry points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..db.store import Database
from .blocklist import Blocklist
from .hashing import verify_password

SYMBOLS = set("!@#$%^&*()-_=+[]{};:'\",.<>/?\\|`~")

#: Absolute floor; the configurable minimum can be raised but never lowered
#: below this without editing the source.
ABSOLUTE_MIN_LENGTH = 12
MAX_LENGTH = 1024  # guards against hashing-cost denial of service


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """The active password rules."""

    min_length: int = ABSOLUTE_MIN_LENGTH
    require_upper: bool = True
    require_lower: bool = True
    require_digit: bool = True
    require_symbol: bool = False
    forbid_username: bool = True
    forbid_previous: bool = True
    use_blocklist: bool = True

    @classmethod
    def from_db(cls, db: Database) -> "PasswordPolicy":
        return cls(
            min_length=max(db.get_int("password_min_length", ABSOLUTE_MIN_LENGTH),
                           ABSOLUTE_MIN_LENGTH),
            require_upper=db.get_bool("password_require_upper", True),
            require_lower=db.get_bool("password_require_lower", True),
            require_digit=db.get_bool("password_require_digit", True),
            require_symbol=db.get_bool("password_require_symbol", False),
            forbid_username=db.get_bool("password_forbid_username", True),
            forbid_previous=db.get_bool("password_forbid_previous", True),
            use_blocklist=db.get_bool("password_use_blocklist", True),
        )

    def describe(self) -> list[str]:
        """Human-readable rule list rendered next to the password fields."""
        rules = [f"At least {self.min_length} characters"]
        if self.require_lower:
            rules.append("At least one lower-case letter")
        if self.require_upper:
            rules.append("At least one upper-case letter")
        if self.require_digit:
            rules.append("At least one digit")
        if self.require_symbol:
            rules.append("At least one symbol (for example ! @ # $ %)")
        if self.forbid_username:
            rules.append("Must not contain your username")
        if self.forbid_previous:
            rules.append("Must not be the same as your current password")
        if self.use_blocklist:
            rules.append("Must not be a common or easily guessed password")
        return rules

    def validate(
        self,
        password: str,
        *,
        username: str | None = None,
        current_hash: str | None = None,
        previous_hash: str | None = None,
        blocklist: Blocklist | None = None,
    ) -> list[str]:
        """Return every reason the password is unacceptable (empty = accepted)."""
        reasons: list[str] = []

        if password is None:
            return ["Password is required"]
        if len(password) > MAX_LENGTH:
            return [f"Password must be at most {MAX_LENGTH} characters"]
        if len(password) < self.min_length:
            reasons.append(f"Password must be at least {self.min_length} characters long")
        if password != password.strip():
            reasons.append("Password must not start or end with a space")

        if self.require_lower and not any(c.islower() for c in password):
            reasons.append("Password must contain at least one lower-case letter")
        if self.require_upper and not any(c.isupper() for c in password):
            reasons.append("Password must contain at least one upper-case letter")
        if self.require_digit and not any(c.isdigit() for c in password):
            reasons.append("Password must contain at least one digit")
        if self.require_symbol and not any(c in SYMBOLS for c in password):
            reasons.append("Password must contain at least one symbol, for example ! @ # $ %")

        if self.forbid_username and username:
            lowered = password.lower()
            if username.lower() in lowered or lowered in username.lower():
                reasons.append("Password must not contain your username")

        if self.forbid_previous and current_hash and verify_password(current_hash, password):
            reasons.append("New password must be different from your current password")
        if self.forbid_previous and previous_hash and verify_password(previous_hash, password):
            reasons.append("New password must be different from your previous password")

        if self.use_blocklist:
            blocked = (blocklist or Blocklist()).reason(password)
            if blocked:
                reasons.append(blocked)

        return reasons


def generate_password(length: int = 20) -> str:
    """Generate a strong random password for the first-run admin account.

    Uses an unambiguous alphabet (no ``0/O`` or ``1/l/I``) because an operator
    reads this off a console or log line and types it into the browser once.
    """
    import secrets

    lowers = "abcdefghijkmnopqrstuvwxyz"
    uppers = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    symbols = "!@#$%^&*-_=+"
    alphabet = lowers + uppers + digits + symbols
    length = max(length, ABSOLUTE_MIN_LENGTH + 4)

    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c in lowers for c in candidate)
            and any(c in uppers for c in candidate)
            and any(c in digits for c in candidate)
            and any(c in symbols for c in candidate)
            and not Blocklist().reason(candidate)
        ):
            return candidate


def policy_errors_text(reasons: Sequence[str]) -> str:
    return " ".join(reasons)
