"""Pure DNS record composer for Práctikah Pro tenant domains.

Per Phase 11 D-16 (Resend-as-relay topology, NOT SES-direct):
- MX → practikah.medikah.health (Mailcow VPS)
- A for mail.<domain> → Vultr VPS public IP (vanity host, optional but standard)
- SPF: include:_spf.resend.com (Resend handles outbound)
- DKIM: dual — Resend (resend._domainkey) + Mailcow (mcdkim._domainkey)
- DMARC: p=none initially; ramp to quarantine/reject is Phase 14 (MAIL-13)
- PTR is NOT per-domain — points once at practikah.medikah.health on the Vultr VPS.

No HTTP calls; pure composition. Cloudflare writes the records (cloudflare_client.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """A single DNS record to be written via the Cloudflare API."""

    record_type: str   # 'A' | 'MX' | 'TXT' | 'CNAME'
    name: str          # '@' | 'mail.<domain>' | '_dmarc' | 'resend._domainkey' | 'mcdkim._domainkey'
    value: str
    priority: Optional[int] = None  # MX only
    ttl: int = 3600


def compose_dns_records(
    domain: str,
    *,
    mailcow_host: str,
    mailcow_vps_ip: str,
    resend_dkim_value: str,
    mailcow_dkim_value: str,
    dmarc_rua: str = 'mailto:dmarc-reports@medikah.health',
) -> List[DnsRecord]:
    """Return the 6-record DNS set for a Pro-tier domain per Phase 11 D-16.

    Args:
        domain:             The Pro physician's custom domain (e.g. 'drsmith.com').
        mailcow_host:       Shared Mailcow VPS hostname, always 'practikah.medikah.health'.
        mailcow_vps_ip:     Vultr VPS public IP — used for the mail.<domain> A record so
                            the doctor can configure IMAP/SOGo with their vanity hostname.
        resend_dkim_value:  DKIM public key value fetched from Resend API per domain.
        mailcow_dkim_value: DKIM public key value fetched from the Mailcow getDKIM API.
        dmarc_rua:          DMARC aggregate-report recipient address. Phase 14 ramps
                            policy from p=none to p=quarantine/reject (MAIL-13).

    Returns:
        List of 6 DnsRecord instances in the canonical D-16 order.

    Note: PTR (reverse DNS) is set ONCE on the Vultr VPS to point at
    practikah.medikah.health. NOT written per Pro domain. See Phase 11 D-16.
    """
    return [
        DnsRecord('MX',  '@',                     mailcow_host,                         priority=10),
        DnsRecord('A',   f'mail.{domain}',        mailcow_vps_ip),
        DnsRecord('TXT', '@',                     'v=spf1 include:_spf.resend.com -all'),
        DnsRecord('TXT', 'resend._domainkey',     resend_dkim_value),
        DnsRecord('TXT', 'mcdkim._domainkey',     mailcow_dkim_value),
        DnsRecord('TXT', '_dmarc',                f'v=DMARC1; p=none; rua={dmarc_rua}'),
    ]
