"""
Slate read routes for the dashboard's /today page + a manual trigger to kick a
daily_run on demand (handy for testing or recovering from a missed schedule).
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.celery_app import daily_run as daily_run_task
from app.database import get_db
from app.models.common import utcnow

router = APIRouter(prefix="/api/slate", tags=["slate"])


class CandidatePublic(BaseModel):
    id: str
    cofounder_id: str
    post_url: str
    author_name: str | None = None
    post_text: str
    post_published_at: datetime | None = None
    source: str
    source_classification: str
    status: str
    comment_text: str | None = None
    comment_type: str | None = None
    icp_score: int | None = None
    user_action: str
    drop_reason: str | None = None


class SlateRunPublic(BaseModel):
    id: str
    run_date: datetime
    status: str
    total_slated: int
    per_cofounder_counts: dict[str, Any]
    sealed_at: datetime | None = None
    email_sent: bool
    force_abort_reason: str | None = None
    # Live progress (populated while status='building').
    current_stage: str | None = None
    stage_progress: dict[str, int] | None = None
    stage_started_at: datetime | None = None
    stage_eta_seconds: int | None = None
    stage_note: str | None = None


class SlateTodayResponse(BaseModel):
    slate_run: SlateRunPublic | None
    candidates: list[CandidatePublic]
    cofounders: list[dict[str, Any]]


def _candidate_to_public(c: dict[str, Any]) -> CandidatePublic:
    icp = (c.get("gate_results") or {}).get("icp") or {}
    # Prefer the audit's normalized 0-10 score (RULE 14). Fall back to raw
    # `total` for slates predating the normalization patch — those will
    # render larger numbers but the relative ordering still works.
    score = icp.get("score_0_10")
    if score is None:
        score = icp.get("total")
    return CandidatePublic(
        id=str(c["_id"]),
        cofounder_id=str(c["cofounder_id"]),
        post_url=c.get("post_url", ""),
        author_name=c.get("author_name"),
        post_text=c.get("post_text") or "",
        post_published_at=c.get("post_published_at"),
        source=c.get("source", ""),
        source_classification=c.get("source_classification", ""),
        status=c.get("status", ""),
        comment_text=c.get("comment_text"),
        comment_type=c.get("comment_type"),
        icp_score=score,
        user_action=c.get("user_action", "pending"),
        drop_reason=c.get("drop_reason"),
    )


def _candidate_sort_key(c: dict[str, Any]) -> tuple:
    """Per-cofounder ranking: ICP score descending, then comment_type, then
    most-recently published first. Frontend uses this order to render
    rank #1..N inside each cofounder section."""
    icp = (c.get("gate_results") or {}).get("icp") or {}
    score = icp.get("score_0_10")
    if score is None:
        score = icp.get("total") or 0
    return (
        str(c.get("cofounder_id", "")),  # group by cofounder first
        -int(score or 0),                 # highest ICP first
        c.get("comment_type") or "Z",
    )


def _slate_to_public(doc: dict[str, Any]) -> SlateRunPublic:
    return SlateRunPublic(
        id=str(doc["_id"]),
        run_date=doc["run_date"],
        status=doc.get("status", "building"),
        total_slated=int(doc.get("total_slated") or 0),
        per_cofounder_counts=doc.get("per_cofounder_counts") or {},
        sealed_at=doc.get("sealed_at"),
        email_sent=bool(doc.get("email_sent", False)),
        force_abort_reason=doc.get("force_abort_reason"),
        current_stage=doc.get("current_stage"),
        stage_progress=doc.get("stage_progress"),
        stage_started_at=doc.get("stage_started_at"),
        stage_eta_seconds=doc.get("stage_eta_seconds"),
        stage_note=doc.get("stage_note"),
    )


