"""
Booking → candidate attribution. Runs synchronously inside the webhook handler.

Match strategies (highest confidence first):
  1. exact_url   — invitee LinkedIn URL == author_linkedin_url of a recent
                   shipped candidate.
  2. exact_email — invitee email == candidate.author_email (we don't capture
                   author_email today, so this is a placeholder for when PDL
                   enrichment surfaces it).
  3. fuzzy_url   — slug similarity (last segment of /in/<slug>/ matches).
  4. (manual)    — user reconciles via Pipeline UI.

On match: attaches booking → candidate → lead, advances lead to S7 / CONVERTED.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.common import utcnow

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 14


def _slug(url: str | None) -> str | None:
    if not url or "/in/" not in url:
        return None
    try:
        return url.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0].lower()
    except (IndexError, ValueError):
        return None


async def attribute_booking(
    db: AsyncIOMotorDatabase,
    *,
    operator_id: ObjectId,
    booking_id: ObjectId,
) -> dict[str, Any]:
    booking = await db.bookings.find_one({"_id": booking_id})
    if not booking:
        return {"status": "booking_missing"}

    cutoff = utcnow() - timedelta(days=LOOKBACK_DAYS)
    candidates = await db.candidates.find(
        {
            "operator_id": operator_id,
            "status": "shipped",
            "shipped_at": {"$gte": cutoff},
        }
    ).to_list(length=200)
    if not candidates:
        await _set_no_match(db, booking_id)
        return {"status": "no_match", "reason": "no_recent_shipped"}

    invitee_url = booking.get("invitee_linkedin_url")
    invitee_email = booking.get("invitee_email")
    invitee_slug = _slug(invitee_url)

    # Pass 1 — exact URL
    if invitee_url:
        for c in candidates:
            if (c.get("author_linkedin_url") or "").rstrip("/") == invitee_url.rstrip("/"):
                return await _attribute(
                    db, booking_id, c, method="exact_url", confidence=1.0
                )

    # Pass 2 — exact email (only if candidate carries author_email)
    if invitee_email:
        for c in candidates:
            if (c.get("author_email") or "").lower() == invitee_email.lower():
                return await _attribute(
                    db, booking_id, c, method="exact_email", confidence=1.0
                )

    # Pass 3 — fuzzy URL (slug match)
    if invitee_slug:
        for c in candidates:
            cand_slug = _slug(c.get("author_linkedin_url"))
            if cand_slug and cand_slug == invitee_slug:
                return await _attribute(
                    db, booking_id, c, method="fuzzy_url", confidence=0.85
                )

    await _set_no_match(db, booking_id)
    return {"status": "no_match", "candidates_in_window": len(candidates)}


async def _attribute(
    db: AsyncIOMotorDatabase,
    booking_id: ObjectId,
    candidate: dict[str, Any],
    *,
    method: str,
    confidence: float,
) -> dict[str, Any]:
    now = utcnow()
    operator_id = candidate["operator_id"]
    lead_id = await _resolve_lead_id(db, candidate)

    await db.bookings.update_one(
        {"_id": booking_id},
        {
            "$set": {
                "candidate_id": candidate["_id"],
                "lead_id": lead_id,
                "attribution_status": "attributed",
                "attribution_method": method,
                "attribution_confidence": confidence,
                "updated_at": now,
            }
        },
    )

    if lead_id:
        await db.leads.update_one(
            {"_id": lead_id},
            {
                "$set": {
                    "current_stage": "S7",
                    "status": "CONVERTED",
                    "last_touched_at": now,
                    "updated_at": now,
                },
                "$addToSet": {"booking_ids": booking_id},
            },
        )

    await db.audit_records.insert_one(
        {
            "operator_id": operator_id,
            "event_type": "booking_attributed",
            "details": {
                "booking_id": str(booking_id),
                "candidate_id": str(candidate["_id"]),
                "lead_id": str(lead_id) if lead_id else None,
                "method": method,
                "confidence": confidence,
            },
            "severity": "info",
            "created_at": now,
        }
    )
    log.info(
        "booking attributed: booking=%s candidate=%s method=%s",
        booking_id,
        candidate["_id"],
        method,
    )
    return {
        "status": "attributed",
        "candidate_id": str(candidate["_id"]),
        "lead_id": str(lead_id) if lead_id else None,
        "method": method,
        "confidence": confidence,
    }


async def _resolve_lead_id(
    db: AsyncIOMotorDatabase, candidate: dict[str, Any]
) -> ObjectId | None:
    url = candidate.get("author_linkedin_url")
    if not url:
        return None
    lead = await db.leads.find_one(
        {"operator_id": candidate["operator_id"], "linkedin_url": url}
    )
    return lead["_id"] if lead else None


async def _set_no_match(db: AsyncIOMotorDatabase, booking_id: ObjectId) -> None:
    await db.bookings.update_one(
        {"_id": booking_id},
        {"$set": {"attribution_status": "no_match", "updated_at": utcnow()}},
    )
    booking = await db.bookings.find_one({"_id": booking_id})
    if booking:
        await db.audit_records.insert_one(
            {
                "operator_id": booking["operator_id"],
                "event_type": "booking_unattributed",
                "details": {"booking_id": str(booking_id)},
                "severity": "warn",
                "created_at": utcnow(),
            }
        )
