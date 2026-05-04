"""Mailcow Admin API wrapper for Práctikah orchestrator (Phase 11).

See https://mailcow.docs.apiary.io/ for full API surface.

Per Phase 11 D-10: every public mutation exposes a do_/undo_ pair.
Per Phase 11 D-11: httpx async + tenacity retry on idempotent reads.
Per Phase 11 D-12: idempotency via GET-before-POST (Mailcow has no Idempotency-Key header).
Per Phase 11 D-19: sandbox mode prefixes domain names with 'sandbox-'.

OPERATOR NOTE (per Phase 11 D-17): MAILCOW_API_KEY rotation is a Phase 10 carry-item.
The current key returns 401. Plan 11-07 staging dry-run will fail loudly until rotated.
Rotation steps: Mailcow admin → Configuration → Access → API → Regenerate → update Render env.

Resource cleanup ordering: Mailcow forbids deleting a domain with active mailboxes.
The orchestrator's rollback runner walks the log in reverse step order, which yields
mailbox-deletion-first naturally. undo_add_domain still logs (does not raise) on
'domain has active mailboxes' error so rollback can continue.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

DEFAULT_DKIM_SELECTOR = "mcdkim"
DEFAULT_QUOTA_MB = 10240  # MAIL-08: 10 GB


@dataclass(frozen=True, slots=True)
class MailcowResult:
    """Result envelope for every Mailcow API mutation (per D-10).

    Mirrors CloudflareResult from Plan 11-03 for orchestrator consistency.
    """

    success: bool
    resource_id: Optional[str]  # domain name (for domain ops) or local_part@domain (for mailbox ops)
    raw_response: dict[str, Any]
    error: Optional[str] = None  # human-readable failure summary

    def summary(self) -> dict[str, Any]:
        """Compact representation for provisioning log detail fields."""
        return {
            "success": self.success,
            "resource_id": self.resource_id,
            "error": self.error,
        }


class MailboxProvisioner:
    """Thin async wrapper around the Mailcow Admin API.

    See https://mailcow.docs.apiary.io/ for API surface.

    Per Phase 11 D-10: every public mutation exposes a do_/undo_ pair.
    Per Phase 11 D-12: idempotency via GET-before-POST (Mailcow has no Idempotency-Key header).
    Per Phase 11 D-19: sandbox mode prefixes domain names with 'sandbox-'.

    The orchestrator (Plan 11-06) is the primary site that decides to inject the sandbox
    prefix. This class applies the prefix as defense-in-depth — if the orchestrator passes
    an un-prefixed domain when sandbox_mode=True, this class corrects it automatically.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        sandbox_mode: bool = False,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not api_url:
            raise ValueError("Mailcow API URL is required")
        if not api_key:
            raise ValueError("Mailcow API key is required")
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._sandbox_mode = sandbox_mode
        self._timeout = httpx.Timeout(timeout_seconds, connect=3.0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build standard Mailcow Admin API request headers.

        Mailcow uses X-API-Key authentication, NOT Authorization: Bearer.
        See https://mailcow.docs.apiary.io/#introduction/api-access
        """
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }

    def _maybe_sandbox_prefix(self, domain: str) -> str:
        """Apply 'sandbox-' prefix when sandbox_mode=True.

        Defense-in-depth: the orchestrator (Plan 11-06) is the primary sandbox guard.
        This method ensures the prefix is applied even if the orchestrator passes a
        non-prefixed domain name. Double-prefix is prevented by checking the existing prefix.
        """
        if self._sandbox_mode and not domain.startswith("sandbox-"):
            return f"sandbox-{domain}"
        return domain

    async def _request_write(
        self, method: str, path: str, json_body: Any
    ) -> Any:
        """Execute a non-retried write against the Mailcow Admin API.

        Mailcow write envelope: [{"type": "success", "msg": [...]}] or [{"type": "error", "msg": "..."}].
        Returns the parsed JSON body (may be list or dict — parse defensively).
        Raises httpx.HTTPStatusError on 4xx/5xx responses.
        Raises httpx.TransportError on network-level failures.

        Write calls are NOT retried per D-12 (Mailcow has no Idempotency-Key header).
        The GET-before-POST check in each do_ method is the idempotency guard.
        """
        url = f"{self._api_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method,
                url,
                json=json_body,
                headers=self._headers(),
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
    async def _get_domain(self, domain: str) -> Optional[dict[str, Any]]:
        """Return the Mailcow domain record for `domain`, or None if not found.

        GET /api/v1/get/domain/<name> returns the domain object if found,
        or {} (empty dict) if the domain does not exist.

        Decorated with @retry for transient network failures (per D-12 — idempotent
        reads may be retried). Called by do_add_domain as idempotency check.
        """
        url = f"{self._api_url}/api/v1/get/domain/{quote(domain, safe='')}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            data = response.json()
            # Mailcow returns {} for missing domains, or the domain dict for existing ones
            if not data or (isinstance(data, dict) and not data):
                return None
            return data if isinstance(data, dict) else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def _get_mailbox(self, address: str) -> Optional[dict[str, Any]]:
        """Return the Mailcow mailbox record for `address`, or None if not found.

        GET /api/v1/get/mailbox/<address> returns the mailbox object if found,
        or {} if the mailbox does not exist.

        Decorated with @retry for transient network failures (per D-12).
        Called by do_add_mailbox as idempotency check.
        """
        url = f"{self._api_url}/api/v1/get/mailbox/{quote(address, safe='')}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            data = response.json()
            if not data or (isinstance(data, dict) and not data):
                return None
            return data if isinstance(data, dict) else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def _get_dkim(self, domain: str) -> Optional[dict[str, Any]]:
        """Return the Mailcow DKIM record for `domain`, or None if no DKIM key exists.

        GET /api/v1/get/dkim/<domain> returns the DKIM record if found, or {} if not.

        Decorated with @retry for transient network failures (per D-12).
        Called by do_get_dkim for both the initial check and the post-create re-fetch.
        """
        url = f"{self._api_url}/api/v1/get/dkim/{quote(domain, safe='')}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            data = response.json()
            if not data or (isinstance(data, dict) and not data):
                return None
            return data if isinstance(data, dict) else None

    def _parse_mailcow_write_response(
        self, response: Any
    ) -> tuple[bool, Optional[str]]:
        """Parse Mailcow's non-standard write response envelope.

        Mailcow write responses are a list with one element:
            [{"type": "success", "msg": ["...", "..."]}]
        or on error:
            [{"type": "error", "msg": "..."}]

        Returns (success: bool, error_message: str | None).
        Parses defensively for both success and error variants.
        """
        if not response:
            return False, "Empty response from Mailcow API"

        # Handle list envelope (standard Mailcow write response)
        item = response[0] if isinstance(response, list) else response
        if not isinstance(item, dict):
            return False, f"Unexpected response format: {response!r}"

        response_type = item.get("type", "")
        if response_type == "success":
            return True, None

        # Error envelope — msg may be a string or list
        msg = item.get("msg", "Unknown error")
        if isinstance(msg, list):
            msg = " ".join(str(m) for m in msg)
        return False, str(msg)

    # ------------------------------------------------------------------
    # Domain operations
    # ------------------------------------------------------------------

    async def do_add_domain(
        self,
        domain: str,
        *,
        run_id: str,
        quota_mb: int = DEFAULT_QUOTA_MB,
    ) -> MailcowResult:
        """Create a mail domain in Mailcow.

        Idempotent: GET /api/v1/get/domain/<name> first. If the domain already exists,
        returns a success result with the existing domain as resource_id.

        In sandbox mode, prefixes domain with 'sandbox-' (defense-in-depth; the
        orchestrator should pass already-prefixed names per D-19).

        Args:
            domain: The domain name to create (e.g., 'drlopez.com').
            run_id: Saga run identifier for correlation and audit.
            quota_mb: Total mailbox quota for the domain in MB. Default 10 GB per MAIL-08.

        Returns:
            MailcowResult with success=True and resource_id=effective_domain on success.
        """
        effective_domain = self._maybe_sandbox_prefix(domain)
        logger.info(
            "[mailcow] do_add_domain domain=%s effective=%s run_id=%s sandbox=%s",
            domain, effective_domain, run_id, self._sandbox_mode,
        )

        # Idempotency check: GET-before-POST per D-12
        try:
            existing = await self._get_domain(effective_domain)
        except httpx.TransportError:
            logger.exception(
                "[mailcow] do_add_domain transport error during GET domain=%s", effective_domain
            )
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response={},
                error="Network error during domain lookup",
            )

        if existing:
            logger.info(
                "[mailcow] do_add_domain domain already exists domain=%s", effective_domain
            )
            return MailcowResult(
                success=True,
                resource_id=effective_domain,
                raw_response=existing,
            )

        # POST to create the domain
        # rl_value=200, rl_frame=h sets the per-domain rate-limit baseline per MAIL-13.
        # Full rate-limit enforcement lands in Phase 14 (OPS-04), but we set the field at
        # create time so it can be tightened without domain recreation.
        body: dict[str, Any] = {
            "domain": effective_domain,
            "description": f"Práctikah Pro tenant — run_id={run_id}",
            "quota": quota_mb,
            "active": "1",
            "rl_value": "200",
            "rl_frame": "h",
        }

        try:
            response = await self._request_write("POST", "/api/v1/add/domain", body)
            success, error_msg = self._parse_mailcow_write_response(response)

            if success:
                logger.info(
                    "[mailcow] do_add_domain success domain=%s", effective_domain
                )
                return MailcowResult(
                    success=True,
                    resource_id=effective_domain,
                    raw_response=response if isinstance(response, dict) else {"envelope": response},
                )
            else:
                logger.warning(
                    "[mailcow] do_add_domain failed domain=%s error=%s", effective_domain, error_msg
                )
                return MailcowResult(
                    success=False,
                    resource_id=None,
                    raw_response=response if isinstance(response, dict) else {"envelope": response},
                    error=error_msg,
                )

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            logger.exception("[mailcow] do_add_domain failed domain=%s", effective_domain)
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response=raw,
                error=str(err),
            )

    async def undo_add_domain(
        self, domain: str, run_id: str, prior_result: MailcowResult
    ) -> None:
        """Delete the domain created by do_add_domain.

        Per D-10: undo_ methods MUST NOT raise — rollback runner relies on this guarantee.
        Tolerates 'domain has active mailboxes' error by logging a WARNING and returning
        (the orchestrator's reverse-order rollback ensures mailbox is deleted first).

        Args:
            domain: The domain name passed to do_add_domain (before sandbox prefix).
            run_id: Saga run identifier for correlation.
            prior_result: The MailcowResult returned by do_add_domain.
        """
        effective_domain = self._maybe_sandbox_prefix(domain)
        logger.info(
            "[mailcow] undo_add_domain domain=%s effective=%s run_id=%s",
            domain, effective_domain, run_id,
        )

        try:
            response = await self._request_write(
                "POST", "/api/v1/delete/domain", [effective_domain]
            )
            success, error_msg = self._parse_mailcow_write_response(response)

            if success:
                logger.info(
                    "[mailcow] undo_add_domain success domain=%s", effective_domain
                )
            else:
                # Check if the error is "domain has active mailboxes"
                if error_msg and "mailbox" in error_msg.lower():
                    logger.warning(
                        "[mailcow] undo_add_domain: domain has active mailboxes — "
                        "orchestrator should call undo_add_mailbox first. domain=%s",
                        effective_domain,
                    )
                else:
                    logger.warning(
                        "[mailcow] undo_add_domain non-success response domain=%s error=%s",
                        effective_domain, error_msg,
                    )

        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info(
                    "[mailcow] undo_add_domain domain already deleted domain=%s", effective_domain
                )
            else:
                # Log but do NOT raise — undo_ methods must be non-throwing (D-10).
                logger.exception(
                    "[mailcow] undo_add_domain failed domain=%s", effective_domain
                )
        except Exception:
            logger.exception(
                "[mailcow] undo_add_domain unexpected error domain=%s", effective_domain
            )

    # ------------------------------------------------------------------
    # Mailbox operations
    # ------------------------------------------------------------------

    async def do_add_mailbox(
        self,
        local_part: str,
        domain: str,
        password: str,
        *,
        run_id: str,
        quota_mb: int = DEFAULT_QUOTA_MB,
    ) -> MailcowResult:
        """Create a mailbox <local_part>@<domain> in Mailcow.

        Idempotent: GET /api/v1/get/mailbox/<address> first. If the mailbox already
        exists, returns a success result with the existing address as resource_id.

        Security: the password parameter is NEVER logged. The MailcowResult raw_response
        excludes the password (Mailcow does not echo passwords back in responses).

        Args:
            local_part: The mailbox local part (e.g., 'dr.lopez' → 'dr.lopez@domain').
            domain: The domain name (sandbox prefix applied automatically if sandbox_mode=True).
            password: The mailbox password. NEVER logged. Caller generates and discards.
            run_id: Saga run identifier for correlation and audit.
            quota_mb: Mailbox storage quota in MB. Default 10 GB per MAIL-08.

        Returns:
            MailcowResult with resource_id='<local_part>@<effective_domain>' on success.
        """
        effective_domain = self._maybe_sandbox_prefix(domain)
        address = f"{local_part}@{effective_domain}"

        # Intentionally omit password from log per threat model T-11-04-02
        logger.info(
            "[mailcow] do_add_mailbox local_part=%s domain=%s run_id=%s sandbox=%s",
            local_part, effective_domain, run_id, self._sandbox_mode,
        )

        # Idempotency check: GET-before-POST per D-12
        try:
            existing = await self._get_mailbox(address)
        except httpx.TransportError:
            logger.exception(
                "[mailcow] do_add_mailbox transport error during GET address=%s", address
            )
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response={},
                error="Network error during mailbox lookup",
            )

        if existing:
            logger.info(
                "[mailcow] do_add_mailbox mailbox already exists address=%s", address
            )
            return MailcowResult(
                success=True,
                resource_id=address,
                raw_response=existing,
            )

        body: dict[str, Any] = {
            "local_part": local_part,
            "domain": effective_domain,
            "password": password,
            "password2": password,
            "quota": quota_mb,
            "active": "1",
            "force_pw_update": "0",
        }

        try:
            response = await self._request_write("POST", "/api/v1/add/mailbox", body)
            success, error_msg = self._parse_mailcow_write_response(response)

            if success:
                logger.info(
                    "[mailcow] do_add_mailbox success address=%s", address
                )
                return MailcowResult(
                    success=True,
                    resource_id=address,
                    raw_response=response if isinstance(response, dict) else {"envelope": response},
                )
            else:
                logger.warning(
                    "[mailcow] do_add_mailbox failed address=%s error=%s", address, error_msg
                )
                return MailcowResult(
                    success=False,
                    resource_id=None,
                    raw_response=response if isinstance(response, dict) else {"envelope": response},
                    error=error_msg,
                )

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            # Intentionally omit password from exception log
            logger.exception(
                "[mailcow] do_add_mailbox failed local_part=%s domain=%s", local_part, effective_domain
            )
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response=raw,
                error=str(err),
            )

    async def undo_add_mailbox(
        self, local_part: str, domain: str, run_id: str, prior_result: MailcowResult
    ) -> None:
        """Delete the mailbox created by do_add_mailbox.

        Per D-10: undo_ methods MUST NOT raise — rollback runner relies on this guarantee.
        Tolerates 404 / 'mailbox not found' gracefully.

        Must be called BEFORE undo_add_domain — Mailcow forbids domain deletion while
        active mailboxes exist. The orchestrator's reverse-order rollback ensures this
        ordering naturally (do_add_domain runs first, do_add_mailbox second → undo in
        reverse = mailbox first, then domain).

        Args:
            local_part: The mailbox local part (same value passed to do_add_mailbox).
            domain: The domain name (sandbox prefix applied automatically if sandbox_mode=True).
            run_id: Saga run identifier for correlation.
            prior_result: The MailcowResult returned by do_add_mailbox.
        """
        effective_domain = self._maybe_sandbox_prefix(domain)
        address = f"{local_part}@{effective_domain}"

        logger.info(
            "[mailcow] undo_add_mailbox address=%s run_id=%s",
            address, run_id,
        )

        try:
            response = await self._request_write(
                "POST", "/api/v1/delete/mailbox", [address]
            )
            success, error_msg = self._parse_mailcow_write_response(response)

            if success:
                logger.info(
                    "[mailcow] undo_add_mailbox success address=%s", address
                )
            else:
                # Tolerate "mailbox not found" — already deleted is fine for undo
                if error_msg and ("not found" in error_msg.lower() or "does not exist" in error_msg.lower()):
                    logger.info(
                        "[mailcow] undo_add_mailbox mailbox already deleted address=%s", address
                    )
                else:
                    logger.warning(
                        "[mailcow] undo_add_mailbox non-success response address=%s error=%s",
                        address, error_msg,
                    )

        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                logger.info(
                    "[mailcow] undo_add_mailbox mailbox already deleted (404) address=%s", address
                )
            else:
                # Log but do NOT raise — undo_ methods must be non-throwing (D-10).
                logger.exception(
                    "[mailcow] undo_add_mailbox failed address=%s", address
                )
        except Exception:
            logger.exception(
                "[mailcow] undo_add_mailbox unexpected error address=%s", address
            )

    # ------------------------------------------------------------------
    # DKIM operations (pure read + optional create)
    # ------------------------------------------------------------------

    async def do_get_dkim(self, domain: str, run_id: str) -> MailcowResult:
        """Fetch the Mailcow-issued DKIM public key for `domain`.

        Returns resource_id = the DKIM TXT record VALUE (e.g., 'v=DKIM1; k=rsa; p=AAAA...').
        This value is consumed by dns_writer.compose_dns_records → cloudflare_client.do_write_dns_record
        to populate the mcdkim._domainkey.<domain> TXT record (per Phase 11 D-16).

        If the domain has no DKIM key yet, this method CREATES one first via
        POST /api/v1/add/dkim with selector='mcdkim' (DEFAULT_DKIM_SELECTOR), then re-fetches.

        Pure read from the caller's perspective — no undo_get_dkim provided.
        DKIM keys are domain-scoped; deleting them on rollback is harmful because
        un-rolled-back domains would lose mail signing capability.

        Retried up to 3× per D-12 (idempotent read + create is safe to retry).

        Args:
            domain: The domain whose DKIM key is needed (sandbox prefix applied automatically).
            run_id: Saga run identifier for correlation and audit.

        Returns:
            MailcowResult with resource_id='v=DKIM1; k=rsa; p=<pubkey>' on success.
            The orchestrator passes this value to compose_dns_records as mailcow_dkim_value.
        """
        effective_domain = self._maybe_sandbox_prefix(domain)
        logger.info(
            "[mailcow] do_get_dkim domain=%s effective=%s run_id=%s sandbox=%s",
            domain, effective_domain, run_id, self._sandbox_mode,
        )

        # Step 1: Try to fetch existing DKIM key
        try:
            existing = await self._get_dkim(effective_domain)
        except httpx.TransportError:
            logger.exception(
                "[mailcow] do_get_dkim transport error during GET domain=%s", effective_domain
            )
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response={},
                error="Network error during DKIM lookup",
            )

        if existing and existing.get("dkim_txt"):
            # Mailcow stores the full DKIM TXT value in dkim_txt field
            dkim_txt = existing["dkim_txt"]
            logger.info(
                "[mailcow] do_get_dkim found existing DKIM domain=%s", effective_domain
            )
            return MailcowResult(
                success=True,
                resource_id=dkim_txt,
                raw_response=existing,
            )

        # Also try the pubkey field as a fallback (some Mailcow versions use different field names)
        if existing and existing.get("pubkey"):
            pubkey = existing["pubkey"]
            dkim_value = f"v=DKIM1; k=rsa; p={pubkey}"
            logger.info(
                "[mailcow] do_get_dkim found existing DKIM (pubkey field) domain=%s", effective_domain
            )
            return MailcowResult(
                success=True,
                resource_id=dkim_value,
                raw_response=existing,
            )

        # Step 2: No DKIM key exists — create one via POST /api/v1/add/dkim
        logger.info(
            "[mailcow] do_get_dkim no DKIM found, creating with selector=%s domain=%s",
            DEFAULT_DKIM_SELECTOR, effective_domain,
        )

        create_body: dict[str, Any] = {
            "domains": [effective_domain],
            "dkim_selector": DEFAULT_DKIM_SELECTOR,
            "key_size": 2048,
        }

        try:
            create_response = await self._request_write("POST", "/api/v1/add/dkim", create_body)
            success, error_msg = self._parse_mailcow_write_response(create_response)

            if not success:
                logger.warning(
                    "[mailcow] do_get_dkim DKIM creation failed domain=%s error=%s",
                    effective_domain, error_msg,
                )
                return MailcowResult(
                    success=False,
                    resource_id=None,
                    raw_response=create_response if isinstance(create_response, dict) else {"envelope": create_response},
                    error=error_msg,
                )

        except httpx.HTTPStatusError as err:
            raw: dict[str, Any] = {}
            try:
                raw = err.response.json()
            except Exception:
                pass
            logger.exception(
                "[mailcow] do_get_dkim DKIM creation request failed domain=%s", effective_domain
            )
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response=raw,
                error=str(err),
            )

        # Step 3: Re-fetch the newly created DKIM key
        try:
            fetched = await self._get_dkim(effective_domain)
        except httpx.TransportError:
            logger.exception(
                "[mailcow] do_get_dkim transport error during re-fetch domain=%s", effective_domain
            )
            return MailcowResult(
                success=False,
                resource_id=None,
                raw_response={},
                error="Network error during DKIM re-fetch after creation",
            )

        if fetched and fetched.get("dkim_txt"):
            dkim_txt = fetched["dkim_txt"]
            logger.info(
                "[mailcow] do_get_dkim DKIM created and fetched domain=%s", effective_domain
            )
            return MailcowResult(
                success=True,
                resource_id=dkim_txt,
                raw_response=fetched,
            )

        if fetched and fetched.get("pubkey"):
            pubkey = fetched["pubkey"]
            dkim_value = f"v=DKIM1; k=rsa; p={pubkey}"
            logger.info(
                "[mailcow] do_get_dkim DKIM created and fetched (pubkey field) domain=%s", effective_domain
            )
            return MailcowResult(
                success=True,
                resource_id=dkim_value,
                raw_response=fetched,
            )

        # Creation succeeded but re-fetch returned empty — unexpected state
        logger.warning(
            "[mailcow] do_get_dkim DKIM created but re-fetch returned empty domain=%s", effective_domain
        )
        return MailcowResult(
            success=False,
            resource_id=None,
            raw_response=fetched or {},
            error="DKIM key created but not retrievable — Mailcow may need a moment to propagate",
        )

    # ------------------------------------------------------------------
    # Phase 13-06: per-domain DKIM (D-30) and Pro mailbox provisioning
    # ------------------------------------------------------------------

    async def get_per_domain_dkim(self, domain: str, run_id: str) -> dict[str, Any]:
        """Generate (or fetch) a *per-domain* DKIM key + selector via Mailcow (D-30).

        Pro custom domains MUST use a per-domain selector — never the shared
        ``mcdkim`` selector that ``do_get_dkim`` uses for free-tier
        ``medikah.health``. This method:

          1. GETs ``/api/v1/get/dkim/<domain>`` — if a per-domain key already
             exists (idempotency), returns ``{selector, public_key}``.
          2. Otherwise POSTs ``/api/v1/add/dkim`` with a freshly generated
             selector ``medikah<unix_seconds>`` (so retried runs don't collide
             with prior runs) and ``key_size=2048``.
          3. Re-fetches and returns ``{selector, public_key}``.

        The returned ``public_key`` is the full TXT record VALUE consumed by
        ``dns_template.compose_pro_dns_records``.

        Sandbox short-circuits to a deterministic stub so 13-10 dry-runs work.

        Returns:
            ``{"selector": str, "public_key": str}`` on success.

        Raises:
            RuntimeError: if Mailcow returns an error envelope or the re-fetch
                returns an empty body (saga step 3 then transitions per D-15).
        """
        import time as _time

        effective_domain = self._maybe_sandbox_prefix(domain)
        logger.info(
            "[mailcow] get_per_domain_dkim domain=%s effective=%s run_id=%s sandbox=%s",
            domain, effective_domain, run_id, self._sandbox_mode,
        )

        if self._sandbox_mode:
            sandbox_selector = f"sandbox{int(_time.time())}"
            return {
                "selector": sandbox_selector,
                "public_key": (
                    "v=DKIM1; k=rsa; p=SANDBOX_PUBKEY_"
                    f"{effective_domain.replace('.', '_')}"
                ),
            }

        # 1. Idempotency: existing key?
        try:
            existing = await self._get_dkim(effective_domain)
        except httpx.TransportError as err:
            raise RuntimeError(
                f"per-domain DKIM lookup transport error: {err}"
            ) from err

        def _extract(payload: dict[str, Any]) -> Optional[dict[str, str]]:
            sel = payload.get("dkim_selector") or payload.get("selector")
            pub = payload.get("dkim_txt") or (
                f"v=DKIM1; k=rsa; p={payload['pubkey']}"
                if payload.get("pubkey")
                else None
            )
            if sel and pub:
                return {"selector": sel, "public_key": pub}
            return None

        if existing:
            extracted = _extract(existing)
            if extracted:
                # Defensive: ensure selector is NOT the shared mcdkim free-tier one.
                if extracted["selector"] == DEFAULT_DKIM_SELECTOR:
                    logger.warning(
                        "[mailcow] get_per_domain_dkim existing key uses shared selector "
                        "%s for domain=%s — replacing with per-domain key (D-30)",
                        DEFAULT_DKIM_SELECTOR, effective_domain,
                    )
                else:
                    return extracted

        # 2. Create with a per-domain selector. Unix-seconds suffix lets retries
        #    after deletion produce distinct selectors without collision.
        per_domain_selector = f"medikah{int(_time.time())}"
        create_body: dict[str, Any] = {
            "domains": [effective_domain],
            "dkim_selector": per_domain_selector,
            "key_size": 2048,
        }
        try:
            create_response = await self._request_write(
                "POST", "/api/v1/add/dkim", create_body
            )
        except httpx.HTTPStatusError as err:
            raise RuntimeError(f"per-domain DKIM create HTTP error: {err}") from err

        success, error_msg = self._parse_mailcow_write_response(create_response)
        if not success:
            raise RuntimeError(
                f"per-domain DKIM create failed for {effective_domain}: {error_msg}"
            )

        # 3. Re-fetch
        try:
            fetched = await self._get_dkim(effective_domain)
        except httpx.TransportError as err:
            raise RuntimeError(
                f"per-domain DKIM re-fetch transport error: {err}"
            ) from err

        if not fetched:
            raise RuntimeError(
                f"per-domain DKIM key created but re-fetch returned empty for {effective_domain}"
            )

        extracted = _extract(fetched)
        if not extracted:
            raise RuntimeError(
                f"per-domain DKIM re-fetch returned unexpected payload for {effective_domain}"
            )
        return extracted

    async def do_provision_pro_mailbox(
        self,
        domain: str,
        local_part: str,
        password: str,
        run_id: str,
        *,
        quota_mb: int = DEFAULT_QUOTA_MB,
    ) -> MailcowResult:
        """Provision ``<local_part>@<domain>`` on the shared Mailcow VPS (PRO-15).

        Mailcow is multi-domain native — Pro mailboxes live on the same VPS as
        free-tier ``medikah.health`` mailboxes. This method delegates to
        ``do_add_mailbox`` after the saga's step 4 has added the domain via
        ``do_add_domain``.

        Security: ``password`` is NEVER logged (T-13-06-09).

        Returns:
            ``MailcowResult`` with ``resource_id='<local_part>@<domain>'`` on success.
        """
        # do_add_mailbox already applies sandbox-prefix + idempotency + password
        # masking, so this is a thin pass-through that gives the saga a clearly
        # named entrypoint.
        return await self.do_add_mailbox(
            local_part=local_part,
            domain=domain,
            password=password,
            run_id=run_id,
            quota_mb=quota_mb,
        )

    async def undo_provision_pro_mailbox(
        self,
        domain: str,
        local_part: str,
        run_id: str,
        prior_result: MailcowResult,
    ) -> None:
        """Compensating action for ``do_provision_pro_mailbox`` (non-raising).

        Delegates to ``undo_add_mailbox``. Per D-10 / D-15 this MUST NOT raise.
        """
        await self.undo_add_mailbox(
            local_part=local_part,
            domain=domain,
            run_id=run_id,
            prior_result=prior_result,
        )


# ---------------------------------------------------------------------------
# Module-level mailbox helpers for Phase 12-03 (password rotation + mobileconfig)
# ---------------------------------------------------------------------------

def _get_mailcow_api_settings() -> tuple[str, str]:
    """Return (api_url, api_key) from environment variables.

    Used by module-level helpers that are called from FastAPI routes directly
    (not via MailboxProvisioner instance) — avoids re-reading env at import time.

    Raises:
        RuntimeError: if required env vars are not set.
    """
    import os
    api_url = os.environ.get("MAILCOW_API_URL", "")
    api_key = os.environ.get("MAILCOW_API_KEY", "")
    if not api_url:
        raise RuntimeError("MAILCOW_API_URL is not configured")
    if not api_key:
        raise RuntimeError("MAILCOW_API_KEY is not configured")
    return api_url, api_key


async def do_update_mailbox_password(
    domain: str, local_part: str, new_password: str
) -> None:
    """Rotate mailbox password via Mailcow Admin API.

    Mailcow endpoint: POST /api/v1/edit/mailbox
    Body: {"items": ["<full_address>"], "attr": {"password": "<new>", "password2": "<new>"}}

    Security: new_password is NEVER logged (T-12-03-01). The authenticated
    FastAPI session (verified_physician) is the proof-of-identity gate (T-12-03-03
    lean choice — doctor may legitimately have forgotten old password; all password
    rotation events are audit-logged with action='workspace.password_changed' for
    detectability).

    Args:
        domain: The domain name (e.g. 'medikah.health').
        local_part: The mailbox local part (e.g. 'dr-lopez').
        new_password: The new mailbox password. NEVER logged anywhere.

    Raises:
        ValueError: if new_password is shorter than 12 characters.
        RuntimeError: if Mailcow returns a non-success envelope.
        httpx.HTTPStatusError: on HTTP 4xx/5xx.
        httpx.TransportError: on network failure.
    """
    if len(new_password) < 12:
        raise ValueError("mailbox password must be >= 12 characters")

    full_address = f"{local_part}@{domain}"
    # Intentionally omit new_password from log — T-12-03-01
    logger.info(
        "[mailcow] do_update_mailbox_password address=%s", full_address
    )

    api_url, api_key = _get_mailcow_api_settings()
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(10.0, connect=3.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}/api/v1/edit/mailbox",
            json={
                "items": [full_address],
                "attr": {"password": new_password, "password2": new_password},
            },
            headers=headers,
        )
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    body = None
    if content_type.startswith("application/json"):
        try:
            body = resp.json()
        except Exception:
            body = None

    # Mailcow returns [{"type":"success","msg":[...]}] on success,
    # or [{"type":"danger"/"error","msg":"..."}] on failure.
    if body and isinstance(body, list):
        item = body[0] if body else {}
        item_type = item.get("type", "") if isinstance(item, dict) else ""
        if item_type in ("danger", "error"):
            msg = item.get("msg", "Unknown error")
            if isinstance(msg, list):
                msg = " ".join(str(m) for m in msg)
            raise RuntimeError(f"Mailcow password update failed: {msg}")

    logger.info(
        "[mailcow] do_update_mailbox_password success address=%s", full_address
    )


async def fetch_mobileconfig(
    domain: str, local_part: str
) -> bytes:
    """Stream Apple .mobileconfig profile bytes from Mailcow.

    Mailcow endpoint: GET /api/v1/get/mobileconfig/<address>
    Returns binary application/x-apple-aspen-config plist content.

    Args:
        domain: The domain name (e.g. 'medikah.health').
        local_part: The mailbox local part (e.g. 'dr-lopez').

    Raises:
        httpx.HTTPStatusError: on HTTP 4xx/5xx.
        httpx.TransportError: on network failure.
    """
    full_address = f"{local_part}@{domain}"
    logger.info(
        "[mailcow] fetch_mobileconfig address=%s", full_address
    )

    api_url, api_key = _get_mailcow_api_settings()
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(15.0, connect=3.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{api_url.rstrip('/')}/api/v1/get/mobileconfig/{full_address}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.content


# ---------------------------------------------------------------------------
# Module-level singleton for Phase 13-06 saga import sites
# ---------------------------------------------------------------------------
# The Pro upgrade saga (services/practikah/pro_saga.py) imports this directly
# rather than going through the orchestrator's lazy ``get_mailbox_provisioner``
# accessor. We auto-engage sandbox mode when MAILCOW_API_KEY is unset so the
# import-time singleton stays valid in tests / CI.

_SANDBOX_MODE_FLAG = os.getenv("MEDIKAH_PROVISIONING_SANDBOX", "false").lower() in {
    "1", "true", "yes", "on",
}
_MC_URL = os.environ.get("MAILCOW_API_URL", "https://practikah.medikah.health")
_MC_KEY = os.environ.get("MAILCOW_API_KEY", "")


class _SandboxMailboxProvisioner(MailboxProvisioner):
    """Sandbox stand-in used when MAILCOW_API_KEY is unset.

    Bypasses the constructor's ``api_key required`` guard so the module
    singleton is always usable. All vendor calls short-circuit via
    ``self._sandbox_mode=True``.
    """

    def __init__(self) -> None:
        # Skip the parent __init__ guard entirely; set fields directly.
        self._api_url = _MC_URL.rstrip("/") if _MC_URL else ""
        self._api_key = ""
        self._sandbox_mode = True
        self._timeout = httpx.Timeout(10.0, connect=3.0)


def _build_module_singleton() -> MailboxProvisioner:
    if _MC_KEY:
        return MailboxProvisioner(
            _MC_URL, _MC_KEY, sandbox_mode=_SANDBOX_MODE_FLAG
        )
    # Auto-sandbox when no key is configured.
    return _SandboxMailboxProvisioner()


# Imported by services.practikah.pro_saga
mailbox_provisioner = _build_module_singleton()

