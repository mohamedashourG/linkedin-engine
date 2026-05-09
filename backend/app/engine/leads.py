"""
Lead helpers. Leads are persons we've engaged with, tracked across multiple
candidates and replies. Stages S1-S9 are defined in the spec; we only mutate
S2 (commented) and S4 (CR sent) automatically — S5/S6/S7 require EOD or
Calendly evidence (Phase 5).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.models.common import utcnow

LEAD_TTL_DAYS = 90


def upsert_lead(
    db: Database,
    *,
    operator_id: ObjectId,
    linkedin_url: str | None,
    name: str | None = None,
    title: str | None = None,
    company: str | None = None,
    candidate_id: ObjectId | None = None,
    advance_stage_to: str | None = None,
) -> ObjectId | None:
    """Find-or-create a lead keyed on (operator_id, linkedin_url).

    Returns the lead's _id, or None if no LinkedIn URL is available (we don't
    create rootless leads).
    """
    if not linkedin_url:
        return None
    now = utcnow()
    expires = now + timedelta(days=LEAD_TTL_DAYS)

    set_on_insert: dict[str, Any] = {
        "operator_id": operator_id,
        "linkedin_url": linkedin_url,
        "status": "ACTIVE",
        "first_engaged_at": now,
        "cr_sent_at": None,
        "cr_accepted_at": None,
        "dm_sent_at": None,
        "reply_count": 0,
        "notes": None,
        "created_at": now,
    }
    set_always: dict[str, Any] = {
        "last_touched_at": now,
        "expires_at": expires,
        "updated_at": now,
    }
    if name:
        set_always["name"] = name
    if title:
        set_always["title"] = title
    if company:
        set_always["company"] = company
    # current_stage gets a default on insert via $set (always written) so it
    # doesn't collide with $setOnInsert.
    set_always["current_stage"] = advance_stage_to or "S1"

    push: dict[str, Any] = {}
    if candidate_id:
        push["candidate_ids"] = candidate_id

    update: dict[str, Any] = {
        "$setOnInsert": set_on_insert,
        "$set": set_always,
    }
    if push:
        update["$addToSet"] = push

    db.leads.update_one(
        {"operator_id": operator_id, "linkedin_url": linkedin_url},
        update,
        upsert=True,
    )
    doc = db.leads.find_one(
        {"operator_id": operator_id, "linkedin_url": linkedin_url},
        {"_id": 1},
    )
    return doc["_id"] if doc else None
