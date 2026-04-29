"""Slug generation utility mirroring medikah-chat-frontend/lib/slug.ts:nameToSlug.

Normalizes a physician full name to a URL-safe slug:
  - Strips Dr./Dra. honorific prefix
  - Strips diacritics (NFD decompose, drop combining marks)
  - Lowercases
  - Replaces runs of non-alphanumeric chars with hyphens
  - Trims leading/trailing hyphens

Examples:
  name_to_slug('Dr. Hector López')   → 'hector-lopez'
  name_to_slug('Dra. Ana Núñez')     → 'ana-nunez'
  name_to_slug('José García Mendez') → 'jose-garcia-mendez'
"""

from __future__ import annotations

import re
import unicodedata


_HONORIFIC_RE = re.compile(r'^(dr\.?\s+|dra\.?\s+)', re.IGNORECASE)
_NON_ALPHANUM_RE = re.compile(r'[^a-z0-9]+')


def name_to_slug(name: str) -> str:
    """Return a URL-safe slug derived from a physician's full name.

    Mirrors lib/slug.ts:nameToSlug() — both must produce identical output for
    the same input so that Next.js routing and FastAPI agree on slugs.

    Args:
        name: A physician's full name, optionally prefixed with Dr./Dra.

    Returns:
        A lowercase hyphenated slug (e.g. 'dr-lopez', 'jose-garcia').
        Returns an empty string if the input is empty.
    """
    if not name:
        return ''
    # Strip honorific prefix
    without_prefix = _HONORIFIC_RE.sub('', name).strip()
    # NFD decompose → drop combining marks (diacritics)
    normalized = unicodedata.normalize('NFD', without_prefix)
    ascii_only = ''.join(ch for ch in normalized if not unicodedata.combining(ch))
    # Lowercase + replace non-alphanum runs with hyphens
    slug = _NON_ALPHANUM_RE.sub('-', ascii_only.lower())
    # Trim leading/trailing hyphens
    return slug.strip('-')
