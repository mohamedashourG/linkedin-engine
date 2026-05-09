"""
Resend wrapper. Used by:
  - engine/email_delivery.py: morning slate email
  - engine/reply_monitor.py (Phase 4): reply digest

Returns the message_id on success so the slate_runs / replies records can
audit-trail the send.
"""
from __future__ import annotations

import logging

import resend

from app.config import settings

log = logging.getLogger(__name__)


class EmailNotConfigured(RuntimeError):
    pass


def send_email(
    *, to: str, subject: str, html: str, text: str | None = None
) -> str:
    if not settings.resend_api_key:
        raise EmailNotConfigured("RESEND_API_KEY is not set.")
    resend.api_key = settings.resend_api_key
    payload: dict = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    response = resend.Emails.send(payload)
    msg_id = response.get("id") if isinstance(response, dict) else None
    log.info("resend sent to=%s id=%s", to, msg_id)
    return msg_id or ""
