"""
Calendly webhook signature verification.

Calendly v2 sends `Calendly-Webhook-Signature: t=<unix_ts>,v1=<hex>` where
`hex = HMAC_SHA256(signing_key, f"{t}.{raw_body}")`.

We allow a 5-minute clock skew. If the operator's signing key is unset, we
log a warning and accept the request unconditionally (dev-only escape hatch
for when the user is testing webhooks locally without registering with
Calendly's API).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_SKEW_SECONDS = 300


class CalendlySignatureError(RuntimeError):
    pass


def verify_signature(
    *,
    signing_key: str | None,
    header_value: str | None,
    raw_body: bytes,
) -> bool:
    """
    Returns True on success. Raises CalendlySignatureError on bad signature.
    Returns False (with log warning) when signing_key is empty — caller decides
    whether to accept or reject.
    """
    if not signing_key:
        log.warning("calendly: no signing_key set; accepting webhook unverified")
        return False
    if not header_value:
        raise CalendlySignatureError("missing Calendly-Webhook-Signature header")

    parts = {
        kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip()
        for kv in header_value.split(",")
        if "=" in kv
    }
    timestamp = parts.get("t")
    sig = parts.get("v1")
    if not timestamp or not sig:
        raise CalendlySignatureError("malformed signature header")

    try:
        ts = int(timestamp)
    except ValueError:
        raise CalendlySignatureError("non-integer timestamp")

    now = int(time.time())
    if abs(now - ts) > _SKEW_SECONDS:
        raise CalendlySignatureError(
            f"timestamp out of tolerance: header={ts} now={now}"
        )

    payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(
        signing_key.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise CalendlySignatureError("HMAC mismatch")
    return True


def parse_invitee(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull out the fields we care about from a Calendly v2 payload."""
    inner = payload.get("payload") or {}
    invitee = inner.get("invitee") or inner
    event = inner.get("event") or {}
    questions = inner.get("questions_and_answers") or []

    linkedin_url: str | None = None
    for q in questions:
        question = (q.get("question") or "").lower()
        answer = q.get("answer") or ""
        if "linkedin" in question and answer:
            linkedin_url = answer
            break

    return {
        "name": invitee.get("name"),
        "email": invitee.get("email"),
        "linkedin_url": linkedin_url,
        "meeting_at": event.get("start_time"),
        "event_uri": event.get("uri") or inner.get("uri"),
    }
