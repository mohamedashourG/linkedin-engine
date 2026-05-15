"""
Slate read routes for the dashboard's /today page + a manual trigger to kick a
daily_run on demand (handy for testing or recovering from a missed schedule).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Annotated, Any, Literal

log = logging.getLogger(__name__)

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
    # Per-source "skip current source → next" support. `current_source` is
    # the source-id the discovery worker is iterating right now (set when
    # a `_run_*` function enters its loop). `skip_current_source` is the
    # source-id the operator clicked to skip — when it matches what the
    # worker is iterating, the worker breaks out and the engine moves on
    # to the next source in the fixed order
    # (unipile_title_search → unipile_keyword → apidirect → exa).
    current_source: str | None = None
    skip_current_source: str | None = None
    skip_current_source_consumed_at: datetime | None = None
    skip_current_source_consumed_for: str | None = None


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
    inline_rubric: PipelineStepBreakdownPublic
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
        current_source=doc.get("current_source"),
        skip_current_source=doc.get("skip_current_source"),
        skip_current_source_consumed_at=doc.get("skip_current_source_consumed_at"),
        skip_current_source_consumed_for=doc.get("skip_current_source_consumed_for"),
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


# Canonical discovery-source order. Mirrors the order in
# `discovery.discover_for_operator`. Used to compute "next source" labels
# for the UI and to validate the skip-target.
_DISCOVERY_SOURCE_ORDER = (
    "unipile_title_search",
    "unipile_keyword",
    "apidirect",
    "exa",
)


def _next_discovery_source(current: str | None) -> str | None:
    """Return the source-id that comes after `current` in the fixed
    discovery order, or None if `current` is the last source or unknown."""
    if not current:
        return _DISCOVERY_SOURCE_ORDER[0] if _DISCOVERY_SOURCE_ORDER else None
    try:
        idx = _DISCOVERY_SOURCE_ORDER.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(_DISCOVERY_SOURCE_ORDER):
        return None
    return _DISCOVERY_SOURCE_ORDER[idx + 1]


@router.post("/runs/{slate_run_id}/skip-current-source")
async def skip_current_source_endpoint(
    slate_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, Any]:
    """Skip the discovery source the worker is currently iterating, then
    continue with the next source in the fixed order:
        unipile_title_search → unipile_keyword → apidirect → exa.

    Reads `current_source` from the slate_run, stamps `skip_current_source`
    with that value. The worker consumes the flag (atomic find_one_and_update
    in `_should_skip_current_source`) and breaks out of the current source's
    per-query loop. The flag self-clears on consume, so a follow-up click
    while a new source is running will skip that one too.

    Returns the source being skipped + the source the worker will move to
    next. 409 if there's no active discovery source (run sealed / aborted
    / between sources)."""
    try:
        run_oid = ObjectId(slate_run_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid slate_run_id")

    # Read current_source first so the API can tell the operator what's
    # being skipped + what comes next. Atomic update happens worker-side
    # via _should_skip_current_source.
    sr = await db.slate_runs.find_one(
        {"_id": run_oid, "operator_id": user["_id"]},
        projection={"current_source": 1, "status": 1, "current_stage": 1},
    )
    if not sr:
        raise HTTPException(404, "Slate run not found")
    current = sr.get("current_source")
    if not current:
        raise HTTPException(
            409,
            "No discovery source is currently active. Either discovery hasn't "
            "started, has already finished, or the worker is between sources.",
        )

    next_src = _next_discovery_source(current)

    await db.slate_runs.update_one(
        {"_id": run_oid},
        {
            "$set": {
                "skip_current_source": current,
                "skip_current_source_requested_at": utcnow(),
                "updated_at": utcnow(),
            }
        },
    )
    return {
        "slate_run_id": str(run_oid),
        "skipped_source": current,
        "next_source": next_src,
        "status": sr.get("status"),
        "current_stage": sr.get("current_stage"),
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


class CrossRunDedupSkips(BaseModel):
    """Discovery-stage skip telemetry: how many posts surfaced this run
    were filtered as "already found in a previous run within the 90-day
    exhaustion window". Populated by ``discovery.discover_for_operator``
    on completion. Empty for runs that started before the feature shipped.

    `sample_urls` is capped at `sample_cap` (200) — the first N unique
    skipped canonical URLs, in surface order. Used by the UI to show the
    operator which posts got filtered without rendering a list of
    thousands. `total_count` is the unrelated count of dedup hits during
    the run (one URL may have been re-surfaced by multiple vendors)."""
    total_count: int = 0
    sample_urls: list[str] = Field(default_factory=list)
    sample_cap: int = 200
    seeded_urls_count: int = 0
    captured_at: datetime | None = None


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
    # Cross-run dedup skip telemetry (null on runs that predate the feature).
    cross_run_dedup_skips: CrossRunDedupSkips | None = None


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

    skips_raw = slate.get("cross_run_dedup_skips")
    cross_run_dedup_skips = (
        CrossRunDedupSkips(**skips_raw) if isinstance(skips_raw, dict) else None
    )

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
        cross_run_dedup_skips=cross_run_dedup_skips,
    )


class RunCostsResponse(BaseModel):
    """Live cost-breakdown payload for a slate run.

    Shape mirrors the ``slate_runs.cost_breakdown`` Mongo subdoc 1:1 so the
    UI can render whatever structure it cares about without the route
    making display decisions. ``totals.dollars`` is the running grand total
    (provider fees + LLM token cost). ``llm.totals.prompt_tokens`` /
    ``completion_tokens`` show aggregate LLM usage.
    """
    slate_run_id: str
    updated_at: datetime | None = None
    totals: dict[str, Any] = Field(default_factory=dict)
    providers: dict[str, Any] = Field(default_factory=dict)


@router.get("/runs/{slate_run_id}/costs", response_model=RunCostsResponse)
async def run_costs(
    slate_run_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> RunCostsResponse:
    """Live cost breakdown for one slate run.

    Reads ``slate_runs.cost_breakdown`` — every paid provider call
    (Wiza, Crustdata, APIDirect) and every LLM round-trip (OpenAI,
    Anthropic) records a ``$inc`` event into this subdoc as the run
    progresses, so polling this endpoint mid-flight returns a running
    tally without waiting for the slate to close.

    Response shape:
      totals.calls         — running count across every paid surface
      totals.dollars       — running grand total in USD
      providers.<name>     — per-provider breakdown:
        totals.{count, dollars}
        line_items.<key>.{count, dollars, ...}
        last_at            — wall-clock of the most recent event
      providers.llm.line_items.<model>.{prompt_tokens,completion_tokens,...}
    """
    try:
        slate_oid = ObjectId(slate_run_id)
    except Exception:
        raise HTTPException(400, "Invalid slate_run_id")
    slate = await db.slate_runs.find_one(
        {"_id": slate_oid, "operator_id": user["_id"]},
        {"cost_breakdown": 1, "_id": 1},
    )
    if not slate:
        raise HTTPException(404, "Slate run not found")
    breakdown = slate.get("cost_breakdown") or {}
    return RunCostsResponse(
        slate_run_id=str(slate_oid),
        updated_at=breakdown.get("updated_at"),
        totals=breakdown.get("totals") or {},
        providers={
            k: v for k, v in breakdown.items()
            if k not in ("totals", "updated_at")
        },
    )


class EmailSelectedRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=200)
    to: str | None = None  # default: the operator's own email
    subject: str | None = None


class EmailSelectedResponse(BaseModel):
    message_id: str
    count: int
    to: str


def _icp_label(c: dict[str, Any]) -> str:
    """ICP label that mirrors the today-page logic.

    Prefer the audit's normalized 0-10 score (RULE 14). Fall back to raw
    `total` for slates pre-dating the normalization patch. For ICP-spared
    candidates (inline rubric already qualified them, LLM ICP gate was
    skipped via gates_icp_scoring_spare_inline_icp) show "spared" since
    there is no numeric score to display.
    """
    icp = (c.get("gate_results") or {}).get("icp") or {}
    s = icp.get("score_0_10")
    if s is None:
        s = icp.get("total")
    if s is None:
        return "spared" if icp.get("spared_inline_icp") else "—"
    return f"{s}/10"


def _selected_drafts_html(rows: list[dict[str, Any]], subject: str) -> str:
    """Render the selected candidates as an inlined-CSS email body."""
    blocks: list[str] = []
    for c in rows:
        icp_str = _icp_label(c)
        comment_text = (c.get("comment_text") or "(no drafted comment)").strip()
        post_text = (c.get("post_text") or "")[:600]
        post_url = c.get("post_url") or "#"
        ctype = c.get("comment_type") or "?"
        author = c.get("author_name") or "(unknown author)"
        source = (c.get("source") or "").strip()
        is_contact = source in ("contact_unipile", "contact_seed")
        source_label = "from your contacts" if is_contact else "from keyword search"
        source_bg = "#dcfce7" if is_contact else "#e0e7ff"
        source_fg = "#166534" if is_contact else "#3730a3"
        # Escape minimal HTML — these are LinkedIn strings, no scripts expected,
        # but defensive.
        def esc(s: str) -> str:
            return (
                s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        source_pill = (
            "&nbsp;"
            f"<span style=\"display:inline-block;background:{source_bg};color:{source_fg};"
            f"padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;"
            f"vertical-align:middle;\">{esc(source_label)}</span>"
        )
        blocks.append(
            "<div style=\"margin:0 0 28px 0;padding:16px;border:1px solid #e5e7eb;"
            "border-radius:12px;font-family:-apple-system,Segoe UI,Roboto,sans-serif;\">"
            f"<div style=\"font-size:12px;color:#6b7280;margin-bottom:6px;\">"
            f"Type {esc(ctype)} · ICP {esc(icp_str)} · {esc(author)}{source_pill}</div>"
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


# ── Comment-engagement tracker ──────────────────────────────────────────
# Surfaces the data the every-2h engagement_poller writes (our_comments
# snapshots + replies + reply_drafter follow-ups) so the operator can see
# per-comment outcomes inline. GET reads existing state; POST triggers a
# Unipile poll for the selected (shipped) candidates and returns the
# refreshed state in one round trip.

class TrackerReply(BaseModel):
    id: str
    text: str
    author_name: str | None = None
    author_linkedin_url: str | None = None
    author_is_post_owner: bool = False
    published_at: datetime | None = None
    suggested_reply: str = ""
    suggested_reply_type: str = ""
    user_action: Literal["pending", "sent", "dismissed", "edited"] = "pending"


class TrackerCandidate(BaseModel):
    candidate_id: str
    cofounder_id: str
    status: str
    shipped_at: datetime | None = None
    post_url: str
    post_text_preview: str = ""
    author_name: str | None = None
    our_comment_text: str = ""
    our_comment_id: str | None = None
    our_comment_status: str = "not_found"
    # Per-our-comment engagement (from Unipile /posts/comments).
    latest_reaction_count: int = 0
    latest_reply_count: int = 0
    latest_polled_at: datetime | None = None
    # Parent-post (the original LinkedIn post we commented on)
    # engagement from APIdirect /v1/linkedin/post — likes, total
    # comments count, shares, and the LinkedIn-style reactions
    # breakdown (like/celebrate/support/love/insightful/funny → counts).
    post_likes: int = 0
    post_comments_total: int = 0
    post_shares: int = 0
    post_reactions: dict[str, int] | None = None
    post_polled_at: datetime | None = None
    replies: list[TrackerReply] = Field(default_factory=list)


class TrackerResponse(BaseModel):
    slate_run_id: str
    candidates: list[TrackerCandidate]
    polled_at: datetime | None = None


class TrackSelectedRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=200)


def _preview_text(s: str | None, limit: int = 220) -> str:
    if not s:
        return ""
    t = s.strip().replace("\n", " ")
    return t if len(t) <= limit else t[: limit - 1] + "…"


async def _lazy_fetch_parent_post_engagement(
    db: AsyncIOMotorDatabase,
    *,
    candidates: list[dict[str, Any]],
) -> None:
    """For each candidate with no `parent_post_polled_at`, call APIdirect
    /v1/linkedin/post and persist the engagement (likes, comments_total,
    shares, reactions_by_type). Caps at 20 calls per request — we don't
    want the GET endpoint to blow through APIdirect quota on a huge
    slate. Anything beyond the cap stays unfetched and shows zeros until
    the every-2h beat or an explicit Track-now picks it up.

    Always marks `parent_post_polled_at` (even on 404 / parse-fail) so
    the next page load doesn't keep retrying.
    """
    if not candidates:
        return
    import asyncio
    from pymongo import MongoClient
    from app.config import settings as _settings
    from app.services import apidirect as _apidirect

    MAX = 20
    targets = candidates[:MAX]

    def _blocking() -> None:
        client = MongoClient(_settings.mongodb_uri)
        try:
            sdb = client[_settings.mongodb_db]
            for c in targets:
                url = (c.get("post_url") or "").strip()
                if not url:
                    continue
                try:
                    d = _apidirect.get_linkedin_post_details(url)
                except _apidirect.ApiDirectQuotaExhausted:
                    log.warning("lazy_fetch: APIdirect quota exhausted")
                    break
                except _apidirect.ApiDirectError as err:
                    log.warning("lazy_fetch: APIdirect error url=%s: %s", url[:60], err)
                    d = None
                except _apidirect.ApiDirectNotConfigured:
                    return
                except Exception as err:  # noqa: BLE001
                    log.warning("lazy_fetch: unexpected: %s", err)
                    d = None
                now = utcnow()
                if d is None:
                    sdb.candidates.update_one(
                        {"_id": c["_id"]},
                        {"$set": {"parent_post_polled_at": now}},
                    )
                    continue
                sdb.candidates.update_one(
                    {"_id": c["_id"]},
                    {"$set": {
                        "parent_post_likes": int(d.likes or 0),
                        "parent_post_comments_total": int(d.comments or 0),
                        "parent_post_shares": int(d.shares or 0),
                        "parent_post_reactions": d.reactions_by_type or None,
                        "parent_post_polled_at": now,
                    }},
                )
        finally:
            client.close()

    await asyncio.to_thread(_blocking)


async def _build_tracker_response(
    db: AsyncIOMotorDatabase,
    *,
    slate_run_id: ObjectId,
    operator_id: ObjectId,
    polled_at: datetime | None = None,
) -> TrackerResponse:
    """Single Mongo join over candidates × our_comments × replies for
    a slate run, scoped to the operator. Used by both the GET (read-
    only) and POST (after poll) endpoints so the response shape is
    identical regardless of trigger.
    """
    cands = await db.candidates.find(
        {"slate_run_id": slate_run_id, "operator_id": operator_id},
        {
            "cofounder_id": 1, "status": 1, "shipped_at": 1, "post_url": 1,
            "post_text": 1, "comment_text": 1, "author_name": 1,
            # APIdirect parent-post fields (engagement_poller writes them).
            "parent_post_likes": 1, "parent_post_comments_total": 1,
            "parent_post_shares": 1, "parent_post_reactions": 1,
            "parent_post_polled_at": 1,
        },
    ).to_list(length=None)
    # Restrict to candidates with a draft (drafted/slated/shipped/dropped_by_user)
    # — the tracker has nothing to show for raw/gate_dropped/etc.
    relevant_statuses = {"drafted", "slated", "shipped", "dropped_by_user"}
    cands = [c for c in cands if c.get("status") in relevant_statuses]
    if not cands:
        return TrackerResponse(
            slate_run_id=str(slate_run_id),
            candidates=[],
            polled_at=polled_at,
        )

    # Lazy-fetch APIdirect parent-post engagement for any candidate that
    # has never been polled. Without this, the first page-load after a
    # ship shows all zeros until either the every-2h beat runs or the
    # operator clicks "Track now". With it, the page is fresh on
    # first load. Cost: one APIdirect call ($0.002) per never-polled
    # post per slate, paid once per post.
    pending = [
        c for c in cands
        if not c.get("parent_post_polled_at")
        and (c.get("post_url") or "").startswith("https://www.linkedin.com/")
    ]
    if pending:
        await _lazy_fetch_parent_post_engagement(db, candidates=pending)
        # Re-read the freshly-updated fields so the response reflects them
        # without a full re-query.
        refreshed = await db.candidates.find(
            {"_id": {"$in": [c["_id"] for c in pending]}},
            {
                "parent_post_likes": 1, "parent_post_comments_total": 1,
                "parent_post_shares": 1, "parent_post_reactions": 1,
                "parent_post_polled_at": 1,
            },
        ).to_list(length=len(pending))
        by_id = {r["_id"]: r for r in refreshed}
        for c in cands:
            r = by_id.get(c["_id"])
            if r:
                c.update({k: v for k, v in r.items() if k != "_id"})

    cand_ids = [c["_id"] for c in cands]
    ocs = await db.our_comments.find(
        {"candidate_id": {"$in": cand_ids}, "operator_id": operator_id}
    ).to_list(length=len(cand_ids))
    oc_by_cand = {oc["candidate_id"]: oc for oc in ocs}

    replies_docs = await db.replies.find(
        {"candidate_id": {"$in": cand_ids}, "operator_id": operator_id}
    ).sort("detected_at", 1).to_list(length=None)
    replies_by_cand: dict[ObjectId, list[dict[str, Any]]] = {}
    for r in replies_docs:
        replies_by_cand.setdefault(r["candidate_id"], []).append(r)

    out: list[TrackerCandidate] = []
    for c in cands:
        oc = oc_by_cand.get(c["_id"])
        our_comment_text = c.get("comment_text") or (oc or {}).get("text") or ""
        our_comment_id = (oc or {}).get("comment_id")
        # Status derivation: if our_comments doc exists and has a Unipile
        # comment_id, we know LinkedIn has our comment. Otherwise it's
        # either queued (outbox waiting) or never sent.
        if our_comment_id:
            oc_status = "detected"
        elif oc:
            oc_status = (oc.get("status") or "queued")
        else:
            oc_status = "not_found"

        rs = []
        for r in replies_by_cand.get(c["_id"], []):
            rs.append(TrackerReply(
                id=str(r["_id"]),
                text=r.get("reply_text") or "",
                author_name=r.get("reply_author_name"),
                author_linkedin_url=r.get("reply_author_linkedin_url"),
                author_is_post_owner=bool(r.get("reply_author_is_post_owner")),
                published_at=r.get("reply_published_at"),
                suggested_reply=r.get("suggested_reply") or "",
                suggested_reply_type=r.get("suggested_reply_type") or "",
                user_action=(r.get("user_action") or "pending"),
            ))

        # Sanitize the parent-post reactions dict (downstream Pydantic
        # rejects non-int values). The engagement_poller already
        # int-coerces, but a hand-written DB row could slip through.
        reactions_raw = c.get("parent_post_reactions")
        reactions: dict[str, int] | None = None
        if isinstance(reactions_raw, dict) and reactions_raw:
            reactions = {
                str(k): int(v)
                for k, v in reactions_raw.items()
                if isinstance(v, (int, float))
            } or None

        out.append(TrackerCandidate(
            candidate_id=str(c["_id"]),
            cofounder_id=str(c.get("cofounder_id") or ""),
            status=c.get("status") or "",
            shipped_at=c.get("shipped_at"),
            post_url=c.get("post_url") or "",
            post_text_preview=_preview_text(c.get("post_text") or "", 220),
            author_name=c.get("author_name"),
            our_comment_text=our_comment_text,
            our_comment_id=our_comment_id,
            our_comment_status=oc_status,
            latest_reaction_count=int((oc or {}).get("latest_reaction_count") or 0),
            latest_reply_count=int((oc or {}).get("latest_reply_count") or 0),
            latest_polled_at=(oc or {}).get("latest_polled_at"),
            post_likes=int(c.get("parent_post_likes") or 0),
            post_comments_total=int(c.get("parent_post_comments_total") or 0),
            post_shares=int(c.get("parent_post_shares") or 0),
            post_reactions=reactions,
            post_polled_at=c.get("parent_post_polled_at"),
            replies=rs,
        ))

    # Stable sort: shipped first (newest shipped_at), then everyone else.
    def _sort_key(t: TrackerCandidate):
        shipped_rank = 0 if t.status == "shipped" else 1
        ts = -(t.shipped_at.timestamp() if t.shipped_at else 0)
        return (shipped_rank, ts)
    out.sort(key=_sort_key)

    return TrackerResponse(
        slate_run_id=str(slate_run_id),
        candidates=out,
        polled_at=polled_at,
    )


@router.get(
    "/runs/{slate_run_id}/tracker",
    response_model=TrackerResponse,
)
async def read_tracker(
    slate_run_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TrackerResponse:
    """Read the tracker state without re-polling Unipile. Returns
    whatever the every-2h beat (or the last on-demand POST) already
    wrote to our_comments / replies."""
    if not ObjectId.is_valid(slate_run_id):
        raise HTTPException(400, "Invalid slate_run_id")
    slate = await db.slate_runs.find_one(
        {"_id": ObjectId(slate_run_id), "operator_id": user["_id"]}, {"_id": 1}
    )
    if not slate:
        raise HTTPException(404, "Run not found")
    return await _build_tracker_response(
        db, slate_run_id=ObjectId(slate_run_id), operator_id=user["_id"],
    )


@router.post(
    "/runs/{slate_run_id}/track-selected",
    response_model=TrackerResponse,
)
async def track_selected(
    slate_run_id: Annotated[str, Path()],
    payload: TrackSelectedRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TrackerResponse:
    """On-demand poll. For each selected candidate that's been shipped,
    fetch the post's comment thread from Unipile and refresh our_comments
    + replies. Returns the new aggregated state."""
    if not ObjectId.is_valid(slate_run_id):
        raise HTTPException(400, "Invalid slate_run_id")
    slate = await db.slate_runs.find_one(
        {"_id": ObjectId(slate_run_id), "operator_id": user["_id"]}, {"_id": 1}
    )
    if not slate:
        raise HTTPException(404, "Run not found")

    cand_oids: list[ObjectId] = [
        ObjectId(s) for s in payload.candidate_ids if ObjectId.is_valid(s)
    ]
    if not cand_oids:
        raise HTTPException(400, "No valid candidate ids")

    # Filter to shipped + operator-scoped + matching this run.
    shipped = await db.candidates.find({
        "_id": {"$in": cand_oids},
        "slate_run_id": ObjectId(slate_run_id),
        "operator_id": user["_id"],
        "status": "shipped",
    }).to_list(length=len(cand_oids))

    if shipped:
        # Need cofounders to know which unipile_account_id to use.
        cf_ids = list({c["cofounder_id"] for c in shipped})
        cofounders = await db.cofounders.find(
            {"_id": {"$in": cf_ids}, "operator_id": user["_id"]}
        ).to_list(length=len(cf_ids))
        cf_by_id = {cf["_id"]: cf for cf in cofounders}

        # The poller is sync (Celery-backed). Open a per-request pymongo
        # client and drive it from a thread so the async route doesn't
        # block the event loop. Same pattern celery_app._sync_db() uses.
        import asyncio
        from pymongo import MongoClient
        from app.config import settings as _settings
        from app.engine.engagement_poller import poll_one_candidate

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
                    except Exception:  # noqa: BLE001 — one bad candidate must not abort the rest
                        log.exception("track_selected: poll_one_candidate failed for %s", c.get("_id"))
            finally:
                client.close()

        await asyncio.to_thread(_poll_blocking)

    polled_at = utcnow()
    return await _build_tracker_response(
        db, slate_run_id=ObjectId(slate_run_id), operator_id=user["_id"],
        polled_at=polled_at,
    )
