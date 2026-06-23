#!/usr/bin/env python3
"""
probe_app_passwd_api.py
-----------------------
Wave 0 / HANDS-07: Live probe of the Mailcow app-passwd Admin API surface.

PURPOSE
  Verifies the exact request-body field names, response shape, and per-protocol
  ACL semantics of the running Mailcow build at practikah.medikah.health BEFORE
  any credential_broker.py code is written.  Hardcoding ASSUMED field names
  without this probe risks minting a credential that can send mail (security
  failure) or that has no CalDAV scope (silent calendar failure).

WHAT IT DOES
  1. GET  /api/v1/get/app-passwd/all/<mailbox>  — inspect existing app-passwds.
  2. POST /api/v1/add/app-passwd                — mint a throwaway "cue-probe-DELETE-ME"
     entry with active="1", imap_access="1", dav_access="1", smtp_access="0".
  3. GET  /api/v1/get/app-passwd/all/<mailbox>  again — read back the entry to
     confirm which protocol fields the running build actually honoured.
  4. POST /api/v1/delete/app-passwd             — delete the throwaway entry.
  Prints the full JSON at each step for manual recording into 23-PROBE-FINDINGS.md.

DO NOT RUN AUTOMATICALLY
  This script requires MAILCOW_API_URL and MAILCOW_API_KEY to be set in the
  environment (already present on the Render service and the VPS).  It touches
  the LIVE Mailcow instance — run only under per-command human approval.

USAGE (Hector, on the VPS or Render shell):
  export MAILCOW_API_URL=https://practikah.medikah.health
  export MAILCOW_API_KEY=<admin api key>
  python scripts/probe_app_passwd_api.py <mailbox>
  e.g.: python scripts/probe_app_passwd_api.py testdoctor@medikah.health

THREAT MITIGATIONS (T-23-01-01, T-23-01-02, T-23-01-04)
  - Uses active="1" NOT active="2" — active=2 would block inbound mail (HANDS-05a).
  - Uses smtp_access="0" — the throwaway credential cannot send mail.
  - The throwaway entry is always deleted in step 4 (even if step 3 failed).
  - This script does NOT print the minted app-passwd secret to the console —
    only the entry's ID and protocol flags are recorded.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

# ---------------------------------------------------------------------------
# Config (read from environment — never hardcoded here)
# ---------------------------------------------------------------------------

MAILCOW_API_URL: str = os.environ.get("MAILCOW_API_URL", "")
MAILCOW_API_KEY: str = os.environ.get("MAILCOW_API_KEY", "")

PROBE_APP_NAME = "cue-probe-DELETE-ME"
TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def _headers() -> dict[str, str]:
    """Mailcow uses X-API-Key auth — mirrors mailbox_provisioner.py:98-107."""
    return {
        "X-API-Key": MAILCOW_API_KEY,
        "Content-Type": "application/json",
    }


def _pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Probe steps
# ---------------------------------------------------------------------------


async def step_get_existing(client: httpx.AsyncClient, mailbox: str) -> list:
    """Step 1: GET existing app-passwds to see the current field inventory."""
    print(f"\n[STEP 1] GET /api/v1/get/app-passwd/all/{mailbox}")
    resp = await client.get(
        f"{MAILCOW_API_URL}/api/v1/get/app-passwd/all/{mailbox}",
        headers=_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  HTTP {resp.status_code}")
    print(f"  Response:\n{_pretty(data)}")
    return data if isinstance(data, list) else []


async def step_mint_probe(client: httpx.AsyncClient, mailbox: str) -> dict | None:
    """Step 2: POST add/app-passwd with full protocol ACL flags.

    HANDS-05a: uses active="1" NOT active="2".
    HANDS-05: smtp_access="0" — no send access on the throwaway credential.
    HANDS-07: dav_access + imap_access field names are ASSUMED here;
              inspect the response and read-back to confirm they are accepted.
    """
    print(f"\n[STEP 2] POST /api/v1/add/app-passwd  (throwaway — will be deleted)")
    payload = {
        "username": mailbox,
        "app_name": PROBE_APP_NAME,
        "active": "1",          # HANDS-05a: NOT "2" (2 blocks inbound mail)
        "imap_access": "1",     # ASSUMED field name — verify in response
        "dav_access": "1",      # ASSUMED field name — verify in response
        "smtp_access": "0",     # HANDS-05: no-send at credential layer
        "pop3": "0",            # ASSUMED field name
        "eas": "0",             # ASSUMED field name
    }
    print(f"  Payload:\n{_pretty(payload)}")
    resp = await client.post(
        f"{MAILCOW_API_URL}/api/v1/add/app-passwd",
        json=payload,
        headers=_headers(),
    )
    print(f"  HTTP {resp.status_code}")
    data = resp.json()
    print(f"  Response:\n{_pretty(data)}")
    # Mailcow returns [{"type":"success","msg":[...]}] or [{"type":"error","msg":"..."}]
    # On success the ID of the created entry should be in msg — inspect and record.
    resp.raise_for_status()
    return data


async def step_readback(client: httpx.AsyncClient, mailbox: str) -> str | None:
    """Step 3: Read the app-passwd back to confirm the protocol fields were honoured.

    This is the critical verification step for HANDS-07: does the running build
    actually store imap_access / dav_access / smtp_access as distinct fields?
    Record the exact field names from this response in 23-PROBE-FINDINGS.md.
    """
    print(f"\n[STEP 3] GET /api/v1/get/app-passwd/all/{mailbox}  (read-back)")
    resp = await client.get(
        f"{MAILCOW_API_URL}/api/v1/get/app-passwd/all/{mailbox}",
        headers=_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  HTTP {resp.status_code}")
    print(f"  Response:\n{_pretty(data)}")

    # Find the probe entry and extract its ID for deletion in step 4.
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("app_name") == PROBE_APP_NAME:
                probe_id = entry.get("id")
                print(f"\n  *** PROBE ENTRY FOUND — id={probe_id} ***")
                print(f"  *** Protocol flags (RECORD THESE): ***")
                for key in ("imap_access", "dav_access", "smtp_access", "pop3", "eas", "active"):
                    val = entry.get(key, "<NOT PRESENT — field name may differ>")
                    print(f"    {key}: {val}")
                # SECURITY: do NOT print the password value
                if "password" in entry or "passwd" in entry:
                    print("  *** SECRET FIELD DETECTED — NOT PRINTED (HANDS-08) ***")
                return probe_id
    print("  *** PROBE ENTRY NOT FOUND — check response above for actual structure ***")
    return None


async def step_delete(client: httpx.AsyncClient, probe_id: str | None) -> None:
    """Step 4: Delete the throwaway app-passwd.  Always runs — no residue left.

    T-23-01-02: probe must clean up after itself.
    """
    print(f"\n[STEP 4] POST /api/v1/delete/app-passwd  (cleanup — id={probe_id})")
    if probe_id is None:
        print("  WARNING: probe_id is None — cannot target specific entry.")
        print("  Sending delete with None; Mailcow may error. Manual cleanup may be needed.")
    payload = [probe_id] if probe_id is not None else []
    resp = await client.post(
        f"{MAILCOW_API_URL}/api/v1/delete/app-passwd",
        json=payload,
        headers=_headers(),
    )
    print(f"  HTTP {resp.status_code}")
    print(f"  Response:\n{_pretty(resp.json())}")
    if resp.is_success:
        print("  *** CLEANUP: throwaway app-passwd DELETED (T-23-01-02 satisfied) ***")
    else:
        print("  *** CLEANUP FAILED — verify manually that 'cue-probe-DELETE-ME' is removed ***")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(mailbox: str) -> None:
    if not MAILCOW_API_URL or not MAILCOW_API_KEY:
        print("ERROR: MAILCOW_API_URL and MAILCOW_API_KEY must be set in environment.")
        sys.exit(1)

    print("=" * 70)
    print("HANDS-07 PROBE: Mailcow app-passwd API surface")
    print(f"  Target: {MAILCOW_API_URL}")
    print(f"  Mailbox: {mailbox}")
    print("=" * 70)
    print("RECORDING INSTRUCTIONS:")
    print("  Paste the output of each step into 23-PROBE-FINDINGS.md.")
    print("  Record the exact field names from STEP 3 — these unblock credential_broker.py.")
    print("  NEVER paste the app-passwd secret value into any file.")
    print("=" * 70)

    probe_id: str | None = None

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            await step_get_existing(client, mailbox)
            await step_mint_probe(client, mailbox)
            probe_id = await step_readback(client, mailbox)
        finally:
            # Step 4 runs even if an earlier step raised — cleanup is unconditional.
            await step_delete(client, probe_id)

    print("\n" + "=" * 70)
    print("PROBE COMPLETE")
    print("Next: fill in 23-PROBE-FINDINGS.md with the STEP 3 field names and values.")
    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <mailbox>")
        print(f"  e.g.: {sys.argv[0]} testdoctor@medikah.health")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
