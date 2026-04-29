"""End-to-end Práctikah workspace provisioning dry-run.

Usage:
    python -m scripts.provision_test_doctor \\
        --physician-id <uuid> \\
        --domain drsmith-test.com \\
        --tld-strategy mocked \\
        --rollback-on-success true

Flags:
    --physician-id <uuid>          (required) UUID of a verified physician in Supabase
    --domain <fqdn>                (required) Test domain (orchestrator prefixes with
                                   'sandbox-' in sandbox mode per D-19)
    --tld-strategy {real,mocked}   (default: mocked) 'mocked' skips registrar API;
                                   'real' burns ~$10 and requires CF Registrar beta
                                   access + Phase 10 D-17 carry-items resolved
    --rollback-on-success {true,false}
                                   (default: false) If 'true', invokes run_rollback
                                   after a successful provisioning run — verifies
                                   both forward and rollback paths in one CLI call
    --resume                       If orphan runs are detected for this physician,
                                   resume rollback rather than exiting 4
    --mailbox-local-part <str>     (default: dr-test) Mailbox local part
    --registrant-name <str>        (default: Dr Práctikah Test)
    --registrant-email <str>       (default: test@medikah.health)
    --registrant-country <str>     (default: MX)

Sandbox safety:
    This script ALWAYS forces MEDIKAH_PROVISIONING_SANDBOX=true regardless of the
    current environment state. This is a defense-in-depth guarantee — the CLI
    cannot accidentally hit production registrars. Sandbox mode uses real APIs
    against the live VPS but scopes all resources with 'sandbox-' prefixes so they
    can be swept without touching production data (Phase 11 D-19).

    To run a real-strategy staging dry-run that exercises the full registrar flow,
    you must ALSO complete the Phase 10 D-17 carry-items:
      1. MAILCOW_API_KEY rotation (currently 401 — see STATE.md pending todos)
      2. mcdkim DKIM key generation in Mailcow + DNS publish
      3. Mailcow admin 2FA re-enable (disabled during 2026-04-26 recovery)
    Without these, '--tld-strategy real' will fail loudly at the Mailcow step.

Exit codes:
    0  — provisioning succeeded; if --rollback-on-success true, rollback also succeeded
    1  — provisioning failed; rollback completed cleanly
    2  — rollback failed (orphan run still present after rollback — operator intervention)
    3  — time-budget overrun (provisioning exceeded 180s — ROADMAP criterion 5 violation)
    4  — orphan run detected without --resume flag (pass --resume to clean up first)
    99 — unhandled exception
    130 — interrupted (KeyboardInterrupt)

ROADMAP success criterion 5: provision_test_doctor.py runs < 3 minutes OR rolls back.
The 180s budget is enforced in code; the rollback verification is enforced via
list_orphan_runs post-check after --rollback-on-success true.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUCCESS_BUDGET_SECONDS = 180  # ROADMAP success criterion 5: < 3 min
DEFAULT_LOCAL_PART = "dr-test"
DEFAULT_REGISTRANT_NAME = "Dr Práctikah Test"
DEFAULT_REGISTRANT_EMAIL = "test@medikah.health"
DEFAULT_REGISTRANT_COUNTRY = "MX"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse CLI flags."""
    p = argparse.ArgumentParser(
        description="End-to-end Práctikah provisioning dry-run (Phase 11).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--physician-id",
        required=True,
        help="UUID of a verified physician in Supabase",
    )
    p.add_argument(
        "--domain",
        required=True,
        help="Test domain to provision (orchestrator prefixes with 'sandbox-' in sandbox mode)",
    )
    p.add_argument(
        "--tld-strategy",
        choices=["real", "mocked"],
        default="mocked",
        help=(
            "'mocked' (default) skips registrar API; "
            "'real' burns ~$10 — requires CF Registrar beta access "
            "(Phase 10 D-17 carry-items required)"
        ),
    )
    p.add_argument(
        "--rollback-on-success",
        choices=["true", "false"],
        default="false",
        help=(
            "If 'true', invokes run_rollback after a successful provisioning run — "
            "verifies both directions in one CLI call"
        ),
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "If orphan runs are detected for this physician, resume rollback "
            "rather than exiting 4"
        ),
    )
    p.add_argument(
        "--mailbox-local-part",
        default=DEFAULT_LOCAL_PART,
        help=f"Mailbox local part (default: {DEFAULT_LOCAL_PART})",
    )
    p.add_argument(
        "--registrant-name",
        default=DEFAULT_REGISTRANT_NAME,
        help=f"WHOIS registrant name (default: {DEFAULT_REGISTRANT_NAME})",
    )
    p.add_argument(
        "--registrant-email",
        default=DEFAULT_REGISTRANT_EMAIL,
        help=f"WHOIS registrant email (default: {DEFAULT_REGISTRANT_EMAIL})",
    )
    p.add_argument(
        "--registrant-country",
        default=DEFAULT_REGISTRANT_COUNTRY,
        help=f"ISO 3166-1 alpha-2 country code (default: {DEFAULT_REGISTRANT_COUNTRY})",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main async logic
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    """Execute the provisioning dry-run and return an exit code."""

    # -----------------------------------------------------------------------
    # Safety: Force sandbox mode regardless of env state (D-19, T-11-07-05)
    # This CANNOT be bypassed from the CLI. Sandbox mode scopes Mailcow domains
    # to 'sandbox-' prefix and tags CF zones with purpose=sandbox.
    # -----------------------------------------------------------------------
    os.environ["MEDIKAH_PROVISIONING_SANDBOX"] = "true"
    print(f"[provision_test_doctor] forcing MEDIKAH_PROVISIONING_SANDBOX=true")

    # -----------------------------------------------------------------------
    # Lazy import AFTER forcing the env var (D-19: modules read SANDBOX at
    # import time via module-level os.getenv()). sys.path manipulation allows
    # running as `python -m scripts.provision_test_doctor` from the repo root
    # without needing the package to be installed.
    # -----------------------------------------------------------------------
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from services.practikah.orchestrator import (  # noqa: PLC0415
        provision_workspace,
        run_rollback,
        resume_orphan_runs,
    )
    from services.practikah.audit import ProvisioningLogWriter  # noqa: PLC0415

    physician_id = args.physician_id
    domain = args.domain

    # -----------------------------------------------------------------------
    # Orphan-run check (D-09: crash-resume)
    # -----------------------------------------------------------------------
    print(
        f"[provision_test_doctor] checking for orphan runs for "
        f"physician={physician_id}"
    )

    all_orphans = await ProvisioningLogWriter.list_orphan_runs()
    my_orphans = [(pid, rid) for pid, rid in all_orphans if pid == physician_id]

    if my_orphans:
        if args.resume:
            print(
                f"[provision_test_doctor] {len(my_orphans)} orphan run(s) detected "
                f"— resuming rollback per --resume flag"
            )
            for _pid, orphan_run_id in my_orphans:
                print(
                    f"[provision_test_doctor] rolling back orphan run_id={orphan_run_id}"
                )
                await run_rollback(physician_id=physician_id, run_id=orphan_run_id)
                print(
                    f"[provision_test_doctor] orphan rollback complete run_id={orphan_run_id}"
                )
        else:
            print(
                f"[provision_test_doctor] ERROR: {len(my_orphans)} orphan run(s) detected "
                f"for physician={physician_id}. Pass --resume to clean up first.",
                file=sys.stderr,
            )
            for _pid, orphan_run_id in my_orphans:
                print(f"  orphan run_id={orphan_run_id}", file=sys.stderr)
            return 4
    else:
        print("[provision_test_doctor] no orphans")

    # -----------------------------------------------------------------------
    # Generate mailbox password — NEVER printed (T-11-07-03)
    # -----------------------------------------------------------------------
    mailbox_password = token_urlsafe(24)

    # -----------------------------------------------------------------------
    # Run provisioning saga
    # -----------------------------------------------------------------------
    tld_strategy = args.tld_strategy
    print(
        f"[provision_test_doctor] starting provisioning domain={domain} "
        f"strategy={tld_strategy}"
    )

    started = time.monotonic()
    result = await provision_workspace(
        physician_id=physician_id,
        domain=domain,
        mailbox_local_part=args.mailbox_local_part,
        mailbox_password=mailbox_password,
        registrant_name=args.registrant_name,
        registrant_email=args.registrant_email,
        registrant_country=args.registrant_country,
        tld_strategy=tld_strategy,  # type: ignore[arg-type]
    )
    elapsed = time.monotonic() - started

    # -----------------------------------------------------------------------
    # Branch on result
    # -----------------------------------------------------------------------
    if result.success:
        print(
            f"[provision_test_doctor] PROVISIONING OK in {elapsed:.1f}s "
            f"— run_id={result.run_id} mailbox={result.mailbox_address}"
        )

        # Time-budget check (ROADMAP criterion 5)
        return_code = 0
        if elapsed > SUCCESS_BUDGET_SECONDS:
            print(
                f"[provision_test_doctor] WARNING: time-budget overrun: "
                f"{elapsed:.1f}s > {SUCCESS_BUDGET_SECONDS}s "
                f"(ROADMAP criterion 5 violation)",
                file=sys.stderr,
            )
            return_code = 3  # Time-budget overrun — but continue to rollback if requested

        # Optional rollback verification (--rollback-on-success true)
        if args.rollback_on_success == "true":
            print(
                f"[provision_test_doctor] --rollback-on-success true "
                f"→ invoking rollback for run_id={result.run_id}"
            )
            rollback_started = time.monotonic()
            await run_rollback(physician_id=physician_id, run_id=result.run_id)
            rollback_elapsed = time.monotonic() - rollback_started
            print(
                f"[provision_test_doctor] ROLLBACK OK in {rollback_elapsed:.1f}s"
            )

            # Verify rollback: no orphan for this run_id should remain
            orphans_after = await ProvisioningLogWriter.list_orphan_runs()
            if any(rid == result.run_id for _, rid in orphans_after):
                print(
                    f"[provision_test_doctor] ERROR: rollback verification failed "
                    f"— run_id={result.run_id} still appears in list_orphan_runs.",
                    file=sys.stderr,
                )
                return 2

        return return_code

    else:
        # Provisioning failed — orchestrator already attempted rollback internally
        print(
            f"[provision_test_doctor] PROVISIONING FAILED in {elapsed:.1f}s "
            f"— error={result.error}"
        )

        # Verify rollback completed: orphan check
        orphans_after = await ProvisioningLogWriter.list_orphan_runs()
        if any(rid == result.run_id for _, rid in orphans_after):
            print(
                f"[provision_test_doctor] ERROR: rollback incomplete "
                f"— run_id={result.run_id} still appears in list_orphan_runs. "
                f"Operator intervention required.",
                file=sys.stderr,
            )
            return 2

        print(
            f"[provision_test_doctor] rollback verified clean "
            f"(no orphan for run_id={result.run_id})"
        )
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Sync wrapper: load .env, configure logging, run async main."""
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[provision_test_doctor] interrupted")
        return 130
    except Exception:
        logging.exception("[provision_test_doctor] unhandled exception")
        return 99


if __name__ == "__main__":
    raise SystemExit(main())
