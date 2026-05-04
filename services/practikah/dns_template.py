"""Versioned DNS record template for Práctikah Pro custom domains (Phase 13-06).

Per D-30: per-domain DKIM selector + keypair (NEVER the shared 'mcdkim' selector).
Per D-31: DMARC starts at p=none; ramp to quarantine/reject is Phase 14 (MAIL-13).
Per D-32: TEMPLATE_VERSION must be bumped on every template change so Phase 14's
DNS drift monitor can compare published records against the version that wrote them.

This module is pure composition — no HTTP. The orchestrator passes the records
list to ``cloudflare_client.do_write_dns_record`` for actual publication.

Phase 11's ``dns_writer.compose_dns_records`` remains the free-tier / shared-DKIM
template; this module is the Pro-tier per-domain replacement and they coexist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

# D-32: bump on every template change so Phase 14's drift monitor knows which
# version of the template wrote which set of records. Stored alongside published
# records in practikah_provisioning_log.detail.template_version.
TEMPLATE_VERSION = "v13.1"


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """One DNS record to be written via the Cloudflare API.

    ``type`` is restricted to the record types the Pro template emits. Adding
    new types means bumping ``TEMPLATE_VERSION`` so drift monitoring stays
    correct.
    """

    type: Literal["A", "CNAME", "MX", "TXT", "SRV"]
    name: str
    content: str
    priority: Optional[int] = None  # MX / SRV only
    ttl: int = 3600


def compose_pro_dns_records(
    domain: str,
    mailcow_a_record: str,
    website_a_record: str,
    spf_value: str,
    dkim_selector: str,
    dkim_public_key: str,
    dmarc_value: str = "v=DMARC1; p=none; rua=mailto:dmarc-reports@medikah.health",
) -> List[DnsRecord]:
    """Return the canonical per-domain DNS record set for a Pro-tier domain.

    Per D-30: ``dkim_selector`` is the per-domain selector returned by Mailcow
    when the DKIM key is generated for *this* domain. It MUST NOT be the
    shared 'mcdkim' selector that the free tier uses.

    Per D-31: ``dmarc_value`` defaults to ``p=none`` so we accumulate aggregate
    reports without breaking inbound mail at any third-party that has lazy
    SPF/DKIM alignment. Phase 14 (MAIL-13) ramps to ``p=quarantine`` then
    ``p=reject`` once the report-based confidence interval is high.

    Args:
        domain: Custom domain (e.g. ``drlopez.com``).
        mailcow_a_record: Public IPv4 of the Mailcow VPS — used for the
            ``mail.<domain>`` A record so SOGo / IMAP can reach the doctor's
            vanity host.
        website_a_record: Cloudflare for SaaS fallback A record IP — points
            at the CF edge that proxies the doctor's published website.
        spf_value: SPF record value (e.g. ``v=spf1 a mx include:_spf.resend.com ~all``).
        dkim_selector: Per-domain DKIM selector returned by Mailcow.
        dkim_public_key: DKIM TXT record value (e.g. ``v=DKIM1; k=rsa; p=...``).
        dmarc_value: DMARC TXT record value. Defaults to p=none per D-31.

    Returns:
        Eight ``DnsRecord`` instances covering apex web, www, mail vanity host,
        MX routing, SPF, per-domain DKIM, DMARC, and a forward-compatible
        CalDAV SRV record (Phase 14 surface for the calendar feature).
    """
    return [
        # Apex website — A record points at CF for SaaS edge fallback.
        DnsRecord(type="A", name=domain, content=website_a_record),
        # www → apex (DV cert covers both via SAN list on CF for SaaS).
        DnsRecord(type="CNAME", name=f"www.{domain}", content=domain),
        # Mail vanity host — A record at mail.<domain> points at the Mailcow VPS.
        DnsRecord(type="A", name=f"mail.{domain}", content=mailcow_a_record),
        # MX routes inbound mail to the vanity host above.
        DnsRecord(type="MX", name=domain, content=f"mail.{domain}", priority=10),
        # SPF — single TXT at apex.
        DnsRecord(type="TXT", name=domain, content=spf_value),
        # DKIM — per-domain selector per D-30 (NEVER the shared mcdkim selector).
        DnsRecord(
            type="TXT",
            name=f"{dkim_selector}._domainkey.{domain}",
            content=dkim_public_key,
        ),
        # DMARC — p=none initially per D-31; Phase 14 ramps the policy.
        DnsRecord(type="TXT", name=f"_dmarc.{domain}", content=dmarc_value),
        # CalDAV SRV — forward-compat for Phase 14 calendar surface.
        DnsRecord(
            type="SRV",
            name=f"_caldav._tcp.{domain}",
            content=f"0 0 443 mail.{domain}",
            priority=0,
        ),
    ]
