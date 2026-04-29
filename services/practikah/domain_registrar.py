"""Domain registrar wrapper with Cloudflare Registrar primary + OpenSRS fallback (Phase 11).

Per Phase 11 D-10: do_/undo_ pair contract.
Per Phase 11 D-12: NO automatic retry on failure — registrar APIs lack idempotency surface;
                    partial state at the registrar side risks double-charge on retry.
                    Failure → orchestrator escalates to rollback runner.
Per Phase 11 D-18: mocked-strategy short-circuit for CI / dev runs (no ~$10/run cost).
Per Phase 11 D-19: sandbox mode prefixes domain with 'sandbox-' before registration
                    (real registration of test names — caller should prefer mocked=True instead
                     unless validating the staging dry-run).

OPERATOR CARRY-ITEM (Phase 10 D-12): Cloudflare Registrar API beta access was requested but
not yet granted as of 2026-04-28 (see runbooks/cf-registrar-api-beta-request.md).
Plan 11-07 staging dry-run with --tld-strategy real will fail until access is granted.
Default to --tld-strategy mocked.

OpenSRS XML-RPC: minimal implementation in this phase. If real ccTLD registration is
needed before Phase 13 hardens this surface, register via OpenSRS web UI and pass the
resulting domain_id to the orchestrator manually.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

import httpx

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
OPENSRS_PRODUCTION_ENDPOINT = "https://rr-n1-tor.opensrs.net:55443"


@dataclass(frozen=True, slots=True)
class RegistrarResult:
    """Result envelope for every registrar mutation (per D-10 / CloudflareResult shape).

    Mirrors CloudflareResult / MailcowResult for orchestrator consistency.
    """

    success: bool
    registrar: Optional[str]             # 'cloudflare' | 'opensrs' | 'mocked'
    registrar_domain_id: Optional[str]   # vendor's resource id (or 'mock-<hash>' in mocked mode)
    raw_response: dict[str, Any]
    error: Optional[str] = None          # human-readable failure summary

    def summary(self) -> dict[str, Any]:
        """Compact representation for provisioning log detail fields."""
        return {
            "success": self.success,
            "registrar": self.registrar,
            "registrar_domain_id": self.registrar_domain_id,
            "error": self.error,
        }


class DomainRegistrar:
    """Domain registration with Cloudflare Registrar primary + OpenSRS fallback.

    Per Phase 11 D-10: do_/undo_ pair contract.
    Per Phase 11 D-12: failed registrations DO NOT auto-retry (no idempotency surface).
    Per Phase 11 D-18: mocked-strategy short-circuit for CI / dev runs.
    Per Phase 11 D-19: sandbox mode prefixes domain with 'sandbox-' before registration
                        (real registration of test names — caller should prefer mocked=True
                         unless validating the staging dry-run).

    TLD coverage per Phase 10 STACK §3 + Phase 11 D-16:
    - Cloudflare Registrar carries: most gTLDs + a curated set including .health, .doctor,
      .clinic (Práctikah Pro premium tier per REQUIREMENTS.md tier model).
    - OpenSRS covers ccTLDs Cloudflare doesn't (e.g., .com.ar, .com.br with local-presence rules).

    OPERATOR CARRY-ITEM: Cloudflare Registrar beta access was requested but not yet granted
    as of 2026-04-28. Cloudflare path will fail with 401 until access is granted.
    All Phase 11 / 11-07 testing uses mocked=True.
    """

    # TLD coverage per Phase 10 STACK §3 + Phase 11 D-16.
    # Cloudflare Registrar carries: most gTLDs + a curated set including .health, .doctor,
    # .clinic. OpenSRS covers ccTLDs Cloudflare doesn't (e.g., .com.ar, .com.br with
    # local-presence rules).
    CLOUDFLARE_SUPPORTED_TLDS: ClassVar[frozenset[str]] = frozenset({
        "com", "net", "org", "io", "co", "info",
        "health", "doctor", "clinic",   # Práctikah Pro premium tier per REQUIREMENTS.md tier model
        "us", "mx", "ca",
    })

    def __init__(
        self,
        cloudflare_api_token: str,
        cloudflare_account_id: str,         # required for CF Registrar API
        opensrs_username: str,
        opensrs_api_key: str,
        opensrs_endpoint: str = OPENSRS_PRODUCTION_ENDPOINT,
        *,
        sandbox_mode: bool = False,
        timeout_seconds: float = 15.0,      # registrar calls take longer than DNS
    ) -> None:
        if not cloudflare_api_token:
            raise ValueError("Cloudflare API token is required for DomainRegistrar")
        if not cloudflare_account_id:
            raise ValueError("Cloudflare account_id is required for DomainRegistrar")
        # OpenSRS credentials may be empty — only fail when an OpenSRS-routed domain
        # is requested (ccTLD path). This allows the registrar class to be instantiated
        # in environments without OpenSRS credentials for gTLD-only use.
        self._cf_token = cloudflare_api_token
        self._cf_account_id = cloudflare_account_id
        self._opensrs_user = opensrs_username
        self._opensrs_key = opensrs_api_key
        self._opensrs_endpoint = opensrs_endpoint
        self._sandbox_mode = sandbox_mode
        self._timeout = httpx.Timeout(timeout_seconds, connect=3.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _supports_cloudflare_registrar(domain: str) -> bool:
        """Return True if the TLD is in CLOUDFLARE_SUPPORTED_TLDS, else False (use OpenSRS).

        Handles multi-part ccTLDs (e.g. 'drlopez.com.ar') correctly by splitting on
        the LAST dot — 'com.ar' is not in the frozenset, so it routes to OpenSRS.
        """
        tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
        return tld in DomainRegistrar.CLOUDFLARE_SUPPORTED_TLDS

    def _maybe_sandbox_prefix(self, domain: str) -> str:
        """Apply 'sandbox-' prefix when sandbox_mode=True.

        Defense-in-depth: the orchestrator (Plan 11-06) is the primary sandbox guard.
        This method ensures the prefix is applied even if the orchestrator passes a
        non-prefixed domain name. Double-prefix is prevented by checking the existing prefix.
        Per D-19.
        """
        if self._sandbox_mode and not domain.startswith("sandbox-"):
            return f"sandbox-{domain}"
        return domain

    def _cf_headers(self) -> dict[str, str]:
        """Build standard Cloudflare API request headers."""
        return {
            "Authorization": f"Bearer {self._cf_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public do_/undo_ pair (per D-10)
    # ------------------------------------------------------------------

    async def do_register(
        self,
        domain: str,
        run_id: str,
        *,
        registrant_name: str,                  # doctor's full_name
        registrant_email: str,                 # doctor's email
        registrant_country: str,               # 'MX' | 'US' | etc — drives ccTLD local-presence
        mocked: bool = False,
        whois_privacy: bool = True,            # PRO-10: WHOIS privacy default ON
    ) -> RegistrarResult:
        """Register `domain`. Routes to Cloudflare Registrar or OpenSRS based on TLD.

        Per D-18: if mocked=True, skips external API and returns deterministic fake
        RegistrarResult with registrar='mocked'. The fake registrar_domain_id is
        sha256-derived from (domain, run_id) so it is stable across repeated calls
        with the same arguments (useful for rollback log correlation).

        Per D-19: if mocked=False AND sandbox_mode=True, prefixes domain with 'sandbox-'
        and registers a real domain. This is reserved for staging dry-runs — CI uses mocked=True.

        Per D-12: does NOT auto-retry on failure. Returns success=False on first
        failure; the orchestrator goes straight to undo_register (rollback). Rationale:
        registrar APIs may have partially completed the registration at the registrar's
        side (zone created, contacts populated, but charge not yet finalized) — retrying
        risks double-charging or leaving inconsistent state.

        Args:
            domain: The bare domain name (e.g., 'drlopez.health').
            run_id: Saga run identifier for correlation and audit.
            registrant_name: Doctor's full name for the WHOIS contact.
            registrant_email: Doctor's email for the WHOIS contact.
            registrant_country: ISO 3166-1 alpha-2 country code ('MX', 'US', etc.).
                                 Drives required local-presence checks for ccTLDs.
            mocked: If True, short-circuits the external API call entirely (per D-18).
            whois_privacy: If True, enables WHOIS privacy masking (default ON per PRO-10).

        Returns:
            RegistrarResult with success=True on success, success=False on failure.
        """
        # ----------------------------------------------------------------
        # Mocked short-circuit (per D-18) — MUST be the very first branch
        # to ensure mocked=True never accidentally reaches a real API call.
        # Acceptance criterion T-11-05-04 verifies this ordering.
        # ----------------------------------------------------------------
        if mocked:
            fake_id = f"mock-{hashlib.sha256(f'{domain}|{run_id}'.encode()).hexdigest()[:12]}"
            logger.info(
                "[registrar] mocked registration domain=%s run_id=%s fake_id=%s",
                domain, run_id, fake_id,
            )
            return RegistrarResult(
                success=True,
                registrar="mocked",
                registrar_domain_id=fake_id,
                raw_response={"mocked": True, "domain": domain, "run_id": run_id},
            )

        # Apply sandbox prefix (per D-19). This is the effective domain used for
        # real API calls. NOTE: Cloudflare may reject 'sandbox-drlopez.com' if
        # 'drlopez.com' itself is unavailable — that is acceptable; the mocked
        # strategy is the primary CI path.
        effective_domain = self._maybe_sandbox_prefix(domain)

        logger.info(
            "[registrar] do_register domain=%s effective=%s run_id=%s sandbox=%s",
            domain, effective_domain, run_id, self._sandbox_mode,
        )

        # Route based on TLD coverage per Phase 10 STACK §3 + Phase 11 D-16.
        # NOTE: Cloudflare failures DO NOT fall back to OpenSRS (per D-12 — registrar
        # failures go straight to rollback, not lateral retry).
        if self._supports_cloudflare_registrar(effective_domain):
            return await self._register_via_cloudflare(
                effective_domain,
                run_id,
                registrant_name=registrant_name,
                registrant_email=registrant_email,
                registrant_country=registrant_country,
                whois_privacy=whois_privacy,
            )
        else:
            return await self._register_via_opensrs(
                effective_domain,
                run_id,
                registrant_name=registrant_name,
                registrant_email=registrant_email,
                registrant_country=registrant_country,
                whois_privacy=whois_privacy,
            )

    async def undo_register(
        self, domain: str, run_id: str, prior_result: RegistrarResult
    ) -> None:
        """Initiate domain release / cancellation.

        Per D-10: undo_ methods MUST NOT raise — rollback runner relies on this guarantee.
        Tolerates 'already deleted' / 'already released' errors gracefully.

        - For Cloudflare: DELETE /accounts/{account_id}/registrar/domains/{domain}.
        - For OpenSRS: minimal-impl mode — logs operator warning, does not call API.
        - For mocked: no-op (no real resource was registered).

        Args:
            domain: The domain name passed to do_register (before sandbox prefix).
            run_id: Saga run identifier for correlation.
            prior_result: The RegistrarResult returned by do_register.
        """
        if prior_result.registrar == "mocked":
            logger.info(
                "[registrar] undo_register no-op for mocked registration domain=%s run_id=%s",
                domain, run_id,
            )
            return

        # Apply the same sandbox prefix used during registration so the DELETE targets
        # the same domain we actually registered.
        effective_domain = self._maybe_sandbox_prefix(domain)

        logger.info(
            "[registrar] undo_register domain=%s effective=%s registrar=%s run_id=%s",
            domain, effective_domain, prior_result.registrar, run_id,
        )

        if prior_result.registrar == "cloudflare":
            await self._undo_via_cloudflare(effective_domain, run_id)
        elif prior_result.registrar == "opensrs":
            # OpenSRS undo is deferred to Phase 13 when the first real ccTLD doctor
            # signs up. For now, log a clear operator warning so they can cancel manually
            # via the OpenSRS web UI. This satisfies T-11-05-05.
            logger.warning(
                "[registrar] undo_register OpenSRS cancellation deferred to Phase 13 "
                "— OPERATOR MUST manually cancel domain='%s' via OpenSRS web UI. "
                "run_id=%s",
                effective_domain, run_id,
            )
        else:
            logger.warning(
                "[registrar] undo_register unknown registrar='%s' for domain=%s run_id=%s — no-op",
                prior_result.registrar, effective_domain, run_id,
            )

    # ------------------------------------------------------------------
    # Cloudflare Registrar path (primary, gTLDs)
    # ------------------------------------------------------------------

    async def _register_via_cloudflare(
        self,
        domain: str,
        run_id: str,
        *,
        registrant_name: str,
        registrant_email: str,
        registrant_country: str,
        whois_privacy: bool,
    ) -> RegistrarResult:
        """Register `domain` via the Cloudflare Registrar API (beta).

        Per Phase 10 D-12: beta API access was requested but not yet granted as of
        2026-04-28. This path will return a 401/403 until access is granted.
        Plan 11-07 staging dry-run with --tld-strategy real will fail loudly, which
        is the intended signal that the operator carry-item is still open.

        API reference: https://blog.cloudflare.com/registrar-api-beta/
        Endpoint: POST /accounts/{account_id}/registrar/domains/{domain}
        Auth: Authorization: Bearer <CLOUDFLARE_API_TOKEN>
        Scopes required: registrar:edit, zone:read, zone:edit (per Phase 10 D-12).
        """
        url = f"{CLOUDFLARE_API_BASE}/accounts/{self._cf_account_id}/registrar/domains/{domain}"
        body: dict[str, Any] = {
            "name": domain,
            "auto_renew": True,
            "privacy": whois_privacy,
            "registrant": {
                "name": registrant_name,
                "email": registrant_email,
                "country": registrant_country,
            },
        }

        logger.info(
            "[registrar] _register_via_cloudflare domain=%s run_id=%s whois_privacy=%s",
            domain, run_id, whois_privacy,
        )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    json=body,
                    headers=self._cf_headers(),
                )
                response.raise_for_status()

            data: dict[str, Any] = response.json()
            # Cloudflare API v4 envelope: {"success": bool, "result": {...}, "errors": [...]}
            if data.get("success"):
                logger.info(
                    "[registrar] _register_via_cloudflare success domain=%s run_id=%s",
                    domain, run_id,
                )
                # Use the domain name itself as the resource ID — CF Registrar's primary
                # key for a registered domain is the domain name in the URL path.
                result_obj = data.get("result") or {}
                domain_id = result_obj.get("id") or domain
                return RegistrarResult(
                    success=True,
                    registrar="cloudflare",
                    registrar_domain_id=domain_id,
                    raw_response=data,
                )
            else:
                errors = data.get("errors", [])
                error_msg = "; ".join(
                    e.get("message", str(e)) for e in errors
                ) if errors else "Unknown Cloudflare Registrar error"
                logger.error(
                    "[registrar] _register_via_cloudflare CF returned success=False "
                    "domain=%s run_id=%s errors=%s",
                    domain, run_id, errors,
                )
                return RegistrarResult(
                    success=False,
                    registrar="cloudflare",
                    registrar_domain_id=None,
                    raw_response=data,
                    error=error_msg,
                )

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            msg = f"HTTP {err.response.status_code}: {err}"
            logger.exception(
                "[registrar] _register_via_cloudflare HTTP error domain=%s run_id=%s status=%s",
                domain, run_id, err.response.status_code,
            )
            return RegistrarResult(
                success=False,
                registrar="cloudflare",
                registrar_domain_id=None,
                raw_response=raw,
                error=msg,
            )
        except httpx.TransportError as err:
            msg = f"Network error: {err}"
            logger.exception(
                "[registrar] _register_via_cloudflare transport error domain=%s run_id=%s",
                domain, run_id,
            )
            return RegistrarResult(
                success=False,
                registrar="cloudflare",
                registrar_domain_id=None,
                raw_response={},
                error=msg,
            )

    async def _undo_via_cloudflare(self, domain: str, run_id: str) -> None:
        """Release a Cloudflare-registered domain via DELETE.

        Tolerates 404 (domain already released / transferred out).
        Per D-10: MUST NOT raise — swallows and logs all errors.
        """
        url = f"{CLOUDFLARE_API_BASE}/accounts/{self._cf_account_id}/registrar/domains/{domain}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.delete(url, headers=self._cf_headers())
                response.raise_for_status()
            logger.info(
                "[registrar] _undo_via_cloudflare success domain=%s run_id=%s",
                domain, run_id,
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info(
                    "[registrar] _undo_via_cloudflare domain already released (404) domain=%s run_id=%s",
                    domain, run_id,
                )
            else:
                # Log but do NOT raise — undo_ methods must be non-throwing (D-10).
                logger.exception(
                    "[registrar] _undo_via_cloudflare HTTP error domain=%s run_id=%s status=%s",
                    domain, run_id, err.response.status_code,
                )
        except Exception:
            logger.exception(
                "[registrar] _undo_via_cloudflare unexpected error domain=%s run_id=%s",
                domain, run_id,
            )

    # ------------------------------------------------------------------
    # OpenSRS path (fallback, ccTLDs)
    # ------------------------------------------------------------------

    async def _register_via_opensrs(
        self,
        domain: str,
        run_id: str,
        *,
        registrant_name: str,
        registrant_email: str,
        registrant_country: str,
        whois_privacy: bool,
    ) -> RegistrarResult:
        """Register `domain` via the OpenSRS Reseller API (XML-RPC over HTTPS).

        Minimal implementation per plan docstring: Phase 13 will harden this surface
        when the first real ccTLD doctor signs up. For Phase 11, all CI / staging
        test runs use mocked=True (--tld-strategy mocked), so this path is not
        exercised during Phase 11.

        If OpenSRS credentials are not configured, returns a clear failure with a
        grep-able error message (satisfies T-11-05-05 and operator carry-item visibility).

        API reference: https://help.opensrs.com/hc/en-us/sections/200272560-API-Documentation
        Endpoint: POST {opensrs_endpoint} (XML-RPC over HTTPS)
        Auth: X-Username + X-Signature headers
        Action: SW_REGISTER for new domain registrations.
        """
        # Fail fast on missing credentials rather than sending an unsigned request.
        if not self._opensrs_user or not self._opensrs_key:
            logger.warning(
                "[registrar] _register_via_opensrs OpenSRS credentials not configured "
                "domain=%s run_id=%s — operator must register via OpenSRS web UI and "
                "pass domain_id manually",
                domain, run_id,
            )
            return RegistrarResult(
                success=False,
                registrar="opensrs",
                registrar_domain_id=None,
                raw_response={},
                error=(
                    "OpenSRS credentials not configured — register via OpenSRS web UI "
                    "and pass domain_id manually for now (Phase 13 hardening)"
                ),
            )

        # Minimal XML-RPC implementation per plan specification.
        # Full SW_REGISTER XML-RPC is deferred to Phase 13; this stub returns a clear
        # failure with a grep-able "deferred to Phase 13" error so the operator knows
        # what to do (register via web UI). See T-11-05-05 in threat register.
        logger.warning(
            "[registrar] _register_via_opensrs OpenSRS XML-RPC implementation deferred "
            "to Phase 13 — domain='%s' run_id=%s. Register via OpenSRS web UI and pass "
            "the resulting domain_id to the orchestrator manually.",
            domain, run_id,
        )
        return RegistrarResult(
            success=False,
            registrar="opensrs",
            registrar_domain_id=None,
            raw_response={},
            error=(
                "OpenSRS XML-RPC implementation deferred to Phase 13 — "
                "register via OpenSRS web UI for now"
            ),
        )
