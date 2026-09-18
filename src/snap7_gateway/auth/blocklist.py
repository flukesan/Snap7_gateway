"""Common-password blocklist (CLAUDE.md section 3.2).

Two layers of matching, because a 12-character minimum already filters the
classic short passwords and what actually shows up in the field is
``Password2024!`` or ``Siemens1234``:

1. exact match against a curated list, after leet-speak normalisation;
2. base-word match - strip digits, symbols and repeated padding and see whether
   what remains is a well-known base word (``password``, ``siemens``,
   ``qwerty``, the product's own name, ...).

Operators can extend the list with one word per line in
``<data-dir>/password-blocklist.txt``; blank lines and ``#`` comments are
ignored. Keeping the list in-tree matters because factory networks are
air-gapped and cannot reach an online breach API.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

BLOCKLIST_FILENAME = "password-blocklist.txt"

_LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s",
                           "@": "a", "!": "i"})

#: Base words that must not form the substance of a password.
BASE_WORDS: frozenset[str] = frozenset(
    {
        "password", "passwort", "passw0rd", "pass", "secret", "letmein", "welcome",
        "qwerty", "qwertz", "azerty", "asdfgh", "zxcvbn", "123456", "abcdef",
        "admin", "administrator", "root", "operator", "user", "guest", "default",
        "login", "changeme", "temp", "test", "demo", "sample",
        "siemens", "simatic", "step7", "tia", "portal", "profinet", "profibus",
        "snap7", "netlink", "accon", "devicewise", "ptc", "thingworx", "gateway",
        "plc", "scada", "hmi", "automation", "factory", "industrial", "machine",
        "sinumerik", "sinamics", "s7300", "s7400", "s71200", "s71500",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "june", "july", "august",
        "september", "october", "november", "december",
        "dragon", "monkey", "master", "shadow", "sunshine", "princess", "football",
        "baseball", "iloveyou", "trustno", "superman", "batman", "starwars",
        "company", "corporate", "office", "server", "system", "network", "computer",
    }
)

#: Full passwords rejected outright even though they are long enough.
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password1234", "password12345", "passwordpassword", "p@ssw0rd1234",
        "qwertyuiop123", "qwerty123456", "1qaz2wsx3edc", "1q2w3e4r5t6y",
        "123456789012", "1234567890123", "112233445566", "abcd1234abcd",
        "administrator", "administrator1", "admin1234567", "adminadmin12",
        "welcome123456", "welcome1234", "letmein123456", "changeme1234",
        "iloveyou1234", "trustno1234", "monkey123456", "dragon123456",
        "siemens12345", "simatic12345", "automation123", "industrial12",
        "gateway12345", "devicewise123", "snap7gateway", "netlinkpro12",
        "abcdefghijkl", "qwertyasdfgh", "zaq12wsxcde3", "asdfghjkl123",
    }
)

_ALNUM = re.compile(r"[^a-z]+")
_REPEAT = re.compile(r"(.)\1{2,}")


def normalize(password: str) -> str:
    """Lower-case and undo common leet substitutions.

    Note this deliberately turns digits into letters, so it is only ever used
    *alongside* the plain lower-cased form - never instead of it, or a numeric
    password would be reshaped into something that looks alphabetic.
    """
    return password.strip().lower().translate(_LEET_MAP)


def base_words_of(password: str) -> set[str]:
    """Alphabetic cores to test: plain and leet-normalised, repeats collapsed."""
    cores: set[str] = set()
    for variant in (password.strip().lower(), normalize(password)):
        collapsed = _REPEAT.sub(r"\1", variant)
        cores.add(_ALNUM.sub("", collapsed))
    return {c for c in cores if c}


def base_word(password: str) -> str:
    """The plain (non-leet) alphabetic core - kept for callers and tests."""
    return _ALNUM.sub("", _REPEAT.sub(r"\1", password.strip().lower()))


#: Runs that make a long password trivially guessable.
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "qwertzuiop",
    "azertyuiop",
)


def _sequence_reason(password: str) -> str | None:
    """Reject straight keyboard or counting runs of 6+ characters."""
    lowered = password.strip().lower()
    for sequence in _SEQUENCES:
        reverse = sequence[::-1]
        for source in (sequence, reverse):
            for start in range(len(source) - 5):
                run = source[start : start + 6]
                if run in lowered:
                    return (
                        f"'{run}' is a straight keyboard or counting sequence; "
                        "use unrelated characters"
                    )
    return None


class Blocklist:
    """Curated blocklist plus any operator-supplied additions."""

    def __init__(self, extra_words: frozenset[str] | None = None) -> None:
        self.extra = extra_words or frozenset()

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Blocklist":
        """Load ``<data-dir>/password-blocklist.txt`` if it exists."""
        if data_dir is None:
            return cls()
        path = Path(data_dir) / BLOCKLIST_FILENAME
        if not path.exists():
            return cls()
        words: set[str] = set()
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                word = line.strip().lower()
                if word and not word.startswith("#"):
                    words.add(word)
        except OSError as exc:
            logger.warning("could not read %s: %s", path, exc)
            return cls()
        logger.info("loaded %d extra blocked password(s) from %s", len(words), path)
        return cls(frozenset(words))

    def reason(self, password: str) -> str | None:
        """Return why the password is blocked, or ``None`` when it is allowed."""
        lowered = password.strip().lower()
        variants = {lowered, normalize(password)}

        if variants & COMMON_PASSWORDS or variants & self.extra:
            return "This password appears on the blocked-password list"

        distinct = len(set(lowered))
        if distinct <= 3:
            return (
                f"This password only uses {distinct} different character(s); "
                "it is trivially guessable"
            )

        sequence = _sequence_reason(password)
        if sequence:
            return sequence

        cores = base_words_of(password)
        if not any(cores):
            return "Password must contain letters, not only digits and symbols"

        for core in cores:
            if core in BASE_WORDS:
                return (
                    f"'{core}' is a well-known word; adding digits or symbols around it "
                    "does not make it safe"
                )
        for core in cores:
            if len(core) < 4:
                continue
            for word in BASE_WORDS:
                if len(word) >= 5 and word in core and len(core) <= len(word) + 3:
                    return (
                        f"This password is essentially the common word '{word}'; "
                        "choose something unrelated"
                    )
        return None
