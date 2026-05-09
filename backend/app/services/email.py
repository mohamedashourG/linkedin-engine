"""
Resend wrapper. `send_email` accepts a single string OR a list of addresses; it
sends one Resend request with all recipients in `to[]`.
"""
from __future__ import annotations

import logging

import resend

from app.config import settings

log = logging.getLogger(__name__)


class EmailNotConfigured(RuntimeError):
    pass


def send_email(
    *,
    to: str | list[str],
    subject: str,
    html: str,
    text: str | None = None,
) -> str:
    if not settings.resend_api_key:
        raise EmailNotConfigured("RESEND_API_KEY is not set.")
    resend.api_key = settings.resend_api_key
    if isinstance(to, str):
        recipients = [to]
    else:
        # Dedupe while preserving order.
        seen: set[str] = set()
        recipients = []
        for r in to:
            r = (r or "").strip().lower()
            if r and r not in seen:
                seen.add(r)
                recipients.append(r)
    if not recipients:
        raise EmailNotConfigured("send_email called with no recipients")
    payload: dict = {
        "from": settings.resend_from_email,
        "to": recipients,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    response = resend.Emails.send(payload)
    msg_id = response.get("id") if isinstance(response, dict) else None
    log.info("resend sent to=%s id=%s", recipients, msg_id)
    return msg_id or ""
