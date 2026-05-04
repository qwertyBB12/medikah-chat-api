"""Cloudflare for SaaS Custom Hostname adapter (Phase 13-06).

Mirrors the do_/undo_ envelope contract from ``cloudflare_client.py`` but
targets Cloudflare's CF-for-SaaS Custom Hostname API surface
(``/zones/{cf_for_saas_zone_id}/custom_hostnames``).

Per D-14 step 6 of the Pro upgrade saga: after the doctor's domain is
registered + DNS is published, we attach a CF for SaaS Custom Hostname so the
doctor's published website reaches the Práctikah edge zone via SNI. Cloudflare
then auto-issues a Let's Encrypt DV certificate for the hostname (WEB-07).

Sandbox mode (``MEDIKAH_PROVISIONING_SANDBOX=true`` OR
``CLOUDFLARE_API_TOKEN`` empty) short-circuits to deterministic stub responses
so 13-10's sandbox dry-run can run end-to-end without burning a real CF zone.

Per D-15: this is step 6 of 7, well past the point of no return (step 2). If
``poll_ssl_status`` times out we transition the saga to ``partial_finish_later``
rather than rolling back. ``undo_attach_hostname`` is therefore non-raising —
it logs and returns so the rollback runner can continue if pre-step-2 logic
ever calls it.
"""

from __future__ import annotations

