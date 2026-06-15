"""Shared password policy for Práctikah mailbox passwords.

Mirrors the frontend lib/passwordPolicy.ts so the rule is identical on both
sides of the BFF. Closes the Phase 17 SC2 carry-item (length-only) and the
pre-CDMX hardening pass (decision 36, 2026-06-15): before ~40 physicians set
their own mailbox passwords, weak passwords must be rejected server-side too —
defense-in-depth, in case anything reaches FastAPI without passing the BFF.

Policy (single definition):
  - At least PASSWORD_MIN_LENGTH (12) characters
  - At least PASSWORD_MIN_CLASSES (3) of the 4 character classes:
    lowercase, uppercase, digit, symbol (any non-alphanumeric)

The password value is NEVER logged or echoed (T-12-03-01) — validators raise
only the generic policy message, never the offending value.
"""

from __future__ import annotations

import re

PASSWORD_MIN_LENGTH = 12
PASSWORD_MIN_CLASSES = 3

_LOWER = re.compile(r"[a-z]")
_UPPER = re.compile(r"[A-Z]")
_DIGIT = re.compile(r"\d")
_SYMBOL = re.compile(r"[^a-zA-Z0-9]")

# Stable, generic messages — safe to surface to the user, never include the value.
TOO_SHORT_MESSAGE = f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
NEEDS_MIX_MESSAGE = (
    "Password must mix at least 3 of: lowercase, uppercase, number, symbol"
)


def count_character_classes(password: str) -> int:
    """Count how many of the 4 character classes appear in the string."""
    count = 0
    if _LOWER.search(password):
        count += 1
    if _UPPER.search(password):
        count += 1
    if _DIGIT.search(password):
        count += 1
    if _SYMBOL.search(password):
        count += 1
    return count


def is_password_valid(password: str) -> bool:
    """True when the password meets length AND character-class requirements."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        return False
    return count_character_classes(password) >= PASSWORD_MIN_CLASSES


def validate_password(password: str) -> str:
    """Pydantic field-validator helper.

    Returns the password unchanged when valid; raises ValueError with a generic
    (value-free) message otherwise. Length is checked before mix so the most
    actionable error surfaces first.
    """
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(TOO_SHORT_MESSAGE)
    if count_character_classes(password) < PASSWORD_MIN_CLASSES:
        raise ValueError(NEEDS_MIX_MESSAGE)
    return password
