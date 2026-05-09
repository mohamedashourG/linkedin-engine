"""
Calendly webhook ingest.

Endpoint: `POST /api/webhooks/calendly?operator_id=<id>` — Calendly's HTTP request
payload is signed with HMAC over `<unix_ts>.<raw_body>` using the operator's
signing key. The operator_id is on the URL because Calendly doesn't surface it
in the payload.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.models.common import utcnow
from app.services.attribution import attribute_booking
from app.services.calendly import (
    CalendlySignatureError,
    parse_invitee,
    verify_signature,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


@router.post("/calendly", status_code=status.HTTP_200_OK)
async def calendly(
    request: Request,
    operator_id: Annotated[str, Query()],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, Any]:
    if not ObjectId.is_valid(operator_id):
        raise HTTPException(400, "invalid operator_id")
    op = await db.users.find_one({"_id": ObjectId(operator_id)})
    if not op:
        raise HTTPException(404, "operator not found")

    raw_body = await request.body()
    sig_header = request.headers.get("calendly-webhook-signature")
    try:
        verify_signature(
            signing_key=op.get("calendly_webhook_signing_key"),
            header_value=sig_header,
            raw_body=raw_body,
        )
    except CalendlySignatureError as err:
        log.warning("calendly webhook signature rejected: %s", err)
        raise HTTPException(401, str(err))

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON")

    event_type = payload.get("event") or payload.get("event_type") or ""
    if "invitee.created" not in event_type and "invitee_created" not in event_type:
        # Acknowledge other events (cancellations etc.) without attributing.
        return {"status": "ignored", "event_type": event_type}

    info = parse_invitee(payload)
    booking_doc = {
        "operator_id": ObjectId(operator_id),
        "cofounder_id": None,
        "lead_id": None,
        "candidate_id": None,
        "invitee_name": info.get("name"),
        "invitee_email": info.get("email"),
        "invitee_linkedin_url": info.get("linkedin_url"),
        "booked_at": utcnow(),
        "meeting_at": _parse_iso(info.get("meeting_at")),
        "attribution_status": "unattributed",
        "attribution_confidence": 0.0,
        "attribution_method": None,
        "raw_calendly_payload": payload,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    result = await db.bookings.insert_one(booking_doc)
    booking_id = result.inserted_id

    attribution = await attribute_booking(
        db, operator_id=ObjectId(operator_id), booking_id=booking_id
    )
    return {"status": "ok", "booking_id": str(booking_id), "attribution": attribution}