import asyncio
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
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
# Dedicated CF for SaaS zone (e.g. ``saas.medikah.health``); distinct from the
# per-domain zones the registrar saga creates. Provisioned once in Phase 10
# infrastructure setup.
CF_SAAS_ZONE_ID = os.environ.get("CLOUDFLARE_FOR_SAAS_ZONE_ID", "")
_SANDBOX_MODE = os.getenv("MEDIKAH_PROVISIONING_SANDBOX", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@dataclass(frozen=True, slots=True)
class SaasResult:
    """Result envelope for every CF for SaaS API mutation.

    Mirrors ``CloudflareResult`` from ``cloudflare_client.py`` exactly so the
    orchestrator's UNDO_REGISTRY can treat both adapters uniformly.
    """

    success: bool
    resource_id: Optional[str]  # custom_hostname_id
    raw_response: dict[str, Any]
    error: Optional[str] = None

    def summary(self) -> dict[str, Any]:
        """Compact representation for practikah_provisioning_log detail fields."""
        return {
            "success": self.success,
            "resource_id": self.resource_id,
            "error": self.error,
        }


class CloudflareForSaasClient:
    """Thin async wrapper around Cloudflare's CF-for-SaaS Custom Hostname API.

    Methods exposed (do_/undo_ pair per D-10):
      - ``attach_hostname(domain, run_id)``           — POST /custom_hostnames
      - ``poll_ssl_status(hostname_id, timeout_sec)`` — GET; waits for LE issue
      - ``undo_attach_hostname(hostname_id, run_id, prior_result)`` — DELETE

    Sandbox mode (per D-19): when ``MEDIKAH_PROVISIONING_SANDBOX`` is set OR
    ``CLOUDFLARE_API_TOKEN`` is empty, write methods short-circuit to
    deterministic stubs with a ``sandbox-ch-`` resource_id prefix.
    """

    def __init__(
        self,
        api_token: str = "",
        zone_id: str = "",
        *,
        sandbox_mode: bool = False,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._api_token = api_token or CF_TOKEN
        self._zone_id = zone_id or CF_SAAS_ZONE_ID
        # Auto-engage sandbox if no token is configured — keeps tests + 13-10
        # sandbox dry-run usable without live CF credentials.
        self._sandbox_mode = sandbox_mode or _SANDBOX_MODE or not self._api_token
        self._timeout = httpx.Timeout(timeout_seconds, connect=3.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = f"{CF_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method, url, json=json_body, headers=self._headers()
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # do_/undo_ pair: attach Custom Hostname
    # ------------------------------------------------------------------

    async def attach_hostname(self, domain: str, run_id: str) -> SaasResult:
        """Attach ``domain`` as a CF for SaaS Custom Hostname.

        POST /zones/{cf_for_saas_zone_id}/custom_hostnames with
        ``{"hostname": domain, "ssl": {"method": "http", "type": "dv"}}``.

        Cloudflare schedules a Let's Encrypt DV certificate issuance for the
        hostname; ``poll_ssl_status`` waits for it to reach ``status=active``.

        Sandbox short-circuits to a deterministic stub.
        """
        logger.info(
            "[cf_for_saas] attach_hostname domain=%s run_id=%s sandbox=%s",
            domain,
            run_id,
            self._sandbox_mode,
        )

        if self._sandbox_mode:
            return SaasResult(
                success=True,
                resource_id=f"sandbox-ch-{domain}",
                raw_response={"sandbox": True, "hostname": domain, "run_id": run_id},
            )

        body: dict[str, Any] = {
            "hostname": domain,
            "ssl": {"method": "http", "type": "dv"},
        }
        path = f"/zones/{self._zone_id}/custom_hostnames"
        try:
            data = await self._request("POST", path, json_body=body)
            result = data.get("result", {}) or {}
            hostname_id = result.get("id")
            return SaasResult(
                success=True, resource_id=hostname_id, raw_response=data
            )
        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            # 409 already-attached → success per saga semantics.
            if err.response.status_code == 409:
                logger.info(
                    "[cf_for_saas] attach_hostname already attached domain=%s",
                    domain,
                )
                return SaasResult(
                    success=True, resource_id=raw.get("result", {}).get("id"),
                    raw_response=raw,
                )
            logger.exception(
                "[cf_for_saas] attach_hostname failed domain=%s", domain
            )
            return SaasResult(
                success=False, resource_id=None, raw_response=raw, error=str(err)
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def _get_custom_hostname(self, hostname_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}"
        )

    async def poll_ssl_status(
        self, hostname_id: str, timeout_sec: int = 300
    ) -> SaasResult:
        """Poll until ``ssl.status == 'active'`` or ``timeout_sec`` elapses.

        Per WEB-07: Cloudflare auto-provisions a Let's Encrypt DV certificate
        for the Custom Hostname. Validation typically completes within 30-60
        seconds once the doctor's CNAME / A record points at our edge, but
        we allow up to 5 minutes by default.

        Returns ``SaasResult(success=True, ...)`` once active. On timeout,
        returns ``success=False`` so the saga transitions to
        ``partial_finish_later`` (per D-15 — step 6 is post-POR).
        """
        logger.info(
            "[cf_for_saas] poll_ssl_status hostname_id=%s timeout_sec=%s sandbox=%s",
            hostname_id,
            timeout_sec,
            self._sandbox_mode,
        )

        if self._sandbox_mode:
            # Single zero-latency loop — no real polling in sandbox.
            return SaasResult(
                success=True,
                resource_id=hostname_id,
                raw_response={"sandbox": True, "ssl": {"status": "active"}},
            )

        deadline = asyncio.get_event_loop().time() + max(0, timeout_sec)
        last_raw: dict[str, Any] = {}
        last_status: Optional[str] = None

        while asyncio.get_event_loop().time() < deadline:
            try:
                data = await self._get_custom_hostname(hostname_id)
                last_raw = data
                result = data.get("result", {}) or {}
                ssl = result.get("ssl") or {}
                last_status = ssl.get("status")
                if last_status == "active":
                    return SaasResult(
                        success=True, resource_id=hostname_id, raw_response=data
                    )
                # Cloudflare also reports terminal failure modes — surface them.
                if last_status in {"deleted", "deactivated"}:
                    return SaasResult(
                        success=False,
                        resource_id=hostname_id,
                        raw_response=data,
                        error=f"ssl status terminal: {last_status}",
                    )
            except httpx.HTTPStatusError as err:
                logger.warning(
                    "[cf_for_saas] poll_ssl_status HTTP error hostname_id=%s status=%s",
                    hostname_id,
                    err.response.status_code,
                )
            except httpx.TransportError:
                logger.warning(
                    "[cf_for_saas] poll_ssl_status transport error hostname_id=%s",
                    hostname_id,
                )

            await asyncio.sleep(5)

        return SaasResult(
            success=False,
            resource_id=hostname_id,
            raw_response=last_raw,
            error=f"timed out after {timeout_sec}s waiting for LE cert; last_status={last_status}",
        )

    async def undo_attach_hostname(
        self,
        hostname_id: str,
        run_id: str,
        prior_result: SaasResult,
    ) -> None:
        """Detach the Custom Hostname (best-effort; never raises).

        Per D-15: this step is post-POR, so undo runs only when called by an
        explicit operator-driven cleanup (or 13-10 sandbox sweeper). Tolerates
        404 (already deleted) silently.
        """
        logger.info(
            "[cf_for_saas] undo_attach_hostname hostname_id=%s run_id=%s",
            hostname_id,
            run_id,
        )

        if not hostname_id:
            return

        if self._sandbox_mode:
            return

        try:
            await self._request(
                "DELETE", f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}"
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info(
                    "[cf_for_saas] undo_attach_hostname already deleted hostname_id=%s",
                    hostname_id,
                )
            else:
                logger.exception(
                    "[cf_for_saas] undo_attach_hostname failed hostname_id=%s",
                    hostname_id,
                )
        except httpx.TransportError:
            logger.exception(
                "[cf_for_saas] undo_attach_hostname transport error hostname_id=%s",
                hostname_id,
            )


# Module singleton. Sandbox auto-engages when CLOUDFLARE_API_TOKEN is unset.
cf_saas = CloudflareForSaasClient()
