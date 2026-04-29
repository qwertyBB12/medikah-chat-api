"""Cloudflare API v4 wrapper for Práctikah orchestrator (Phase 11).

Per Phase 11 D-10: every public mutation exposes a do_/undo_ pair.
Per Phase 11 D-11: httpx async + tenacity retry on idempotent reads.
Per Phase 11 D-19: sandbox mode tags zones with purpose=sandbox.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from services.practikah.dns_writer import DnsRecord

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
# TXT record name used as belt-and-suspenders sandbox tag (per D-19)
# If CF rejects meta on POST /zones, this TXT record identifies sandbox zones for sweep queries.
SANDBOX_TAG_RECORD_NAME = "_practikah_sandbox"


@dataclass(frozen=True, slots=True)
class CloudflareResult:
    """Result envelope for every Cloudflare API mutation (per D-10 / cofepris.ts pattern)."""

    success: bool
    resource_id: Optional[str]   # zone_id, custom_hostname_id, or dns_record_id
    raw_response: dict[str, Any]
    error: Optional[str] = None  # human-readable failure summary

    def summary(self) -> dict[str, Any]:
        """Compact representation for provisioning log detail fields."""
        return {
            "success": self.success,
            "resource_id": self.resource_id,
            "error": self.error,
        }


class CloudflareClient:
    """Thin async wrapper around Cloudflare API v4.

    Exposes do_/undo_ pairs for the three operations the Phase 13 Pro provisioning
    saga requires: zone creation, custom hostname attach (CF for SaaS), and DNS
    record writes.  Each do_ method is idempotent via GET-check-before-POST.
    Each undo_ method tolerates already-deleted (404) state.

    Sandbox mode (per D-19): does NOT mock API calls.  Real Cloudflare requests
    are made, but zones are tagged with `meta.purpose='sandbox'` (and a fallback
    TXT record) so cleanup sweep queries can identify and delete them.
    """

    def __init__(
        self,
        api_token: str,
        *,
        sandbox_mode: bool = False,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not api_token:
            raise ValueError("Cloudflare API token is required for CloudflareClient")
        self._api_token = api_token
        self._sandbox_mode = sandbox_mode
        self._timeout = httpx.Timeout(timeout_seconds, connect=3.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self, idempotency_key: Optional[str] = None) -> dict[str, str]:
        """Build the standard Cloudflare API request headers."""
        h: dict[str, str] = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute a single HTTP request against the Cloudflare API v4.

        Returns the parsed JSON response body.  Raises httpx.HTTPStatusError on
        4xx/5xx responses so callers can catch and wrap into CloudflareResult.
        Raises httpx.TransportError on network-level failures — tenacity retries
        these on the private GET helpers.
        """
        url = f"{CLOUDFLARE_API_BASE}{path}"
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
    # Idempotent GET helpers (retried by tenacity per D-12)
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def _get_zone_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Return the first Cloudflare zone matching `name`, or None.

        Decorated with @retry for transient network failures (per D-12 — idempotent
        reads may be retried).  Called by do_create_zone as idempotency check.
        """
        data = await self._request("GET", f"/zones?name={name}")
        result = data.get("result", [])
        return result[0] if result else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def _get_custom_hostname(
        self, zone_id: str, hostname: str
    ) -> Optional[dict[str, Any]]:
        """Return the CF for SaaS custom hostname entry for `hostname` in `zone_id`, or None."""
        data = await self._request(
            "GET",
            f"/zones/{zone_id}/custom_hostnames?hostname={hostname}",
        )
        result = data.get("result", [])
        return result[0] if result else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def _get_dns_record(
        self, zone_id: str, record_type: str, name: str, content: str
    ) -> Optional[dict[str, Any]]:
        """Return an existing DNS record matching type+name+content, or None.

        Used by do_write_dns_record as the idempotency check before POSTing.
        Matches on all three fields to avoid false-positive matches when a zone
        has multiple TXT records at the same name (e.g. both SPF and DMARC at '@').
        """
        data = await self._request(
            "GET",
            f"/zones/{zone_id}/dns_records?type={record_type}&name={name}",
        )
        for record in data.get("result", []):
            if record.get("content") == content:
                return record
        return None

    # ------------------------------------------------------------------
    # Zone operations
    # ------------------------------------------------------------------

    async def do_create_zone(self, domain: str, run_id: str) -> CloudflareResult:
        """Create a Cloudflare zone for `domain`.

        Idempotent: GET zones?name=<domain> first; if found, return prior zone_id.
        In sandbox mode, attach metadata `{"purpose": "sandbox", "run_id": run_id}`
        to the POST body (per D-19).  Also writes a _practikah_sandbox TXT record
        as a belt-and-suspenders sweep tag in case CF rejects `meta` on POST.
        """
        logger.info("[cloudflare] do_create_zone domain=%s run_id=%s", domain, run_id)

        # Idempotency check
        try:
            existing = await self._get_zone_by_name(domain)
        except httpx.TransportError:
            logger.exception("[cloudflare] do_create_zone transport error during GET domain=%s", domain)
            return CloudflareResult(success=False, resource_id=None, raw_response={}, error="Network error during zone lookup")

        if existing:
            zone_id = existing["id"]
            logger.info("[cloudflare] do_create_zone zone already exists domain=%s zone_id=%s", domain, zone_id)
            return CloudflareResult(success=True, resource_id=zone_id, raw_response=existing)

        # Build POST body
        body: dict[str, Any] = {"name": domain, "type": "full"}
        if self._sandbox_mode:
            # Attempt to attach purpose=sandbox metadata on creation (per D-19).
            # CF may or may not honour the `meta` key on POST /zones — we add it
            # optimistically and fall back to the TXT record sweep tag below.
            body["meta"] = {"purpose": "sandbox", "run_id": run_id}

        try:
            data = await self._request("POST", "/zones", json_body=body)
            zone = data.get("result", {})
            zone_id = zone.get("id")
            logger.info("[cloudflare] do_create_zone success domain=%s zone_id=%s", domain, zone_id)

            # Belt-and-suspenders sandbox tag: write a TXT record so sweep queries
            # can find sandbox zones by TXT name even if `meta` was ignored (per D-19).
            if self._sandbox_mode and zone_id:
                await self._write_sandbox_tag_txt(zone_id, run_id)

            return CloudflareResult(success=True, resource_id=zone_id, raw_response=data)

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            msg = str(err)
            logger.exception("[cloudflare] do_create_zone failed domain=%s", domain)
            return CloudflareResult(success=False, resource_id=None, raw_response=raw, error=msg)

    async def undo_create_zone(
        self, domain: str, run_id: str, prior_result: CloudflareResult
    ) -> None:
        """Delete the zone created by do_create_zone.

        Idempotent: tolerates already-deleted (404) state.  Per D-10, undo_ methods
        MUST NOT raise — rollback runner relies on this guarantee.
        """
        logger.info("[cloudflare] undo_create_zone domain=%s run_id=%s", domain, run_id)

        if not prior_result.resource_id:
            logger.info("[cloudflare] undo_create_zone no zone_id to undo domain=%s", domain)
            return

        zone_id = prior_result.resource_id
        try:
            await self._request("DELETE", f"/zones/{zone_id}")
            logger.info("[cloudflare] undo_create_zone success domain=%s zone_id=%s", domain, zone_id)
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info("[cloudflare] undo_create_zone zone already deleted domain=%s zone_id=%s", domain, zone_id)
            else:
                # Log but do NOT raise — undo_ methods must be non-throwing (D-10).
                logger.exception("[cloudflare] undo_create_zone failed domain=%s zone_id=%s", domain, zone_id)
        except httpx.TransportError:
            logger.exception("[cloudflare] undo_create_zone transport error domain=%s zone_id=%s", domain, zone_id)

    # ------------------------------------------------------------------
    # Custom hostname operations (CF for SaaS)
    # ------------------------------------------------------------------

    async def do_create_custom_hostname(
        self, zone_id: str, hostname: str, run_id: str
    ) -> CloudflareResult:
        """Attach a CF for SaaS Custom Hostname under `zone_id`.

        Idempotent via GET /zones/{zone_id}/custom_hostnames?hostname=<hostname>.
        On success, CF issues a certificate validation record for the hostname.
        """
        logger.info(
            "[cloudflare] do_create_custom_hostname zone_id=%s hostname=%s run_id=%s",
            zone_id, hostname, run_id,
        )

        # Idempotency check
        try:
            existing = await self._get_custom_hostname(zone_id, hostname)
        except httpx.TransportError:
            logger.exception(
                "[cloudflare] do_create_custom_hostname transport error during GET zone_id=%s hostname=%s",
                zone_id, hostname,
            )
            return CloudflareResult(success=False, resource_id=None, raw_response={}, error="Network error during hostname lookup")

        if existing:
            hostname_id = existing["id"]
            logger.info(
                "[cloudflare] do_create_custom_hostname already exists zone_id=%s hostname=%s hostname_id=%s",
                zone_id, hostname, hostname_id,
            )
            return CloudflareResult(success=True, resource_id=hostname_id, raw_response=existing)

        body: dict[str, Any] = {
            "hostname": hostname,
            "ssl": {"method": "http", "type": "dv"},
        }

        try:
            data = await self._request(
                "POST", f"/zones/{zone_id}/custom_hostnames", json_body=body
            )
            result = data.get("result", {})
            hostname_id = result.get("id")
            logger.info(
                "[cloudflare] do_create_custom_hostname success zone_id=%s hostname=%s hostname_id=%s",
                zone_id, hostname, hostname_id,
            )
            return CloudflareResult(success=True, resource_id=hostname_id, raw_response=data)

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            msg = str(err)
            logger.exception(
                "[cloudflare] do_create_custom_hostname failed zone_id=%s hostname=%s", zone_id, hostname
            )
            return CloudflareResult(success=False, resource_id=None, raw_response=raw, error=msg)

    async def undo_create_custom_hostname(
        self,
        zone_id: str,
        hostname_id: str,
        run_id: str,
        prior_result: CloudflareResult,
    ) -> None:
        """Detach the custom hostname.  Tolerates 404 (already deleted)."""
        logger.info(
            "[cloudflare] undo_create_custom_hostname zone_id=%s hostname_id=%s run_id=%s",
            zone_id, hostname_id, run_id,
        )

        try:
            await self._request("DELETE", f"/zones/{zone_id}/custom_hostnames/{hostname_id}")
            logger.info(
                "[cloudflare] undo_create_custom_hostname success zone_id=%s hostname_id=%s",
                zone_id, hostname_id,
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info(
                    "[cloudflare] undo_create_custom_hostname already deleted zone_id=%s hostname_id=%s",
                    zone_id, hostname_id,
                )
            else:
                logger.exception(
                    "[cloudflare] undo_create_custom_hostname failed zone_id=%s hostname_id=%s",
                    zone_id, hostname_id,
                )
        except httpx.TransportError:
            logger.exception(
                "[cloudflare] undo_create_custom_hostname transport error zone_id=%s hostname_id=%s",
                zone_id, hostname_id,
            )

    # ------------------------------------------------------------------
    # DNS record operations
    # ------------------------------------------------------------------

    async def do_write_dns_record(
        self, zone_id: str, record: DnsRecord, run_id: str
    ) -> CloudflareResult:
        """Write a DNS record to `zone_id`.

        Idempotency:
        1. Compute a deterministic Idempotency-Key from run_id + record fields.
        2. GET existing records at type+name — if content matches, return prior id.
        3. POST with Idempotency-Key header so CF server-side de-duplicates retries.

        Per D-12: retry is NOT placed here (write without guaranteed idempotency).
        The GET idempotency-check + Idempotency-Key header together make re-invocation
        safe, but we do NOT use tenacity on the POST itself to avoid double-writes
        on partial-success responses.
        """
        logger.info(
            "[cloudflare] do_write_dns_record zone_id=%s type=%s name=%s run_id=%s",
            zone_id, record.record_type, record.name, run_id,
        )

        # Deterministic idempotency key per D-12
        idem_key = hashlib.sha256(
            f"{run_id}|{record.record_type}|{record.name}|{record.value}".encode()
        ).hexdigest()

        # Idempotency check: GET existing records matching type+name+content
        try:
            existing = await self._get_dns_record(zone_id, record.record_type, record.name, record.value)
        except httpx.TransportError:
            logger.exception(
                "[cloudflare] do_write_dns_record transport error during GET zone_id=%s name=%s",
                zone_id, record.name,
            )
            return CloudflareResult(success=False, resource_id=None, raw_response={}, error="Network error during DNS record lookup")

        if existing:
            record_id = existing["id"]
            logger.info(
                "[cloudflare] do_write_dns_record already exists zone_id=%s type=%s name=%s record_id=%s",
                zone_id, record.record_type, record.name, record_id,
            )
            return CloudflareResult(success=True, resource_id=record_id, raw_response=existing)

        # Build POST body
        body: dict[str, Any] = {
            "type": record.record_type,
            "name": record.name,
            "content": record.value,
            "ttl": record.ttl,
        }
        if record.priority is not None:
            body["priority"] = record.priority

        try:
            data = await self._request(
                "POST",
                f"/zones/{zone_id}/dns_records",
                json_body=body,
                idempotency_key=idem_key,
            )
            result = data.get("result", {})
            record_id = result.get("id")
            logger.info(
                "[cloudflare] do_write_dns_record success zone_id=%s type=%s name=%s record_id=%s",
                zone_id, record.record_type, record.name, record_id,
            )
            return CloudflareResult(success=True, resource_id=record_id, raw_response=data)

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            msg = str(err)
            logger.exception(
                "[cloudflare] do_write_dns_record failed zone_id=%s type=%s name=%s",
                zone_id, record.record_type, record.name,
            )
            return CloudflareResult(success=False, resource_id=None, raw_response=raw, error=msg)

    async def undo_write_dns_record(
        self, zone_id: str, record_id: str, run_id: str
    ) -> None:
        """Delete the DNS record identified by `record_id`.  Tolerates 404."""
        logger.info(
            "[cloudflare] undo_write_dns_record zone_id=%s record_id=%s run_id=%s",
            zone_id, record_id, run_id,
        )

        try:
            await self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
            logger.info(
                "[cloudflare] undo_write_dns_record success zone_id=%s record_id=%s",
                zone_id, record_id,
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info(
                    "[cloudflare] undo_write_dns_record already deleted zone_id=%s record_id=%s",
                    zone_id, record_id,
                )
            else:
                logger.exception(
                    "[cloudflare] undo_write_dns_record failed zone_id=%s record_id=%s",
                    zone_id, record_id,
                )
        except httpx.TransportError:
            logger.exception(
                "[cloudflare] undo_write_dns_record transport error zone_id=%s record_id=%s",
                zone_id, record_id,
            )

    # ------------------------------------------------------------------
    # Sandbox tagging helper (internal)
    # ------------------------------------------------------------------

    async def _write_sandbox_tag_txt(self, zone_id: str, run_id: str) -> None:
        """Write a TXT record at _practikah_sandbox with value=run_id.

        Belt-and-suspenders sandbox tag per D-19: even if CF ignores `meta.purpose`
        on POST /zones, this TXT record identifies sandbox zones for sweep queries.
        Never raises — failure is logged and swallowed (sandbox tag is informational).
        """
        body: dict[str, Any] = {
            "type": "TXT",
            "name": SANDBOX_TAG_RECORD_NAME,
            "content": run_id,
            "ttl": 300,
        }
        try:
            await self._request("POST", f"/zones/{zone_id}/dns_records", json_body=body)
            logger.info(
                "[cloudflare] _write_sandbox_tag_txt success zone_id=%s run_id=%s",
                zone_id, run_id,
            )
        except Exception:
            logger.exception(
                "[cloudflare] _write_sandbox_tag_txt failed (non-fatal) zone_id=%s run_id=%s",
                zone_id, run_id,
            )