@router.get("/today", response_model=SlateTodayResponse)
async def get_today(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SlateTodayResponse:
    today_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
    slate = await db.slate_runs.find_one(
        {"operator_id": user["_id"], "run_date": today_start},
        sort=[("created_at", -1)],
    )
    if not slate:
        slate = await db.slate_runs.find_one(
            {"operator_id": user["_id"]},
            sort=[("created_at", -1)],
        )

    candidates_raw: list[dict[str, Any]] = []
    if slate:
        cursor = db.candidates.find(
            {
                "slate_run_id": slate["_id"],
                "status": {"$in": ["slated", "shipped", "dropped_by_user"]},
            }
        )
        candidates_raw = await cursor.to_list(length=None)
        # Sort in Python — Mongo can't easily order by a nested gate_results
        # path while also grouping by cofounder. Within each cofounder we
        # rank by ICP score descending so the frontend's #1..N badge
        # reflects the engine's quality ordering.
        candidates_raw.sort(key=_candidate_sort_key)

    cofounders = await db.cofounders.find(
        {"operator_id": user["_id"], "active": True}
    ).to_list(length=None)
    cofounders_serialized = [
        {
            "id": str(cf["_id"]),
            "display_name": cf["display_name"],
            "linkedin_url": cf["linkedin_url"],
            "daily_volume_target": cf["daily_volume_target"],
        }
        for cf in cofounders
    ]

    return SlateTodayResponse(
        slate_run=_slate_to_public(slate) if slate else None,
        candidates=[_candidate_to_public(c) for c in candidates_raw],
        cofounders=cofounders_serialized,
    )


class CandidateActionRequest(BaseModel):
    action: Literal["copied", "shipped", "dropped", "edited"]
    edited_text: str | None = None


@router.put("/candidates/{candidate_id}/action")
async def candidate_action(
    candidate_id: Annotated[str, Path()],
    payload: CandidateActionRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, str]:
    if not ObjectId.is_valid(candidate_id):
        raise HTTPException(400, "Invalid id")
    update: dict[str, Any] = {"user_action": payload.action, "updated_at": utcnow()}
    if payload.action == "shipped":
        update["status"] = "shipped"
        update["shipped_at"] = utcnow()
    elif payload.action == "dropped":
        update["status"] = "dropped_by_user"
    elif payload.action == "edited" and payload.edited_text:
        update["comment_text"] = payload.edited_text
    result = await db.candidates.find_one_and_update(
        {"_id": ObjectId(candidate_id), "operator_id": user["_id"]},
        {"$set": update},
        return_document=True,
    )
    if not result:
        raise HTTPException(404, "Candidate not found")

    # On ship, find-or-create the post-author Lead and link this candidate to it.
    # Stage advances to S2 ("commented"). Pulled into a sync helper so the Lead
    # path matches the Celery reply-monitor write surface.
    if payload.action == "shipped" and result.get("author_linkedin_url"):
        await _upsert_lead_async(
            db,
            operator_id=user["_id"],
            linkedin_url=result["author_linkedin_url"],
            name=result.get("author_name"),
            title=result.get("author_title"),
            company=result.get("author_company"),
            candidate_id=result["_id"],
            advance_stage_to="S2",
        )
    return {"ok": "true"}


async def _upsert_lead_async(
    db: AsyncIOMotorDatabase,
    *,
    operator_id: ObjectId,
    linkedin_url: str,
    name: str | None,
    title: str | None,
    company: str | None,
    candidate_id: ObjectId,
    advance_stage_to: str,
) -> None:
    now = utcnow()
    set_on_insert = {
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
        "current_stage": advance_stage_to,
        "updated_at": now,
    }
    if name:
        set_always["name"] = name
    if title:
        set_always["title"] = title
    if company:
        set_always["company"] = company
    await db.leads.update_one(
        {"operator_id": operator_id, "linkedin_url": linkedin_url},
        {
            "$setOnInsert": set_on_insert,
            "$set": set_always,
            "$addToSet": {"candidate_ids": candidate_id},
        },
        upsert=True,
    )


@router.post("/run-now")
async def run_now(user: CurrentUser) -> dict[str, str]:
    """Manually trigger a daily_run for the current operator (testing/recovery)."""
    if not user.get("onboarding_complete"):
        raise HTTPException(400, "Onboarding not complete")
    result = daily_run_task.delay(str(user["_id"]))
    return {"task_id": result.id, "status": "queued"}
