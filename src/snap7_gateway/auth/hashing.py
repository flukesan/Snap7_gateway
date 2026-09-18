"""Password hashing with Argon2id (CLAUDE.md section 2).

Plaintext and reversible encryption are both forbidden. Parameters are the
``argon2-cffi`` defaults tuned down slightly on memory so the gateway stays
responsive on small edge hardware (a Raspberry Pi-class box) while remaining
well above the OWASP minimum of 19 MiB / t=2.
"""

from __future__ import annotations

import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

logger = logging.getLogger(__name__)

#: OWASP-aligned Argon2id parameters: 64 MiB, 3 passes, 4 lanes.
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


#: Reference hash used to equalise login timing for unknown usernames.
_DUMMY_HASH = _HASHER.hash("snap7-gateway-timing-equaliser")


def hash_password(password: str) -> str:
    """Return an Argon2id PHC string for ``password``."""
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time-ish verification that never raises on a bad password.

    A corrupt or foreign hash is treated as a failed verification and logged
    without any part of the credential.
    """
    if not stored_hash:
        return False
    try:
        return bool(_HASHER.verify(stored_hash, password))
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError) as exc:
        logger.warning("stored password hash could not be verified: %s", type(exc).__name__)
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether ``stored_hash`` uses outdated parameters and should be upgraded."""
    try:
        return bool(_HASHER.check_needs_rehash(stored_hash))
    except (InvalidHashError, VerificationError):
        return True


def dummy_verify() -> None:
    """Burn a real hash verification to keep login timing uniform.

    Called when the submitted username does not exist, so an attacker cannot
    tell valid usernames from invalid ones by response time. The reference hash
    is computed once at import time - verifying a syntactically invalid hash
    would return immediately and defeat the whole point.
    """
    try:
        _HASHER.verify(_DUMMY_HASH, "not-the-password")
    except Exception:  # noqa: BLE001 - the point is the elapsed time, not the result
        pass
