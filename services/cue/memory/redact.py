"""
services/cue/memory/redact.py
-----------------------------
PATCH-01 — free-text redaction for memory notes.

BeNeXT's strip-pii.ts only removes object keys literally named email/phone; it
NEVER scans string values. The memory judge writes a free-text sentence — exactly
where contact details, IDs, and patient names would appear. This module scrubs the
note BODY before persist. It is the load-bearing no-BAA Mexico control (LFPDPPP).

Pure function, no I/O. Conservative: replaces clear PII patterns with neutral
bilingual placeholders; leaves benign operational text untouched.
"""
from __future__ import annotations

import re

# Order matters: email before phone before digit-run, so an email's digits are
# not first eaten by the digit-run rule.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Phone: either an international form with a leading '+', OR a grouped form that
# CONTAINS at least one separator (space/-/.). A bare run of digits with NO
# separator is NOT a phone — it falls through to the digit-run rule below ([id]),
# which keeps "0123456789" (an MRN-like id) distinct from "+52 55 1234 5678".
_PHONE = re.compile(
    r"\+\d[\d\s.\-]{6,}\d"            # international, leading +
    r"|"
    r"\d{2,4}[\s.\-]\d[\d\s.\-]{4,}\d"  # grouped, at least one internal separator
)
# Long bare digit run (MRN / record / national-id-like): 6+ consecutive digits.
_DIGIT_RUN = re.compile(r"\b\d{6,}\b")
# Honorific-prefixed proper name: Sr./Sra./Srta./Don/Doña + one or two Capitalized words.
# (Dr./Dra. deliberately NOT here — "the doctor" is the note subject, not a patient.)
_HONORIFIC_NAME = re.compile(
    r"\b(?:Sr\.?|Sra\.?|Srta\.?|Don|Do(?:n|ña)|Mr\.?|Mrs\.?|Ms\.?)\s+"
    r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)?"
)


def redact_free_text(text: str | None) -> str:
    """Replace clear PII in a free-text note body with neutral placeholders.

    Fail-closed contract: the caller must treat any raised exception as
    "do not store this note". This function does not raise for str/None input.
    """
    if not text:
        return ""
    out = _EMAIL.sub("[correo]", text)
    out = _HONORIFIC_NAME.sub("[paciente]", out)
    out = _PHONE.sub("[teléfono]", out)
    out = _DIGIT_RUN.sub("[id]", out)
    return out
