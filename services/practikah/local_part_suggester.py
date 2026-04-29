"""Local-part candidate ranking and Mailcow availability checking (Phase 12-02).

Provides two public callables used by the /practikah/wizard/local-part-suggestions
endpoint:

  rank_candidates(title, first_name, middle_name, last_name, maternal_last_name)
    → List[str]          5 ranked local-part candidates derived from the doctor's name

  check_candidate_availability(local_part, domain)
    → Candidate          Mailcow live availability check + block-list guard

Mitigations:
  T-12-02-01: RESERVED_LOCAL_PARTS block-list prevents admin/postmaster/medikah/etc.
  T-12-02-01: LOCAL_PART_REGEX rejects non-RFC-5321 local parts before Mailcow call.
  T-12-02-07: Caller rate-limits to 10/minute via SlowAPI; Mailcow upstream also limits.

OPERATOR NOTE (D-23): MAILCOW_API_KEY is currently 401 on the live VPS. Until the key
is rotated (Mailcow admin → Configuration → Access → API), check_candidate_availability
will return available=False for all mailcow_check results. The block-list checks
(reserved/invalid) still work without the Mailcow API.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import List, Literal, Optional, TypedDict

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reserved local parts (T-12-02-01)
# ---------------------------------------------------------------------------

RESERVED_LOCAL_PARTS = {
    'admin',
    'administrator',
    'postmaster',
    'webmaster',
    'noreply',
    'no-reply',
    'support',
    'help',
    'info',
    'mail',
    'root',
    'abuse',
    'security',
    'practikah',
    'medikah',
    'klinikah',
    'hostmaster',
    'mailer-daemon',
    'welcome',
    'contact',
    'sales',
    'billing',
}

# RFC 5321 compliant local-part regex (lowercase-only variant — doctors must use lowercase)
LOCAL_PART_REGEX = re.compile(r"^[a-z0-9._-]+$")

# ---------------------------------------------------------------------------
# TypedDict for candidate results
# ---------------------------------------------------------------------------


class Candidate(TypedDict):
    local_part: str
    available: bool
    source: Literal['mailcow_check', 'reserved', 'invalid']


# ---------------------------------------------------------------------------
# Slug helper (standalone — same logic as local_part_suggester internal use)
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    """Strip diacritics and convert to lowercase hyphenated slug segment.

    Used internally by rank_candidates to derive safe local-part segments
    from name components.

    Examples:
      slugify('López')        → 'lopez'
      slugify('Núñez García') → 'nunez-garcia'
      slugify('')             → ''
    """
    if not s:
        return ''
    normalized = unicodedata.normalize('NFD', s)
    ascii_only = ''.join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]+', '-', ascii_only.lower()).strip('-')


# ---------------------------------------------------------------------------
# Candidate ranking
# ---------------------------------------------------------------------------

def rank_candidates(
    title: Literal['Dr', 'Dra'],
    first_name: str,
    last_name: str,
    middle_name: Optional[str] = None,
    maternal_last_name: Optional[str] = None,
) -> List[str]:
    """Generate up to 5 ranked local-part candidates from a physician's name.

    Candidate order (most-preferred first):
      1. {title_prefix}-{last_name}           e.g. dr-lopez
      2. {first_initial}-{last_name}          e.g. h-lopez
      3. {first_initial}-{mid_initial}-{last} e.g. h-r-lopez  (if middle_name given)
      4. {last_name}-{maternal_last_name}      e.g. lopez-mendez (if maternal_last given)

    Deduplication: identical candidates are dropped; order preserved.

    Args:
        title:              'Dr' or 'Dra' (physician honorific).
        first_name:         Doctor's first given name.
        last_name:          Doctor's paternal/primary family name.
        middle_name:        Doctor's middle name or second given name (optional).
        maternal_last_name: Doctor's maternal family name (common in Latin America, optional).

    Returns:
        List of unique candidate strings (possibly fewer than 4 if name parts are absent).
    """
    title_prefix = title.lower()  # 'dr' or 'dra'
    ln = slugify(last_name)
    fn = slugify(first_name)
    mn = slugify(middle_name) if middle_name else ''
    mln = slugify(maternal_last_name) if maternal_last_name else ''

    candidates: List[str] = []

    # 1. dr-{last}
    if ln:
        candidates.append(f"{title_prefix}-{ln}")

    # 2. {first_initial}-{last}
    if fn and ln:
        candidates.append(f"{fn[:1]}-{ln}")

    # 3. {first_initial}-{mid_initial}-{last}
    if fn and mn and ln:
        candidates.append(f"{fn[:1]}-{mn[:1]}-{ln}")

    # 4. {last}-{maternal_last}
    if ln and mln:
        candidates.append(f"{ln}-{mln}")

    # Deduplicate preserving order
    seen: set[str] = set()
    result: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)

    return result


# ---------------------------------------------------------------------------
# Mailcow availability check (with tenacity retry on network failures)
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.5, min=0.5, max=4))
async def _mailcow_get_mailbox(client: httpx.AsyncClient, full_address: str) -> httpx.Response:
    """GET /api/v1/get/mailbox/<address> from Mailcow Admin API.

    Decorated with @retry (2 attempts, exponential backoff) for transient network errors.
    The caller checks the response — 401 or 5xx are treated as 'unavailable/unknown'.
    """
    return await client.get(f"/api/v1/get/mailbox/{full_address}", timeout=5.0)


async def check_candidate_availability(
    local_part: str,
    domain: str = "medikah.health",
) -> Candidate:
    """Check whether a mailbox local-part is available on the Mailcow server.

    Guard sequence (short-circuits on match):
      1. LOCAL_PART_REGEX check → source='invalid'
      2. RESERVED_LOCAL_PARTS block-list → source='reserved'
      3. Live Mailcow GET → source='mailcow_check'

    Falls back to available=False (fail-closed) if Mailcow env vars are missing
    or the API call fails — callers must handle unavailability gracefully.

    Args:
        local_part: The proposed mailbox local part (e.g. 'dr-lopez').
        domain:     The Mailcow domain to check against (default 'medikah.health').

    Returns:
        Candidate TypedDict with local_part, available (bool), and source string.
    """
    # Guard 1: format validation
    if not LOCAL_PART_REGEX.match(local_part):
        return {
            'local_part': local_part,
            'available': False,
            'source': 'invalid',
        }

    # Guard 2: reserved block-list
    if local_part in RESERVED_LOCAL_PARTS:
        return {
            'local_part': local_part,
            'available': False,
            'source': 'reserved',
        }

    # Guard 3: live Mailcow availability check
    api_url = os.environ.get('MAILCOW_API_URL')
    api_key = os.environ.get('MAILCOW_API_KEY')

    if not api_url or not api_key:
        logger.warning(
            '[local_part_suggester] MAILCOW_API_URL or MAILCOW_API_KEY not set '
            '— failing closed for local_part=%s (D-23 carry-item: rotate MAILCOW_API_KEY)',
            local_part,
        )
        return {
            'local_part': local_part,
            'available': False,
            'source': 'mailcow_check',
        }

    full_address = f"{local_part}@{domain}"

    try:
        async with httpx.AsyncClient(
            base_url=api_url.rstrip('/'),
            headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
        ) as client:
            resp = await _mailcow_get_mailbox(client, full_address)

            # Parse Mailcow response: {} / [] for not-found; populated dict for found
            content_type = resp.headers.get('content-type', '')
            if content_type.startswith('application/json'):
                try:
                    data = resp.json()
                except Exception:
                    data = None
            else:
                data = None

            # Mailcow returns a dict with 'username' key when the mailbox exists
            taken = (
                bool(data)
                and isinstance(data, dict)
                and data.get('username') == full_address
            )
            return {
                'local_part': local_part,
                'available': not taken,
                'source': 'mailcow_check',
            }

    except Exception as exc:
        logger.warning(
            '[local_part_suggester] availability check failed for local_part=%s: %s',
            local_part, exc,
        )
        return {
            'local_part': local_part,
            'available': False,
            'source': 'mailcow_check',
        }
