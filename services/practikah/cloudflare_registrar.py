"""Cloudflare Registrar API wrapper for Práctikah Pro upsell (Phase 13).

Mirrors the do_/undo_ envelope contract from `cloudflare_client.py` and
`mailbox_provisioner.py`, but targets Cloudflare's *Registrar* API surface
(`/accounts/{account_id}/registrar/domains`) rather than the Zone/DNS API.

Per Phase 13 D-04: Cloudflare-only registrar at launch (sandbox mode short-
circuits to deterministic stub responses while CLOUDFLARE_REGISTRAR_TOKEN is
unset and the live wiring lands in 13-10).

Per Phase 13 D-15: Step 2 (do_register) is the saga's point of no return —
ICANN's 60-day post-registration transfer lock prevents an actual unregister.
`undo_register` therefore MUST NOT raise; it logs a warning and returns so the
finish-later handler can continue rolling forward.

Per PRO-09 + PRO-10: `auto_renew=True` and `privacy=True` are hardcoded into
every `do_register` body. There is no code path that disables WHOIS privacy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

CF_API_BASE = "https://api.cloudflare.com/client/v4"
CF_REGISTRAR_TOKEN = os.environ.get("CLOUDFLARE_REGISTRAR_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
_SANDBOX_MODE = os.getenv("MEDIKAH_PROVISIONING_SANDBOX", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@dataclass(frozen=True, slots=True)
class RegistrarResult:
    """Result envelope for every Cloudflare Registrar API mutation.

    Mirrors `CloudflareResult` from cloudflare_client.py exactly so the
    orchestrator's UNDO_REGISTRY can treat both adapters uniformly.
    """

    success: bool
    resource_id: Optional[str]
    raw_response: dict[str, Any]
    error: Optional[str] = None

    def summary(self) -> dict[str, Any]:
        """Compact representation for practikah_provisioning_log detail fields."""
        return {
            "success": self.success,
            "resource_id": self.resource_id,
            "error": self.error,
        }


class CloudflareRegistrarClient:
    """Thin async wrapper around Cloudflare's Registrar API.

    Methods exposed (do_/undo_ pairs per D-10):
      - check_availability(domain)         — read; CF Registrar availability
      - do_register(domain, registrant, run_id) / undo_register(...)
      - do_transfer_in(domain, epp_code, registrant, run_id)
      - do_transfer_out(domain, run_id)    — returns EPP code in resource_id
      - get_expiration(domain)             — read; consumed by 13-09 dashboard

    Sandbox mode (per D-19 / D-04 launch path): when MEDIKAH_PROVISIONING_SANDBOX
    is set OR CLOUDFLARE_REGISTRAR_TOKEN is empty, write methods short-circuit
    and return deterministic stub responses with a "sandbox-" resource_id prefix.
    Read methods (check_availability, get_expiration) return RDAP-fallback-friendly
    failure envelopes so callers degrade gracefully.

    NON-NEGOTIABLES:
      1. `privacy=True` and `auto_renew=True` are hardcoded into do_register/transfer_in.
      2. undo_register MUST NOT raise (D-15 ICANN 60-day lock).
      3. EPP code from do_transfer_out is surfaced via RegistrarResult.resource_id.
    """

    def __init__(
        self,
        api_token: str = "",
        account_id: str = "",
        *,
        sandbox_mode: bool = False,
        timeout_seconds: float = 10.0,
    ) -> None:
        # Effective configuration (instance args override env at construction time)
        self._api_token = api_token or CF_REGISTRAR_TOKEN
        self._account_id = account_id or CF_ACCOUNT_ID
        self._sandbox_mode = sandbox_mode or _SANDBOX_MODE or not self._api_token
        self._timeout = httpx.Timeout(timeout_seconds, connect=3.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self, idempotency_key: Optional[str] = None) -> dict[str, str]:
        h: dict[str, str] = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            # Cloudflare's documented header is X-Idempotency-Key on Registrar mutations.
            h["X-Idempotency-Key"] = idempotency_key
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        url = f"{CF_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method,
                url,
                json=json_body,
                headers=self._headers(idempotency_key),
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Read: availability
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def check_availability(self, domain: str) -> RegistrarResult:
        """Check whether `domain` is available to register via CF Registrar.

        Returns RegistrarResult with raw_response shaped like:
            {"available": bool, "tld": str, "wholesale_price_usd": float}

        On CF 4xx/5xx, returns success=False so the caller (availability service)
        can fall back to RDAP per D-20.
        """
        logger.info("[cf_registrar] check_availability domain=%s", domain)

        if self._sandbox_mode:
            tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
            return RegistrarResult(
                success=True,
                resource_id=None,
                raw_response={
                    "available": True,
                    "tld": tld,
                    "wholesale_price_usd": 9.15,
                    "sandbox": True,
                },
            )

        path = (
            f"/accounts/{self._account_id}/registrar/domains/{domain}/availability"
        )
        try:
            data = await self._request("GET", path)
            result = data.get("result", {}) or {}
            tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
            raw = {
                "available": bool(result.get("available", False)),
                "tld": result.get("tld", tld),
                "wholesale_price_usd": result.get(
                    "wholesale_price_usd", result.get("price")
                ),
            }
            return RegistrarResult(success=True, resource_id=None, raw_response=raw)
        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            logger.warning(
                "[cf_registrar] check_availability HTTP error domain=%s status=%s",
                domain,
                err.response.status_code,
            )
            return RegistrarResult(
                success=False,
                resource_id=None,
                raw_response=raw,
                error=str(err),
            )

    # ------------------------------------------------------------------
    # Mutate: register / undo_register
    # ------------------------------------------------------------------

    async def do_register(
        self,
        domain: str,
        registrant: dict[str, Any],
        run_id: str,
    ) -> RegistrarResult:
        """Register `domain` via Cloudflare Registrar.

        Body hardcodes `auto_renew=True` (PRO-09) and `privacy=True` (PRO-10).
        Idempotency: X-Idempotency-Key: run_id so retries don't double-register.

        Sandbox short-circuit returns success with resource_id="sandbox-{domain}".
        Status 200/201/409("already_registered_to_account") all map to success=True.
        Other 4xx/5xx → success=False with error string.
        """
        logger.info("[cf_registrar] do_register domain=%s run_id=%s", domain, run_id)

        if self._sandbox_mode:
            logger.info(
                "[cf_registrar] do_register sandbox short-circuit domain=%s", domain
            )
            return RegistrarResult(
                success=True,
                resource_id=f"sandbox-{domain}",
                raw_response={
                    "sandbox": True,
                    "domain": domain,
                    "auto_renew": True,
                    "privacy": True,
                },
            )

        body: dict[str, Any] = {
            "name": domain,
            "auto_renew": True,
            "privacy": True,
            "contacts": registrant,
        }

        path = f"/accounts/{self._account_id}/registrar/domains"
        try:
            data = await self._request(
                "POST", path, json_body=body, idempotency_key=run_id
            )
            result = data.get("result", {}) or {}
            domain_id = result.get("id") or domain
            logger.info(
                "[cf_registrar] do_register success domain=%s id=%s",
                domain,
                domain_id,
            )
            return RegistrarResult(
                success=True, resource_id=domain_id, raw_response=data
            )
        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            # 409 with "already_registered_to_account" is a success per saga semantics.
            err_code = ""
            try:
                errors = raw.get("errors", []) or []
                if errors:
                    err_code = str(errors[0].get("code", "")) or str(
                        errors[0].get("message", "")
                    )
            except Exception:
                pass
            if err.response.status_code == 409 and (
                "already_registered_to_account" in err_code.lower()
                or "already_registered_to_account" in str(raw).lower()
            ):
                logger.info(
                    "[cf_registrar] do_register already-owned domain=%s", domain
                )
                return RegistrarResult(
                    success=True, resource_id=domain, raw_response=raw
                )
            logger.exception("[cf_registrar] do_register failed domain=%s", domain)
            return RegistrarResult(
                success=False, resource_id=None, raw_response=raw, error=str(err)
            )

    async def undo_register(
        self,
        domain: str,
        run_id: str,
        prior_result: RegistrarResult,
    ) -> None:
        """Compensating action for do_register.

        Per D-15: registration is the saga's point of no return. ICANN imposes
        a 60-day post-registration transfer lock; we cannot programmatically
        unregister a freshly-registered domain. This method therefore logs a
        warning and returns. The finish-later handler in the orchestrator
        continues forward rather than treating this as a rollback failure.

        MUST NOT raise — orchestrator depends on this for D-15 invariants.
        """
        logger.warning(
            "[cf_registrar] undo_register called for %s — domain registered, "
            "ICANN 60-day lock prevents unregister; finish-later handler will continue "
            "(run_id=%s prior_resource_id=%s)",
            domain,
            run_id,
            prior_result.resource_id if prior_result else None,
        )
        return None

    # ------------------------------------------------------------------
    # Mutate: transfer-in (PRO-06)
    # ------------------------------------------------------------------

    async def do_transfer_in(
        self,
        domain: str,
        epp_code: str,
        registrant: dict[str, Any],
        run_id: str,
    ) -> RegistrarResult:
        """Initiate a transfer-in to Cloudflare Registrar (PRO-06).

        Body sets `transfer_in=true` and includes the EPP/auth_code from the
        losing registrar. WHOIS privacy and auto-renew are still hardcoded on.
        """
        logger.info(
            "[cf_registrar] do_transfer_in domain=%s run_id=%s", domain, run_id
        )

        if self._sandbox_mode:
            return RegistrarResult(
                success=True,
                resource_id=f"sandbox-transfer-{domain}",
                raw_response={"sandbox": True, "transfer_in": True, "domain": domain},
            )

        body: dict[str, Any] = {
            "name": domain,
            "transfer_in": True,
            "auth_code": epp_code,
            "auto_renew": True,
            "privacy": True,
            "contacts": registrant,
        }
        path = f"/accounts/{self._account_id}/registrar/domains"
        try:
            data = await self._request(
                "POST", path, json_body=body, idempotency_key=run_id
            )
            result = data.get("result", {}) or {}
            return RegistrarResult(
                success=True,
                resource_id=result.get("id") or domain,
                raw_response=data,
            )
        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            logger.exception(
                "[cf_registrar] do_transfer_in failed domain=%s", domain
            )
            return RegistrarResult(
                success=False, resource_id=None, raw_response=raw, error=str(err)
            )

    # ------------------------------------------------------------------
    # Mutate: transfer-out (PRO-11)
    # ------------------------------------------------------------------

    async def do_transfer_out(self, domain: str, run_id: str) -> RegistrarResult:
        """Initiate transfer-out (release) for `domain` (PRO-11).

        CF returns the EPP/auth_code synchronously in the response body; the
        physician needs this to complete transfer at the gaining registrar.
        We surface it via RegistrarResult.resource_id so the route handler
        can return it directly to the dashboard UI without an extra call.
        """
        logger.info(
            "[cf_registrar] do_transfer_out domain=%s run_id=%s", domain, run_id
        )

        if self._sandbox_mode:
            stub_epp = f"SANDBOX-EPP-{domain}".upper().replace(".", "-")
            return RegistrarResult(
                success=True,
                resource_id=stub_epp,
                raw_response={
                    "sandbox": True,
                    "auth_code": stub_epp,
                    "domain": domain,
                },
            )

        path = (
            f"/accounts/{self._account_id}/registrar/domains/{domain}/transfer_out"
        )
        try:
            data = await self._request("POST", path, idempotency_key=run_id)
            result = data.get("result", {}) or {}
            epp_code = (
                result.get("auth_code")
                or result.get("epp_code")
                or result.get("transfer_auth_code")
                or ""
            )
            if not epp_code:
                logger.warning(
                    "[cf_registrar] do_transfer_out missing auth_code in response domain=%s",
                    domain,
                )
            return RegistrarResult(
                success=True, resource_id=epp_code, raw_response=data
            )
        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            logger.exception(
                "[cf_registrar] do_transfer_out failed domain=%s", domain
            )
            return RegistrarResult(
                success=False, resource_id=None, raw_response=raw, error=str(err)
            )

    # ------------------------------------------------------------------
    # Read: expiration (consumed by 13-09 dashboard for PRO-08)
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def get_expiration(self, domain: str) -> RegistrarResult:
        """Return the registration expiration timestamp for `domain` (PRO-08).

        raw_response shape: {"expires_at": iso8601, "auto_renew": bool}.
        """
        logger.info("[cf_registrar] get_expiration domain=%s", domain)

        if self._sandbox_mode:
            return RegistrarResult(
                success=True,
                resource_id=domain,
                raw_response={
                    "sandbox": True,
                    "expires_at": "2027-05-01T00:00:00Z",
                    "auto_renew": True,
                },
            )

        path = f"/accounts/{self._account_id}/registrar/domains/{domain}"
        try:
            data = await self._request("GET", path)
            result = data.get("result", {}) or {}
            return RegistrarResult(
                success=True,
                resource_id=domain,
                raw_response={
                    "expires_at": result.get("expires_at"),
                    "auto_renew": result.get("auto_renew", True),
                },
            )
        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            logger.warning(
                "[cf_registrar] get_expiration HTTP error domain=%s status=%s",
                domain,
                err.response.status_code,
            )
            return RegistrarResult(
                success=False, resource_id=None, raw_response=raw, error=str(err)
            )


# Module singleton. Sandbox mode auto-engages when CLOUDFLARE_REGISTRAR_TOKEN is unset.
cf_registrar = CloudflareRegistrarClient()
