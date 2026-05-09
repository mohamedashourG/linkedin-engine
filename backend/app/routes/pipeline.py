"""
Pipeline kanban + lead detail. Reads from leads, candidates, replies, bookings.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.database import get_db
from app.models.common import utcnow

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

STAGES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")


class LeadCard(BaseModel):
    id: str
    linkedin_url: str
    name: str | None
    title: str | None
    company: str | None
    current_stage: str
    status: str
    reply_count: int
    last_touched_at: datetime | None
    cr_sent_at: datetime | None
    cr_accepted_at: datetime | None
    dm_sent_at: datetime | None
    booking_count: int


class PipelineResponse(BaseModel):
    by_stage: dict[str, list[LeadCard]]


class TimelineEntry(BaseModel):
    kind: Literal["candidate", "reply", "booking", "stage_change"]
    at: datetime
    title: str
    body: str | None = None


class LeadDetail(BaseModel):
    lead: LeadCard
    timeline: list[TimelineEntry]


def _lead_card(doc: dict[str, Any], booking_count: int = 0) -> LeadCard:
    return LeadCard(
        id=str(doc["_id"]),
        linkedin_url=doc.get("linkedin_url", ""),
        name=doc.get("name"),
        title=doc.get("title"),
        company=doc.get("company"),
        current_stage=doc.get("current_stage", "S1"),
        status=doc.get("status", "ACTIVE"),
        reply_count=int(doc.get("reply_count") or 0),
        last_touched_at=doc.get("last_touched_at"),
        cr_sent_at=doc.get("cr_sent_at"),
        cr_accepted_at=doc.get("cr_accepted_at"),
        dm_sent_at=doc.get("dm_sent_at"),
        booking_count=booking_count,
    )


@router.get("/", response_model=PipelineResponse)
async def get_pipeline(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> PipelineResponse:
    leads = await db.leads.find({"operator_id": user["_id"]}).to_list(length=None)
    bookings = await db.bookings.aggregate(
        [
            {"$match": {"operator_id": user["_id"], "lead_id": {"$ne": None}}},
            {"$group": {"_id": "$lead_id", "n": {"$sum": 1}}},
        ]
    ).to_list(length=None)
    booking_counts = {b["_id"]: b["n"] for b in bookings if b["_id"]}

    by_stage: dict[str, list[LeadCard]] = {s: [] for s in STAGES}
    for lead in leads:
        stage = lead.get("current_stage", "S1")
        if stage not in by_stage:
            by_stage[stage] = []
        by_stage[stage].append(_lead_card(lead, booking_counts.get(lead["_id"], 0)))

    for stage in by_stage:
        by_stage[stage].sort(
            key=lambda c: c.last_touched_at or datetime.min,
            reverse=True,
        )
    return PipelineResponse(by_stage=by_stage)


@router.get("/leads/{lead_id}", response_model=LeadDetail)
async def get_lead(
    lead_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> LeadDetail:
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(400, "Invalid id")
    lead = await db.leads.find_one(
        {"_id": ObjectId(lead_id), "operator_id": user["_id"]}
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    candidates = await db.candidates.find(
        {"_id": {"$in": lead.get("candidate_ids") or []}}
    ).to_list(length=200)
    replies = await db.replies.find(
        {"_id": {"$in": lead.get("reply_ids") or []}}
    ).to_list(length=200)
    bookings = await db.bookings.find(
        {"_id": {"$in": lead.get("booking_ids") or []}}
    ).to_list(length=50)

    timeline: list[TimelineEntry] = []

    for c in candidates:
        timeline.append(
            TimelineEntry(
                kind="candidate",
                at=c.get("shipped_at") or c.get("created_at") or utcnow(),
                title=f"Comment {('shipped' if c.get('status') == 'shipped' else c.get('status') or 'drafted')}",
                body=(c.get("comment_text") or "")[:400] or None,
            )
        )
    for r in replies:
        timeline.append(
            TimelineEntry(
                kind="reply",
                at=r["detected_at"],
                title=f"Reply from {r.get('reply_author_name') or 'unknown'}",
                body=(r.get("reply_text") or "")[:400] or None,
            )
        )
    for b in bookings:
        timeline.append(
            TimelineEntry(
                kind="booking",
                at=b.get("booked_at") or utcnow(),
                title=f"Booked: {b.get('invitee_name') or 'unknown'}",
                body=f"Meeting at {b.get('meeting_at')}" if b.get("meeting_at") else None,
            )
        )
    if lead.get("cr_sent_at"):
        timeline.append(
            TimelineEntry(
                kind="stage_change",
                at=lead["cr_sent_at"],
                title="Connection request sent",
                body=lead.get("cr_invitation_id"),
            )
        )
    if lead.get("cr_accepted_at"):
        timeline.append(
            TimelineEntry(
                kind="stage_change",
                at=lead["cr_accepted_at"],
                title="Connection accepted",
            )
        )
    if lead.get("dm_sent_at"):
        timeline.append(
            TimelineEntry(
                kind="stage_change",
                at=lead["dm_sent_at"],
                title="DM sent",
            )
        )

    timeline.sort(key=lambda e: e.at, reverse=True)
    return LeadDetail(
        lead=_lead_card(lead, len(bookings)),
        timeline=timeline,
    )


class StageOverrideRequest(BaseModel):
    stage: str = Field(pattern=r"^S[1-9]$")


@router.put("/leads/{lead_id}/stage")
async def override_stage(
    lead_id: Annotated[str, Path()],
    payload: StageOverrideRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(400, "Invalid id")
    result = await db.leads.find_one_and_update(
        {"_id": ObjectId(lead_id), "operator_id": user["_id"]},
        {"$set": {"current_stage": payload.stage, "updated_at": utcnow()}},
        return_document=True,
    )
    if not result:
        raise HTTPException(404, "Lead not found")
    return {"ok": True}
