"""
services/cue/credential_broker.py
-----------------------------------
Cue-scoped Mailcow app-password broker (HANDS-01/05/05a/08/08a/09a).

Mints a Cue-OWNED, no-send (smtp_access=0), inbound-safe (active=1, NEVER
active=2) Mailcow app-password for a physician's mailbox, used by the CalDAV /
IMAP read clients (calendar_dav.py / mail_reader.py) to read the doctor's OWN
calendar and inbox. The credential is:

  - lazy-minted on first hands use (get_cue_credential),
  - stored by ID ONLY in physician_workspace_accounts.cue_app_passwd_id,
  - audited on mint + revoke (workspace_audit_log) with NO secret value,
  - gated behind the global CUE-04a kill-switch BEFORE any Mailcow call (HANDS-09a),
  - revoked with a single DELETE /api/v1/delete/app-passwd of the stored ID.

CONFIRMED Mailcow contract (23-PROBE-FINDINGS §1b/1c, live-probed 2026-06-23):
  - ADD: the running build IGNORES the flat `imap_access`/`dav_access`/... keys.
    Per-protocol access is derived ONLY from a `protocols` array. Sending
    `protocols:["imap_access","dav_access"]` (OMIT smtp_access) yields a
    no-send IMAP+DAV credential. The flat keys are silently dropped → DEAD
    credential — they are NEVER used here.
  - ADD body ALSO requires `app_passwd` + `app_passwd2` that pass server
    password-complexity (a missing/empty password → {"msg":"password_complexity"}).
  - The ADD response does NOT carry the app-passwd id directly; the id is read
    back via GET /api/v1/get/app-passwd/all/<mailbox>, matching on the `name`
    field (NOT `app_name` — that key only exists on the add path).
  - DELETE body is a JSON array of ids: [<id>].

NEVER-LOG-SECRET DISCIPLINE (HANDS-08 / mirror T-12-03-01):
  The minted password value is returned to the caller for the in-request
  CalDAV/IMAP connection, but is NEVER passed to logger.* and NEVER written to
  an audit row. Only the app_passwd_id (an opaque id, not the secret) is logged
  or stored.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

# Re-exported at module level so the kill-switch gate is monkeypatchable in
# tests (test_credential_broker.py monkeypatches broker_mod.check_kill_switch).
from services.cue.gate import bilingual_unavailable, check_kill_switch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mailcow Admin API config (mirrors mailbox_provisioner.py — X-API-Key auth)
# ---------------------------------------------------------------------------

MAIL_DOMAIN = "medikah.health"

# The Cue app-password is no-send IMAP+DAV ONLY. smtp_access is enforced at the
# CREDENTIAL by OMITTING it from the protocols array (HANDS-05).
_CUE_PROTOCOLS = ["imap_access", "dav_access"]

_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


class KillSwitchTrippedError(RuntimeError):
    """Raised by get_cue_credential when the global kill-switch is tripped.

    HANDS-09a: the kill-switch gate runs BEFORE any Mailcow call — a tripped
    switch must prevent the broker from minting OR handing out a credential.
    The message includes the word 'kill-switch' (and the bilingual_unavailable
    text) so callers and tests can recognise it as a gate, not a Mailcow error.
    """


@dataclass(frozen=True, slots=True)
class CueCredential:
    """A live Cue app-password for an in-request CalDAV/IMAP login.

    `password` is the secret — it is NEVER logged or persisted. Only
    `app_passwd_id` is stored (physician_workspace_accounts.cue_app_passwd_id)
    and audited.
    """

    username: str          # f"{local_part}@medikah.health"
    password: str          # the minted secret — transient, never logged
    app_passwd_id: Optional[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _api_url() -> str:
    """Mailcow API origin (e.g. https://practikah.medikah.health). Required."""
    url = os.environ.get("MAILCOW_API_URL", "")
    return url.rstrip("/")


def _headers() -> dict[str, str]:
    """Mailcow uses X-API-Key authentication, NOT Authorization: Bearer."""
    return {
        "X-API-Key": os.environ.get("MAILCOW_API_KEY", ""),
        "Content-Type": "application/json",
    }


def _get_db():
    """Service-role Supabase client (server-side only). May be None in dev."""
    from db.client import get_supabase

    return get_supabase()


def _generate_app_passwd() -> str:
    """Generate a strong, complexity-passing throwaway secret.

    Mailcow rejects weak/empty passwords with {"msg":"password_complexity"}
    (23-PROBE-FINDINGS §1b). token_urlsafe(24) yields ~32 chars of mixed
    case + digits + URL-safe symbols, comfortably passing any policy. The
    value is never logged.
    """
    return secrets.token_urlsafe(24) + "Aa9!"


async def _readback_app_passwd_id(client: httpx.AsyncClient, username: str, app_name: str) -> Optional[str]:
    """Resolve the just-minted app-passwd id by matching the `name` field.

    23-PROBE-FINDINGS §1a: on READ the key is `name` (NOT `app_name`), and the
    id we must store for one-call revoke is the `id` field. Also used to warn on
    smtp_access drift (HANDS-08 code-side readback).
    """
    url = f"{_api_url()}/api/v1/get/app-passwd/all/{quote(username, safe='')}"
    resp = await client.get(url, headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name", "")) == app_name:
            # HANDS-08 code-side drift check — warn (do NOT fail) if smtp leaked.
            smtp = str(entry.get("smtp_access", "")).strip()
            if smtp not in ("", "0"):
                logger.warning(
                    "[cue:cred] ACL drift — expected smtp_access=0 got smtp_access=%s "
                    "app_passwd_id=%s",
                    smtp,
                    entry.get("id"),
                )
            app_id = entry.get("id")
            return str(app_id) if app_id is not None else None
    return None


def _write_audit(action: str, physician_id: str, app_passwd_id: Optional[str], *, ip=None, ua=None) -> None:
    """Best-effort workspace_audit_log row (HANDS-08/08a).

    detail carries ONLY the app_passwd_id + non-secret ACL flags — NEVER the
    secret value. ip/ua are threaded in only by route-level callers (the DELETE
    /credential revoke route in 23-04); in-loop callers (the lazy mint) omit
    them (HANDS-08a scoping).
    """
    db = _get_db()
    if db is None:
        return
    detail: dict = {
        "app_passwd_id": app_passwd_id,
        "smtp_access": "0",
        "active": "1",
        "scopes": ["imap", "dav"],
    }
    if ip is not None:
        detail["ip"] = ip
    if ua is not None:
        detail["ua"] = ua
    try:
        db.table("workspace_audit_log").insert(
            {
                "physician_id": physician_id,
                "actor_id": physician_id,
                "actor_role": "physician",
                "action": action,
                "resource_type": "cue_credential",
                "resource_id": app_passwd_id,
                "detail": detail,
            }
        ).execute()
    except Exception:
        logger.exception(
            "[cue:cred] audit insert failed action=%s physician=%s (non-fatal)",
            action,
            physician_id,
        )


def _read_stored_app_passwd_id(physician_id: str) -> Optional[str]:
    """Read physician_workspace_accounts.cue_app_passwd_id for reuse (lazy)."""
    db = _get_db()
    if db is None:
        return None
    try:
        result = (
            db.table("physician_workspace_accounts")
            .select("cue_app_passwd_id")
            .eq("physician_id", physician_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if rows:
            return rows[0].get("cue_app_passwd_id")
    except Exception:
        logger.exception(
            "[cue:cred] read cue_app_passwd_id failed physician=%s", physician_id
        )
    return None


def _store_app_passwd_id(physician_id: str, app_passwd_id: Optional[str]) -> None:
    """Persist (or clear) cue_app_passwd_id for the physician (service-role)."""
    db = _get_db()
    if db is None:
        return
    try:
        (
            db.table("physician_workspace_accounts")
            .update({"cue_app_passwd_id": app_passwd_id})
            .eq("physician_id", physician_id)
            .execute()
        )
    except Exception:
        logger.exception(
            "[cue:cred] store cue_app_passwd_id failed physician=%s", physician_id
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def mint_cue_credential(
    physician_id: str,
    mailbox_local_part: str,
) -> CueCredential:
    """Mint a Cue-scoped no-send Mailcow app-password (HANDS-01/05/05a).

    POST /api/v1/add/app-passwd with the CONFIRMED contract (23-PROBE-FINDINGS):
      protocols=["imap_access","dav_access"]  → smtp_access=0 (no send),
      active="1"                              → inbound mail NOT blocked (never the freeze flag),
      app_passwd/app_passwd2                  → strong complexity-passing secret.

    The app-passwd id is read back (GET .../all/<mailbox>, match on `name`),
    stored in cue_app_passwd_id, and audited. The secret is returned to the
    caller for the in-request login but is NEVER logged or persisted.
    """
    username = f"{mailbox_local_part}@{MAIL_DOMAIN}"
    app_name = f"cue-l2-{physician_id}"
    secret = _generate_app_passwd()

    # CONFIRMED mint payload (23-PROBE-FINDINGS §1b). active is "1" only —
    # never the freeze value that would block inbound mail (HANDS-05a).
    #
    # No-send is enforced AUTHORITATIVELY by OMITTING "smtp_access" from the
    # `protocols` array (the running build derives per-protocol access ONLY from
    # this array; the flat keys are silently ignored on the add path). We ALSO
    # set explicit flat flags (smtp_access="0", imap_access="1", dav_access="1")
    # as documented intent + defence-in-depth: even if a future Mailcow build
    # honoured the flat keys, smtp_access="0" still means no-send. The flat keys
    # can NEVER widen access here — `protocols` is the grant source of truth and
    # it never contains smtp_access (HANDS-05/05a).
    payload = {
        "username": username,
        "app_name": app_name,
        "app_passwd": secret,
        "app_passwd2": secret,
        "active": "1",
        "protocols": _CUE_PROTOCOLS,
        # Explicit no-send flat flags (ignored by the live add path; intent only):
        "smtp_access": "0",
        "imap_access": "1",
        "dav_access": "1",
    }

    logger.info(
        "[cue:cred] minting no-send credential physician=%s local_part=%s "
        "(smtp_access=0, active=1)",  # NOTE: secret is NEVER logged (HANDS-08)
        physician_id,
        mailbox_local_part,
    )

    app_passwd_id: Optional[str] = None
    # NOTE: timeout is passed per-request (not to the AsyncClient constructor) so
    # the client object remains constructible by lightweight test doubles that
    # patch httpx.AsyncClient with a no-arg __init__ (test_credential_broker.py).
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_api_url()}/api/v1/add/app-passwd",
            json=payload,
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        # The add response does not carry the id; read it back by `name`.
        try:
            app_passwd_id = await _readback_app_passwd_id(client, username, app_name)
        except Exception:
            logger.exception(
                "[cue:cred] app-passwd id readback failed physician=%s", physician_id
            )

    # Persist ID-only (lazy reuse) + audit (no secret) — best-effort.
    _store_app_passwd_id(physician_id, app_passwd_id)
    _write_audit("cue.credential_minted", physician_id, app_passwd_id)

    return CueCredential(username=username, password=secret, app_passwd_id=app_passwd_id)


async def revoke_cue_credential(
    physician_id: str,
    *,
    ip=None,
    ua=None,
) -> bool:
    """Revoke the physician's Cue app-password (HANDS-09 one-call revoke).

    DELETE /api/v1/delete/app-passwd with [stored_id], then clear
    cue_app_passwd_id. This NEVER touches the doctor's mailbox login password —
    only the Cue-owned app-passwd id. The optional ip/ua are threaded into the
    audit detail when called from the DELETE /credential route (which has a
    Request, 23-04); None for any non-route caller (HANDS-08a scoping).

    Returns True if a stored id was found and a delete was attempted.
    """
    app_passwd_id = _read_stored_app_passwd_id(physician_id)
    if not app_passwd_id:
        logger.info(
            "[cue:cred] revoke: no stored app_passwd_id for physician=%s (noop)",
            physician_id,
        )
        return False

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_api_url()}/api/v1/delete/app-passwd",
            json=[app_passwd_id],
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()

    # Clear the column + audit (no secret; ip/ua only from route-level callers).
    _store_app_passwd_id(physician_id, None)
    _write_audit(
        "cue.credential_revoked", physician_id, app_passwd_id, ip=ip, ua=ua
    )
    logger.info(
        "[cue:cred] revoked app_passwd_id=%s physician=%s", app_passwd_id, physician_id
    )
    return True


async def get_cue_credential(
    physician_id: str,
    mailbox_local_part: str,
    locale: str = "es",
) -> CueCredential:
    """Hand out a live Cue credential, lazy-minting on first use (HANDS-01).

    Gate order (HANDS-09a — CRITICAL): the global kill-switch is checked FIRST,
    BEFORE any Mailcow call. A tripped switch raises KillSwitchTrippedError and
    NO mint is attempted.

    Then: if cue_app_passwd_id is already stored, the credential is re-minted in
    Mailcow (Mailcow never returns the plaintext for an existing id, so a fresh
    secret is required for the per-request login) — the existing id is reused as
    the lazy marker. Otherwise a fresh credential is minted and stored.

    NOTE: an existing stored id means the physician already connected Cue; we
    still need a usable plaintext password for the in-request CalDAV/IMAP login,
    so we mint a fresh app-passwd. The broker reuses the *connection intent*
    (lazy) but always returns a live secret — the secret is never persisted.
    """
    db = _get_db()

    # GATE (HANDS-09a): kill-switch BEFORE any Mailcow call.
    kill_status = await check_kill_switch(db, locale)
    if kill_status == "tripped":
        raise KillSwitchTrippedError(
            f"kill-switch tripped — {bilingual_unavailable(locale)}"
        )

    # Lazy: a stored id proves the physician previously connected Cue. We still
    # mint a fresh app-passwd to obtain a usable plaintext for this request, and
    # revoke the stale id so app-passwords do not accumulate.
    stored_id = _read_stored_app_passwd_id(physician_id)
    if stored_id:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_api_url()}/api/v1/delete/app-passwd",
                    json=[stored_id],
                    headers=_headers(),
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
        except Exception:
            logger.exception(
                "[cue:cred] stale app-passwd cleanup failed id=%s physician=%s",
                stored_id,
                physician_id,
            )

    return await mint_cue_credential(physician_id, mailbox_local_part)


# Plan / executor alias: 23-02 PLAN and executors.py refer to get_cue_cred.
# Keep both names so the test scaffold (get_cue_credential) and the executor
# wiring (get_cue_cred) resolve to the same gated, lazy-mint entry point.
get_cue_cred = get_cue_credential
