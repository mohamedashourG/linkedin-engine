"""
Slate read routes for the dashboard's /today page + a manual trigger to kick a
daily_run on demand (handy for testing or recovering from a missed schedule).
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Annotated, Any, Literal

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pymongo import MongoClient
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.celery_app import daily_run as daily_run_task
from app.config import settings
from app.database import get_db
from app.engine import outbox
from app.models.common import utcnow
from app.routes.slate_pipeline import compute_pipeline_breakdown

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
    # Operator-controlled flags. `skip_remaining_discovery=True` tells the
    # discovery worker to abandon all remaining sources and let downstream
    # gates/allocator/drafter finish on already-found candidates.
    skip_remaining_discovery: bool = False
    skip_remaining_discovery_at: datetime | None = None


class PipelinePostRef(BaseModel):
    id: str
    post_url: str
    author_name: str | None = None
    post_preview: str = ""
    status: str = ""
    drop_reason: str | None = None


class PipelineStepBreakdownPublic(BaseModel):
    passed: list[PipelinePostRef]
    failed: list[PipelinePostRef]
    pending: list[PipelinePostRef] = Field(default_factory=list)
    passed_total: int
    failed_total: int
    pending_total: int
    truncated: bool


class PipelineBreakdownPublic(BaseModel):
    discovery: PipelineStepBreakdownPublic
    verification: PipelineStepBreakdownPublic
    gates: PipelineStepBreakdownPublic
    allocator: PipelineStepBreakdownPublic
    drafter: PipelineStepBreakdownPublic
    rule_23: PipelineStepBreakdownPublic
    email_delivery: PipelineStepBreakdownPublic


class SlateTodayResponse(BaseModel):
    slate_run: SlateRunPublic | None
    candidates: list[CandidatePublic]
    cofounders: list[dict[str, Any]]
    pipeline: PipelineBreakdownPublic | None = None


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
        skip_remaining_discovery=bool(doc.get("skip_remaining_discovery", False)),
        skip_remaining_discovery_at=doc.get("skip_remaining_discovery_at"),
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
    pipeline: PipelineBreakdownPublic | None = None
    if slate:
        all_for_run = await db.candidates.find(
            {"slate_run_id": slate["_id"]}
        ).to_list(length=None)
        pipeline_raw = compute_pipeline_breakdown(
            all_for_run,
            slate_status=str(slate.get("status") or "building"),
            email_sent=bool(slate.get("email_sent", False)),
        )
        pipeline = PipelineBreakdownPublic.model_validate(pipeline_raw)

        # While the run is still building, surface drafted-but-not-yet-sealed
        # candidates too so the operator sees them appear live as the drafter
        # worker emits each one (streaming pipeline) rather than waiting for
        # RULE 23 + email at the end. After seal, only the final lineup
        # (slated / shipped / dropped_by_user) shows — drafted leftovers that
        # didn't make the seal are filtered out (rule_23 dropped them).
        slate_status = str(slate.get("status") or "building")
        if slate_status == "building":
            visible_statuses = ("slated", "shipped", "dropped_by_user", "drafted")
        else:
            visible_statuses = ("slated", "shipped", "dropped_by_user")
        candidates_raw = [
            c for c in all_for_run if c.get("status") in visible_statuses
        ]
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
        pipeline=pipeline,
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

    if (
        payload.action == "shipped"
        and settings.comment_outbox_on_ship_enabled
        and result.get("comment_text")
        and result.get("post_url")
        and result.get("cofounder_id")
        and result.get("slate_run_id")
    ):
        text = str(result.get("comment_text") or "").strip()
        post_url = str(result.get("post_url") or "").strip()
        if text and post_url:
            sync_client = MongoClient(settings.mongodb_uri)
            try:
                sdb = sync_client[settings.mongodb_db]
                cof = sdb.cofounders.find_one({"_id": result["cofounder_id"]})
                acc = (cof or {}).get("unipile_account_id") or ""
                if acc:
                    outbox.enqueue_comment(
                        sdb,
                        operator_id=user["_id"],
                        cofounder_id=result["cofounder_id"],
                        unipile_account_id=acc,
                        candidate_id=result["_id"],
                        slate_run_id=result["slate_run_id"],
                        parent_post_url=post_url,
                        parent_post_id=result.get("post_id"),
                        parent_post_author_provider_id=None,
                        text=text,
                        parent_post_reaction_count_at_send=int(
                            result.get("reaction_counter") or 0
                        ),
                        parent_post_comment_count_at_send=int(
                            result.get("comment_counter") or 0
                        ),
                        parent_post_repost_count_at_send=int(
                            result.get("repost_counter") or 0
                        ),
                    )
            finally:
                sync_client.close()
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


@router.post("/runs/{slate_run_id}/skip-discovery")
async def skip_remaining_discovery(
    slate_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, Any]:
    """Flag the in-flight slate to abandon all remaining discovery sources.

    The discovery worker checks this flag at the top of every source and
    every per-query iteration (within a few seconds). Already-inserted
    candidates continue flowing through gates → allocator → drafter → seal.

    Idempotent. Returns the current state."""
    try:
        run_oid = ObjectId(slate_run_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid slate_run_id")

    result = await db.slate_runs.find_one_and_update(
        {"_id": run_oid, "operator_id": user["_id"]},
        {
            "$set": {
                "skip_remaining_discovery": True,
                "skip_remaining_discovery_at": utcnow(),
                "updated_at": utcnow(),
            }
        },
        return_document=True,
    )
    if not result:
        raise HTTPException(404, "Slate run not found")
    return {
        "slate_run_id": str(run_oid),
        "skip_remaining_discovery": True,
        "skip_remaining_discovery_at": result.get("skip_remaining_discovery_at"),
        "status": result.get("status"),
        "current_stage": result.get("current_stage"),
    }


# ── Past-runs viewer ─────────────────────────────────────────────────────
# Operator-scoped list + per-run detail for browsing historical slate runs.


class RunListItem(BaseModel):
    """Compact summary of one slate_run for the past-runs list view."""

    id: str
    run_date: datetime
    status: str  # "building" | "sealed" | "force_aborted"
    sealed_at: datetime | None = None
    created_at: datetime
    runtime_seconds: float | None = None  # sealed_at - created_at
    total_discovered: int = 0
    total_verified: int = 0
    total_gated: int = 0
    total_drafted: int = 0
    total_slated: int = 0
    email_sent: bool = False
    force_abort_reason: str | None = None


class RunsListResponse(BaseModel):
    runs: list[RunListItem]
    next_before: datetime | None = None


class SourceStatusBucket(BaseModel):
    source: str
    status: str
    count: int


class DropReasonBucket(BaseModel):
    reason: str
    count: int


class RunDetailResponse(BaseModel):
    slate_run: SlateRunPublic
    runtime_seconds: float | None = None
    total_discovered: int = 0
    total_verified: int = 0
    total_gated: int = 0
    total_drafted: int = 0
    total_slated: int = 0
    # Per-(source, status) counts aggregated from the candidates collection.
    source_status: list[SourceStatusBucket]
    # Top drop_reason histogram (truncated to top 20).
    top_drop_reasons: list[DropReasonBucket]
    # The drafted / slated / shipped candidates for this run (UI table).
    candidates: list[CandidatePublic]
    cofounders: list[dict[str, Any]]


def _run_list_item(doc: dict[str, Any]) -> RunListItem:
    runtime: float | None = None
    if doc.get("sealed_at") and doc.get("created_at"):
        try:
            runtime = (doc["sealed_at"] - doc["created_at"]).total_seconds()
        except (TypeError, AttributeError):
            runtime = None
    return RunListItem(
        id=str(doc["_id"]),
        run_date=doc["run_date"],
        status=doc.get("status", "building"),
        sealed_at=doc.get("sealed_at"),
        created_at=doc["created_at"],
        runtime_seconds=runtime,
        total_discovered=int(doc.get("total_discovered") or 0),
        total_verified=int(doc.get("total_verified") or 0),
        total_gated=int(doc.get("total_gated") or 0),
        total_drafted=int(doc.get("total_drafted") or 0),
        total_slated=int(doc.get("total_slated") or 0),
        email_sent=bool(doc.get("email_sent", False)),
        force_abort_reason=doc.get("force_abort_reason"),
    )


@router.get("/runs", response_model=RunsListResponse)
async def list_runs(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[datetime | None, Query()] = None,
) -> RunsListResponse:
    """Past slate runs for the current operator, newest first.

    ``before`` is an exclusive cursor on ``created_at`` for load-more
    pagination; pass the last returned run's ``created_at`` to fetch the
    next page."""
    q: dict[str, Any] = {"operator_id": user["_id"]}
    if before is not None:
        q["created_at"] = {"$lt": before}
    cursor = (
        db.slate_runs.find(q).sort("created_at", -1).limit(limit + 1)
    )
    docs = await cursor.to_list(length=limit + 1)
    has_more = len(docs) > limit
    page = docs[:limit]
    next_before = page[-1]["created_at"] if has_more and page else None
    return RunsListResponse(
        runs=[_run_list_item(d) for d in page],
        next_before=next_before,
    )


@router.get("/runs/{slate_run_id}", response_model=RunDetailResponse)
async def run_detail(
    slate_run_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> RunDetailResponse:
    """Full detail for one slate_run: funnel by source × status,
    drop-reason histogram, and the drafted/slated candidate list."""
    try:
        slate_oid = ObjectId(slate_run_id)
    except Exception:
        raise HTTPException(400, "Invalid slate_run_id")
    slate = await db.slate_runs.find_one(
        {"_id": slate_oid, "operator_id": user["_id"]}
    )
    if not slate:
        raise HTTPException(404, "Slate run not found")

    # Per-(source, status) breakdown.
    pipeline = [
        {"$match": {"slate_run_id": slate_oid}},
        {
            "$group": {
                "_id": {"source": "$source", "status": "$status"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
    ]
    source_status_rows = await db.candidates.aggregate(pipeline).to_list(length=None)
    source_status = [
        SourceStatusBucket(
            source=str(r["_id"].get("source") or "(unknown)"),
            status=str(r["_id"].get("status") or "(unknown)"),
            count=int(r["count"]),
        )
        for r in source_status_rows
    ]

    # Top drop-reason histogram.
    drop_pipeline = [
        {
            "$match": {
                "slate_run_id": slate_oid,
                "drop_reason": {"$exists": True, "$ne": None, "$type": "string"},
            }
        },
        {
            "$group": {
                "_id": {"$substr": ["$drop_reason", 0, 80]},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    drop_rows = await db.candidates.aggregate(drop_pipeline).to_list(length=20)
    top_drop_reasons = [
        DropReasonBucket(reason=str(r["_id"]), count=int(r["count"]))
        for r in drop_rows
    ]

    # Drafted / slated / shipped candidates.
    cand_docs = await db.candidates.find(
        {
            "slate_run_id": slate_oid,
            "status": {"$in": ["slated", "shipped", "dropped_by_user", "drafted"]},
        }
    ).to_list(length=None)
    cand_docs.sort(key=_candidate_sort_key)
    candidates = [_candidate_to_public(c) for c in cand_docs]

    # Cofounders for the operator (used by the UI to group/label).
    cofounders_raw = await db.cofounders.find(
        {"operator_id": user["_id"]}
    ).to_list(length=None)
    cofounders = [
        {
            "id": str(cf["_id"]),
            "display_name": cf.get("display_name") or "",
            "active": bool(cf.get("active", True)),
        }
        for cf in cofounders_raw
    ]

    runtime: float | None = None
    if slate.get("sealed_at") and slate.get("created_at"):
        try:
            runtime = (slate["sealed_at"] - slate["created_at"]).total_seconds()
        except (TypeError, AttributeError):
            runtime = None

    return RunDetailResponse(
        slate_run=_slate_to_public(slate),
        runtime_seconds=runtime,
        total_discovered=int(slate.get("total_discovered") or 0),
        total_verified=int(slate.get("total_verified") or 0),
        total_gated=int(slate.get("total_gated") or 0),
        total_drafted=int(slate.get("total_drafted") or 0),
        total_slated=int(slate.get("total_slated") or 0),
        source_status=source_status,
        top_drop_reasons=top_drop_reasons,
        candidates=candidates,
        cofounders=cofounders,
    )


class EmailSelectedRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=200)
    to: str | None = None  # default: the operator's own email
    subject: str | None = None


class EmailSelectedResponse(BaseModel):
    message_id: str
    count: int
    to: str


def _selected_drafts_html(rows: list[dict[str, Any]], subject: str) -> str:
    """Render the selected candidates as an inlined-CSS email body."""
    blocks: list[str] = []
    for c in rows:
        icp = ((c.get("gate_results") or {}).get("icp") or {}).get("score_0_10")
        icp_str = str(icp) if icp is not None else "?"
        comment_text = (c.get("comment_text") or "(no drafted comment)").strip()
        post_text = (c.get("post_text") or "")[:600]
        post_url = c.get("post_url") or "#"
        ctype = c.get("comment_type") or "?"
        author = c.get("author_name") or "(unknown author)"
        # Escape minimal HTML — these are LinkedIn strings, no scripts expected,
        # but defensive.
        def esc(s: str) -> str:
            return (
                s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        blocks.append(
            "<div style=\"margin:0 0 28px 0;padding:16px;border:1px solid #e5e7eb;"
            "border-radius:12px;font-family:-apple-system,Segoe UI,Roboto,sans-serif;\">"
            f"<div style=\"font-size:12px;color:#6b7280;margin-bottom:6px;\">"
            f"Type {esc(ctype)} · ICP {esc(icp_str)} · {esc(author)}</div>"
            "<div style=\"font-size:13px;color:#111827;margin-bottom:10px;\">"
            "<strong>Original post</strong><br/>"
            f"<span style=\"color:#374151;\">{esc(post_text)}</span></div>"
            "<div style=\"font-size:13px;background:#0b1220;color:#e5e7eb;padding:12px;"
            f"border-radius:8px;white-space:pre-wrap;\">{esc(comment_text)}</div>"
            "<div style=\"margin-top:8px;font-size:12px;\">"
            f"<a href=\"{esc(post_url)}\">Open post on LinkedIn →</a></div>"
            "</div>"
        )
    return (
        "<html><body style=\"background:#f9fafb;padding:24px;\">"
        f"<h2 style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;\">{subject}</h2>"
        + "".join(blocks)
        + "</body></html>"
    )


@router.post(
    "/runs/{slate_run_id}/email-selected",
    response_model=EmailSelectedResponse,
)
async def email_selected_drafts(
    slate_run_id: Annotated[str, Path()],
    payload: EmailSelectedRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> EmailSelectedResponse:
    """Email a chosen subset of a run's candidates (original post + drafted
    comment) to the operator. Defaults `to` to the logged-in operator's email."""
    if not ObjectId.is_valid(slate_run_id):
        raise HTTPException(400, "Invalid slate_run_id")

    # Authorize: run must belong to this operator.
    slate = await db.slate_runs.find_one(
        {"_id": ObjectId(slate_run_id), "operator_id": user["_id"]},
        {"_id": 1},
    )
    if not slate:
        raise HTTPException(404, "Run not found")

    # Validate ids + load candidates scoped to operator + this run.
    cand_oids: list[ObjectId] = []
    for s in payload.candidate_ids:
        if ObjectId.is_valid(s):
            cand_oids.append(ObjectId(s))
    if not cand_oids:
        raise HTTPException(400, "No valid candidate ids")

    cursor = db.candidates.find(
        {
            "_id": {"$in": cand_oids},
            "slate_run_id": ObjectId(slate_run_id),
            "operator_id": user["_id"],
        }
    )
    rows = await cursor.to_list(length=len(cand_oids))
    if not rows:
        raise HTTPException(404, "No candidates matched")

    # Preserve the client's id order so the email matches the UI selection order.
    pos = {oid: i for i, oid in enumerate(cand_oids)}
    rows.sort(key=lambda r: pos.get(r["_id"], 1_000_000))

    to_addr = (payload.to or user.get("email") or "").strip().lower()
    if not to_addr:
        raise HTTPException(400, "No recipient")

    subject = payload.subject or f"Selected slate drafts ({len(rows)} candidates)"
    html = _selected_drafts_html(rows, subject)

    # Import here so a missing Resend key only fails the route, not import time.
    from app.services.email import send_email, EmailNotConfigured
    try:
        msg_id = send_email(to=to_addr, subject=subject, html=html)
    except EmailNotConfigured as err:
        raise HTTPException(503, f"Email not configured: {err}")
    except Exception as err:  # noqa: BLE001 — Resend raises a variety
        raise HTTPException(502, f"Email send failed: {err}")

    return EmailSelectedResponse(message_id=msg_id, count=len(rows), to=to_addr)
