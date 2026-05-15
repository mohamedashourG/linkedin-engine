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
    # Per-comment engagement for this lead's shipped candidates. Same
    # shape the past-run page uses (defined in routes.slate) so the
    # shared CommentTracker UI component renders both surfaces.
    comments_engagement: list[Any] = Field(default_factory=list)


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

    # Build per-comment engagement view for this lead. Reuses the slate
    # tracker helpers so both surfaces (past-run page + lead detail
    # panel) speak the same JSON shape and render via the shared
    # CommentTracker component on the frontend.
    comments_engagement = await _build_lead_comments_engagement(
        db, candidates=candidates, replies=replies, operator_id=user["_id"]
    )

    return LeadDetail(
        lead=_lead_card(lead, len(bookings)),
        timeline=timeline,
        comments_engagement=comments_engagement,
    )


async def _build_lead_comments_engagement(
    db: AsyncIOMotorDatabase,
    *,
    candidates: list[dict[str, Any]],
    replies: list[dict[str, Any]],
    operator_id: ObjectId,
) -> list[dict[str, Any]]:
    """Build the same TrackerCandidate JSON shape `routes.slate` uses,
    but scoped to this lead's already-fetched candidates + replies.
    Returns plain dicts (typed via the LeadDetail.comments_engagement
    field's `Any` slot) so we don't introduce a circular pydantic
    import between pipeline.py and slate.py."""
    if not candidates:
        return []

    cand_ids = [c["_id"] for c in candidates]
    ocs = await db.our_comments.find(
        {"candidate_id": {"$in": cand_ids}, "operator_id": operator_id}
    ).to_list(length=len(cand_ids))
    oc_by_cand = {oc["candidate_id"]: oc for oc in ocs}

    replies_by_cand: dict[ObjectId, list[dict[str, Any]]] = {}
    for r in replies:
        cid = r.get("candidate_id")
        if cid is not None:
            replies_by_cand.setdefault(cid, []).append(r)

    out: list[dict[str, Any]] = []
    for c in candidates:
        if c.get("status") not in ("drafted", "slated", "shipped", "dropped_by_user"):
            continue
        oc = oc_by_cand.get(c["_id"])
        our_comment_id = (oc or {}).get("comment_id")
        if our_comment_id:
            oc_status = "detected"
        elif oc:
            oc_status = (oc.get("status") or "queued")
        else:
            oc_status = "not_found"
        rs = []
        for r in sorted(
            replies_by_cand.get(c["_id"], []),
            key=lambda r: r.get("detected_at") or datetime.min,
        ):
            rs.append({
                "id": str(r["_id"]),
                "text": r.get("reply_text") or "",
                "author_name": r.get("reply_author_name"),
                "author_linkedin_url": r.get("reply_author_linkedin_url"),
                "author_is_post_owner": bool(r.get("reply_author_is_post_owner")),
                "published_at": r.get("reply_published_at"),
                "suggested_reply": r.get("suggested_reply") or "",
                "suggested_reply_type": r.get("suggested_reply_type") or "",
                "user_action": (r.get("user_action") or "pending"),
            })
        text = c.get("post_text") or ""
        reactions_raw = c.get("parent_post_reactions")
        reactions: dict[str, int] | None = None
        if isinstance(reactions_raw, dict) and reactions_raw:
            reactions = {
                str(k): int(v)
                for k, v in reactions_raw.items()
                if isinstance(v, (int, float))
            } or None
        out.append({
            "candidate_id": str(c["_id"]),
            "cofounder_id": str(c.get("cofounder_id") or ""),
            "status": c.get("status") or "",
            "shipped_at": c.get("shipped_at"),
            "post_url": c.get("post_url") or "",
            "post_text_preview": (text[:220] + "…") if len(text) > 220 else text,
            "author_name": c.get("author_name"),
            "our_comment_text": c.get("comment_text") or (oc or {}).get("text") or "",
            "our_comment_id": our_comment_id,
            "our_comment_status": oc_status,
            "latest_reaction_count": int((oc or {}).get("latest_reaction_count") or 0),
            "latest_reply_count": int((oc or {}).get("latest_reply_count") or 0),
            "latest_polled_at": (oc or {}).get("latest_polled_at"),
            "post_likes": int(c.get("parent_post_likes") or 0),
            "post_comments_total": int(c.get("parent_post_comments_total") or 0),
            "post_shares": int(c.get("parent_post_shares") or 0),
            "post_reactions": reactions,
            "post_polled_at": c.get("parent_post_polled_at"),
            "replies": rs,
        })

    # Shipped first, newest shipped_at first.
    out.sort(
        key=lambda t: (
            0 if t["status"] == "shipped" else 1,
            -(t["shipped_at"].timestamp() if t.get("shipped_at") else 0),
        )
    )
    return out


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


@router.post("/leads/{lead_id}/track-comments")
async def track_lead_comments(
    lead_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> LeadDetail:
    """On-demand poll for this lead's shipped candidates. Same flow as
    slate's /track-selected but scoped to one lead. Returns the full
    refreshed LeadDetail so the UI re-renders in one round trip."""
    if not ObjectId.is_valid(lead_id):
        raise HTTPException(400, "Invalid id")
    lead = await db.leads.find_one(
        {"_id": ObjectId(lead_id), "operator_id": user["_id"]}
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    cand_ids = lead.get("candidate_ids") or []
    if cand_ids:
        shipped = await db.candidates.find({
            "_id": {"$in": cand_ids},
            "operator_id": user["_id"],
            "status": "shipped",
        }).to_list(length=len(cand_ids))

        if shipped:
            cf_ids = list({c["cofounder_id"] for c in shipped if c.get("cofounder_id")})
            cofounders = await db.cofounders.find(
                {"_id": {"$in": cf_ids}, "operator_id": user["_id"]}
            ).to_list(length=len(cf_ids))
            cf_by_id = {cf["_id"]: cf for cf in cofounders}

            # Same sync-pymongo thread bridge as routes.slate's track-selected.
            import asyncio
            from pymongo import MongoClient
            from app.config import settings as _settings
            from app.engine.engagement_poller import poll_one_candidate
            import logging as _logging
            _log = _logging.getLogger(__name__)

            def _poll_blocking() -> None:
                client = MongoClient(_settings.mongodb_uri)
                try:
                    sdb = client[_settings.mongodb_db]
                    for c in shipped:
                        cf = cf_by_id.get(c["cofounder_id"])
                        if not cf:
                            continue
                        try:
                            poll_one_candidate(
                                sdb,
                                operator_id=user["_id"],
                                cofounder=cf,
                                candidate=c,
                            )
                        except Exception:  # noqa: BLE001
                            _log.exception(
                                "track_lead_comments: poll failed for %s", c.get("_id"),
                            )
                finally:
                    client.close()

            await asyncio.to_thread(_poll_blocking)

    # Re-build the lead-detail response so the caller gets fresh state.
    return await get_lead(lead_id=lead_id, user=user, db=db)
