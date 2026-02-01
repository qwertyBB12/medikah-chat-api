"""Notification service for appointment scheduling events."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

import resend

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """An email attachment."""

    filename: str
    content: str  # base64-encoded content
    content_type: str = "text/calendar"


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """Payload for an email notification."""

    recipient: str
    subject: str
    plain_body: str
    html_body: Optional[str] = None
    attachments: List[EmailAttachment] = field(default_factory=list)


class NotificationService:
    """Thin wrapper around Resend to send notifications asynchronously."""

    def __init__(
        self, api_key: str, sender_email: str, *, sandbox_mode: bool = False
    ) -> None:
        if not api_key:
            raise ValueError("Resend API key is required for NotificationService")
        if not sender_email:
            raise ValueError("Sender email is required for NotificationService")
        resend.api_key = api_key
        self._sender_email = sender_email
        self._sandbox_mode = sandbox_mode

    async def send_bulk(self, messages: Iterable[NotificationMessage]) -> None:
        """Dispatch a collection of messages concurrently."""
        tasks = [
            asyncio.create_task(self._send_message(message))
            for message in messages
        ]
        if not tasks:
            logger.warning("No notification messages queued for delivery.")
            return
        await asyncio.gather(*tasks)

    async def _send_message(self, message: NotificationMessage) -> None:
        """Send a single message through Resend."""
        if self._sandbox_mode:
            logger.info(
                "[Sandbox] Notification to %s skipped. Subject: %s",
                message.recipient,
                message.subject,
            )
            logger.debug("[Sandbox] Body: %s", message.plain_body)
            return

        params: dict = {
            "from": self._sender_email,
            "to": [message.recipient],
            "subject": message.subject,
            "text": message.plain_body,
        }
        if message.html_body:
            params["html"] = message.html_body
        if message.attachments:
            params["attachments"] = [
                {
                    "filename": att.filename,
                    "content": att.content,
                    "content_type": att.content_type,
                }
                for att in message.attachments
            ]

        logger.info("Sending notification to %s via Resend", message.recipient)
        try:
            response = await asyncio.to_thread(resend.Emails.send, params)
            logger.info(
                "Resend response for %s: %s",
                message.recipient,
                response,
            )
        except Exception:
            logger.exception(
                "Failed to send notification to %s", message.recipient
            )
            raise
