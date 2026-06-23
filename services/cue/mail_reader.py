"""
services/cue/mail_reader.py
-----------------------------
Cue READ-ONLY IMAP client over the doctor's OWN inbox (HANDS-02).

read_recent(...) fetches recent message HEADERS only, newest-first, capped — to
give Cue lightweight inbox context. It is strictly read-only:

  - mark_seen=False  : NEVER mutates the IMAP \\Seen flag on the doctor's inbox.
  - headers_only=True: the full body is never transferred over the wire or into
                       Python memory; only subject/from_/date/uid are read.
  - bodies are TRANSIENT: the return value carries NO body/text/html/payload —
                          message content is never persisted (HANDS-02).

Credential discipline: the MailBox is built PER REQUEST from the Cue
app-password (username/password) handed out by credential_broker.get_cue_cred.
The credential is never stored at module level. TLS on port 993 only — there is
no plaintext path (imap-tools MailBox uses implicit SSL/TLS on 993).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# IMAP host/port (HANDS-06 apex; 993 TLS — same host as the SOGo CalDAV base).
IMAP_HOST = os.environ.get("CUE_IMAP_HOST", "practikah.medikah.health")
IMAP_PORT = int(os.environ.get("CUE_IMAP_PORT", "993"))


def read_recent(
    username: str,
    password: str,
    limit: int = 10,
    since_days: int = 7,
) -> list[dict]:
    """Read recent inbox message HEADERS, read-only (HANDS-02).

    Returns up to `limit` summary dicts ({subject, from_, date, uid}), newest
    first, from the last `since_days` days. mark_seen=False + headers_only=True
    guarantee no \\Seen mutation and no body transfer. Bodies are transient and
    NEVER appear in the return value.

    SYNCHRONOUS: imap-tools MailBox is a blocking, synchronous client — there is
    no async variant. The async executor (inbox_read_recent) offloads this call
    to a worker thread (asyncio.to_thread) so it never blocks the event loop.

    Imports imap_tools lazily so the module imports cleanly in minimal test
    environments and so tests can patch imap_tools.MailBox.
    """
    import imap_tools

    since = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
    criteria = imap_tools.AND(date_gte=since.date())

    results: list[dict] = []
    # MailBox() uses implicit SSL/TLS on port 993 (no plaintext path).
    mailbox = imap_tools.MailBox(IMAP_HOST, port=IMAP_PORT).login(username, password)
    with mailbox as mb:
        for msg in mb.fetch(
            criteria,
            mark_seen=False,    # HANDS-02: NEVER mutate the IMAP \Seen flag
            limit=limit,
            reverse=True,       # newest first
            headers_only=True,  # subject/from only — bodies never transferred
        ):
            results.append(
                {
                    "subject": msg.subject,
                    "from_": msg.from_,
                    "date": str(msg.date),
                    "uid": msg.uid,
                    # NOTE: no body/text/html/payload — bodies are transient (HANDS-02).
                }
            )
    return results
