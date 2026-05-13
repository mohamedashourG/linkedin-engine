"""
Analytics aggregations: overview KPIs, funnel, reply rate by comment type,
reply rate by ICP score band.

All endpoints accept ?range=7d|30d|90d (default 30d) and scope to the current
operator.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.auth.deps import CurrentUser
from app.database import get_db
from app.models.common import utcnow

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}


def _cutoff(range_param: str):
    days = _RANGE_DAYS.get(range_param, 30)
    return utcnow() - timedelta(days=days)


# ---------------------------------------------------------------- overview

class OverviewKpis(BaseModel):
    range: str
    shipped: int
    replies: int
    reply_rate: float
    crs_sent: int
    crs_accepted: int
    bookings: int
    converted_leads: int
    active_leads: int
    stalled_leads: int
    sealed_runs: int
    aborted_runs: int


@router.get("/overview", response_model=OverviewKpis)
async def overview(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
) -> OverviewKpis:
    operator_id: ObjectId = user["_id"]
    cutoff = _cutoff(range)

    shipped = await db.candidates.count_documents(
        {"operator_id": operator_id, "status": "shipped", "shipped_at": {"$gte": cutoff}}
    )
    replies = await db.replies.count_documents(
        {"operator_id": operator_id, "detected_at": {"$gte": cutoff}}
    )
    reply_rate = (replies / shipped) if shipped > 0 else 0.0

    crs_sent = await db.leads.count_documents(
        {"operator_id": operator_id, "cr_sent_at": {"$gte": cutoff}}
    )
    crs_accepted = await db.leads.count_documents(
        {"operator_id": operator_id, "cr_accepted_at": {"$gte": cutoff}}
    )
    bookings = await db.bookings.count_documents(
        {"operator_id": operator_id, "booked_at": {"$gte": cutoff}}
    )

    converted = await db.leads.count_documents(
        {"operator_id": operator_id, "status": "CONVERTED"}
    )
    active = await db.leads.count_documents(
        {"operator_id": operator_id, "status": "ACTIVE"}
    )
    stalled = await db.leads.count_documents(
        {"operator_id": operator_id, "status": "STALLED"}
    )

    sealed = await db.slate_runs.count_documents(
        {"operator_id": operator_id, "status": "sealed", "created_at": {"$gte": cutoff}}
    )
    aborted = await db.slate_runs.count_documents(
        {
            "operator_id": operator_id,
            "status": "force_aborted",
            "created_at": {"$gte": cutoff},
        }
    )

    return OverviewKpis(
        range=range,
        shipped=shipped,
        replies=replies,
        reply_rate=round(reply_rate, 3),
        crs_sent=crs_sent,
        crs_accepted=crs_accepted,
        bookings=bookings,
        converted_leads=converted,
        active_leads=active,
        stalled_leads=stalled,
        sealed_runs=sealed,
        aborted_runs=aborted,
    )


# ---------------------------------------------------------------- funnel

class FunnelStage(BaseModel):
    name: str
    count: int


class FunnelResponse(BaseModel):
    range: str
    stages: list[FunnelStage]


@router.get("/funnel", response_model=FunnelResponse)
async def funnel(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
) -> FunnelResponse:
    operator_id: ObjectId = user["_id"]
    cutoff = _cutoff(range)

    shipped = await db.candidates.count_documents(
        {"operator_id": operator_id, "status": "shipped", "shipped_at": {"$gte": cutoff}}
    )
    replies = await db.replies.count_documents(
        {"operator_id": operator_id, "detected_at": {"$gte": cutoff}}
    )
    crs_sent = await db.leads.count_documents(
        {"operator_id": operator_id, "cr_sent_at": {"$gte": cutoff}}
    )
    crs_accepted = await db.leads.count_documents(
        {"operator_id": operator_id, "cr_accepted_at": {"$gte": cutoff}}
    )
    dms = await db.leads.count_documents(
        {"operator_id": operator_id, "dm_sent_at": {"$gte": cutoff}}
    )
    bookings = await db.bookings.count_documents(
        {"operator_id": operator_id, "booked_at": {"$gte": cutoff}}
    )

    return FunnelResponse(
        range=range,
        stages=[
            FunnelStage(name="Comments shipped", count=shipped),
            FunnelStage(name="Replies received", count=replies),
            FunnelStage(name="CRs sent", count=crs_sent),
            FunnelStage(name="CRs accepted", count=crs_accepted),
            FunnelStage(name="DMs sent", count=dms),
            FunnelStage(name="Bookings", count=bookings),
        ],
    )


# ---------------------------------------------------------------- by type

class TypeBreakdown(BaseModel):
    type: str
    shipped: int
    replies: int
    reply_rate: float


class ByTypeResponse(BaseModel):
    range: str
    rows: list[TypeBreakdown]


@router.get("/by_type", response_model=ByTypeResponse)
async def by_type(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
) -> ByTypeResponse:
    operator_id: ObjectId = user["_id"]
    cutoff = _cutoff(range)

    shipped_pipeline = [
        {
            "$match": {
                "operator_id": operator_id,
                "status": "shipped",
                "shipped_at": {"$gte": cutoff},
                "comment_type": {"$ne": None},
            }
        },
        {"$group": {"_id": "$comment_type", "shipped": {"$sum": 1}}},
    ]
    shipped_by_type = {
        r["_id"]: r["shipped"]
        async for r in db.candidates.aggregate(shipped_pipeline)
    }

    reply_pipeline = [
        {"$match": {"operator_id": operator_id, "detected_at": {"$gte": cutoff}}},
        {
            "$lookup": {
                "from": "candidates",
                "localField": "candidate_id",
                "foreignField": "_id",
                "as": "candidate",
            }
        },
        {"$unwind": "$candidate"},
        {"$group": {"_id": "$candidate.comment_type", "replies": {"$sum": 1}}},
    ]
    replies_by_type = {
        r["_id"]: r["replies"] async for r in db.replies.aggregate(reply_pipeline)
    }

    rows: list[TypeBreakdown] = []
    for t in ("A", "B", "C", "D", "E", "F"):
        s = int(shipped_by_type.get(t, 0))
        r = int(replies_by_type.get(t, 0))
        rate = round(r / s, 3) if s > 0 else 0.0
        rows.append(TypeBreakdown(type=t, shipped=s, replies=r, reply_rate=rate))
    return ByTypeResponse(range=range, rows=rows)


# ---------------------------------------------------------------- by score

class ScoreBand(BaseModel):
    band: str
    shipped: int
    replies: int
    reply_rate: float


class ByScoreResponse(BaseModel):
    range: str
    rows: list[ScoreBand]


def _band_for_score(score: int) -> str:
    if score >= 15:
        return "15+"
    if score >= 10:
        return "10-14"
    if score >= 6:
        return "6-9"
    return "0-5"


@router.get("/by_score", response_model=ByScoreResponse)
async def by_score(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
) -> ByScoreResponse:
    operator_id: ObjectId = user["_id"]
    cutoff = _cutoff(range)

    shipped_cur = db.candidates.find(
        {
            "operator_id": operator_id,
            "status": "shipped",
            "shipped_at": {"$gte": cutoff},
        },
        {"_id": 1, "gate_results": 1},
    )
    band_shipped: dict[str, int] = {"0-5": 0, "6-9": 0, "10-14": 0, "15+": 0}
    cand_to_band: dict[ObjectId, str] = {}
    async for c in shipped_cur:
        score = int(((c.get("gate_results") or {}).get("icp") or {}).get("total") or 0)
        band = _band_for_score(score)
        band_shipped[band] = band_shipped.get(band, 0) + 1
        cand_to_band[c["_id"]] = band

    band_replies: dict[str, int] = {"0-5": 0, "6-9": 0, "10-14": 0, "15+": 0}
    if cand_to_band:
        reply_cur = db.replies.find(
            {
                "operator_id": operator_id,
                "detected_at": {"$gte": cutoff},
                "candidate_id": {"$in": list(cand_to_band.keys())},
            },
            {"candidate_id": 1},
        )
        async for r in reply_cur:
            band = cand_to_band.get(r["candidate_id"])
            if band:
                band_replies[band] = band_replies.get(band, 0) + 1

    rows: list[ScoreBand] = []
    for band in ("0-5", "6-9", "10-14", "15+"):
        s = band_shipped.get(band, 0)
        r = band_replies.get(band, 0)
        rate = round(r / s, 3) if s > 0 else 0.0
        rows.append(ScoreBand(band=band, shipped=s, replies=r, reply_rate=rate))
    return ByScoreResponse(range=range, rows=rows)


# ---------------------------------------------------------------- gate funnel

class GateDropPost(BaseModel):
    candidate_id: str
    post_url: str | None
    author_name: str | None
    author_title: str | None
    post_text: str
    drop_reason: str
    source: str | None
    matched_keyword: str | None
    gate_rationale: str | None


class GateDropGroup(BaseModel):
    stage: str
    reason: str
    label: str
    description: str
    count: int
    posts: list[GateDropPost]


class GateStage(BaseModel):
    key: str
    label: str
    count: int


class GateFunnelResponse(BaseModel):
    slate_run_id: str | None
    run_date: str | None
    status: str | None
    scope: str  # "run" or "aggregate"
    range: str | None  # for aggregate scope, e.g. "30d"
    runs_included: int  # for aggregate: number of slate_runs summed
    stages: list[GateStage]
    drops: list[GateDropGroup]


_REASON_META: dict[str, dict[str, str]] = {
    "inline_geo": {
        "label": "Geo not matched",
        "description": "Author's profile location didn't match any of the operator's target geographies.",
    },
    "inline_no_profile": {
        "label": "No enriched profile",
        "description": "Couldn't fetch the author's profile (LinkedIn rate limit or per-run fetch budget exhausted), so geo/title couldn't be verified.",
    },
    "inline_rubric": {
        "label": "Rubric below threshold",
        "description": "Author title + industry + post keywords all scored too low to qualify on either path.",
    },
    "comments_disabled": {
        "label": "Comments disabled",
        "description": "LinkedIn post has comments turned off — cannot engage.",
    },
    "rejected_url_mismatch": {
        "label": "Verification rejected",
        "description": "Post too old, snippet empty/thin, or post URL didn't resolve. Filtered before any LLM cost.",
    },
    "non_buyer": {
        "label": "Non-buyer voice",
        "description": "LLM judged the post as recruiter, vendor, news share or other non-buyer voice.",
    },
    "post_quality": {
        "label": "Low post quality",
        "description": "Post was too short, promotional, or otherwise low-signal to be worth engaging.",
    },
    "analyst": {
        "label": "Analyst reportage",
        "description": "LLM judged the post as 3rd-party reportage/commentary rather than an authentic operator voice.",
    },
    "icp_low": {
        "label": "ICP below threshold",
        "description": "Author scored below the rubric threshold across title / industry / geo / stage.",
    },
    "drafter_error": {
        "label": "Drafter LLM error",
        "description": "The drafting LLM call failed or returned an invalid result.",
    },
    "drafter_no_cofounder": {
        "label": "No cofounder mapped",
        "description": "Candidate had no cofounder assigned to draft for (data inconsistency).",
    },
    "validator": {
        "label": "Validator rejected draft",
        "description": "The draft contained banned tokens (em-dashes, ellipses), banned buzzwords, sycophantic openers, or other rule violations.",
    },
    "gate_error_unexpected": {
        "label": "Unexpected gate error",
        "description": "An exception was raised during gate evaluation (transient API or code bug).",
    },
    "error_non_buyer": {
        "label": "Non-buyer gate error",
        "description": "Exception inside the non_buyer gate (transient LLM/network failure).",
    },
    "error_quality": {
        "label": "Post-quality gate error",
        "description": "Exception inside the post_quality gate.",
    },
    "error_analyst": {
        "label": "Analyst gate error",
        "description": "Exception inside the analyst_reportage gate.",
    },
    "error_icp": {
        "label": "ICP gate error",
        "description": "Exception inside the icp_scoring gate.",
    },
    "validator_retries_exhausted": {
        "label": "Validator retries exhausted",
        "description": "Drafter re-drafted N times with corrective feedback, every attempt still failed the validator. Inspect the drafter_attempts_log on the candidate doc to see which validation rule kept firing.",
    },
    "other": {
        "label": "Other",
        "description": "Drop reason couldn't be classified into a known bucket — inspect the raw reason text.",
    },
}

_REASON_TO_STAGE = {
    "inline_geo": "discovery",
    "inline_no_profile": "discovery",
    "inline_rubric": "discovery",
    "comments_disabled": "verification",
    "rejected_url_mismatch": "verification",
    "non_buyer": "cheap_gates",
    "post_quality": "cheap_gates",
    "error_non_buyer": "cheap_gates",
    "error_quality": "cheap_gates",
    "analyst": "expensive_gates",
    "icp_low": "expensive_gates",
    "error_analyst": "expensive_gates",
    "error_icp": "expensive_gates",
    "drafter_error": "drafter",
    "drafter_no_cofounder": "drafter",
    "validator": "drafter",
    "validator_retries_exhausted": "drafter",
    "gate_error_unexpected": "expensive_gates",
}


def _classify_drop_reason(status: str | None, reason: str | None) -> str:
    if status == "rejected_url_mismatch":
        return "rejected_url_mismatch"
    if not reason:
        return "other"
    head = reason.split(":", 1)[0].strip()
    return head if head in _REASON_TO_STAGE else "other"


def _extract_rationale(gate_results: dict[str, Any] | None, key: str) -> str | None:
    """Pull a one-line rationale from the gate's structured result when present."""
    if not gate_results:
        return None
    blob: Any = None
    if key in ("non_buyer", "post_quality", "analyst"):
        blob = gate_results.get(key)
    elif key == "icp_low":
        blob = gate_results.get("icp")
    if isinstance(blob, dict):
        for f in ("rationale", "reason"):
            v = blob.get(f)
            if v:
                return str(v)[:300]
    return None


@router.get("/gate-funnel", response_model=GateFunnelResponse)
async def gate_funnel(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    slate_run_id: Annotated[str | None, Query()] = None,
    aggregate: Annotated[bool, Query()] = False,
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
    posts_per_reason: Annotated[int, Query(ge=1, le=200)] = 50,
) -> GateFunnelResponse:
    """Discovery → slate gate funnel. Two scopes:

    - **single run** (default): pass `slate_run_id`, or omit to use the latest
      slate_run for the operator. Returns per-stage counts + grouped drop posts
      scoped to that one run.
    - **aggregate**: set `aggregate=true` with `range=7d|30d|90d`. Sums across
      every slate_run whose `run_date` falls inside the window."""
    operator_id: ObjectId = user["_id"]

    scope = "aggregate" if aggregate else "run"

    if aggregate:
        cutoff = _cutoff(range)
        run_ids = [
            r["_id"]
            async for r in db.slate_runs.find(
                {"operator_id": operator_id,
                 "$or": [
                     {"run_date": {"$gte": cutoff}},
                     {"created_at": {"$gte": cutoff}},
                 ]},
                {"_id": 1},
            )
        ]
        if not run_ids:
            return GateFunnelResponse(
                slate_run_id=None, run_date=None, status=None,
                scope="aggregate", range=range, runs_included=0,
                stages=[], drops=[],
            )
        q = {"operator_id": operator_id, "slate_run_id": {"$in": run_ids}}
        run_meta = {
            "id": None, "run_date_iso": None, "status": None,
            "runs_included": len(run_ids),
        }
    else:
        if slate_run_id:
            try:
                run = await db.slate_runs.find_one(
                    {"_id": ObjectId(slate_run_id), "operator_id": operator_id}
                )
            except Exception:
                run = None
        else:
            run = await db.slate_runs.find_one(
                {"operator_id": operator_id},
                sort=[("created_at", -1)],
            )
        if not run:
            return GateFunnelResponse(
                slate_run_id=None, run_date=None, status=None,
                scope="run", range=None, runs_included=0,
                stages=[], drops=[],
            )
        run_id = run["_id"]
        run_date_val = run.get("run_date") or run.get("created_at")
        q = {"operator_id": operator_id, "slate_run_id": run_id}
        run_meta = {
            "id": str(run_id),
            "run_date_iso": run_date_val.isoformat() if run_date_val else None,
            "status": run.get("status"),
            "runs_included": 1,
        }

    status_counts: dict[str, int] = {}
    async for d in db.candidates.aggregate([
        {"$match": q},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]):
        status_counts[d["_id"] or "?"] = int(d.get("n", 0))

    total = sum(status_counts.values())
    # A candidate's status is overwritten as it progresses. "made it past stage X"
    # = count of statuses downstream of X, plus the dropped statuses that imply
    # it passed earlier stages.
    downstream_of = {
        "verified": {"verified", "cheap_gate_passed", "gate_passed",
                     "allocated", "drafted", "slated", "shipped",
                     "gate_dropped"},
        "cheap_passed": {"cheap_gate_passed", "gate_passed",
                         "allocated", "drafted", "slated", "shipped"},
        "gate_passed": {"gate_passed", "allocated", "drafted",
                        "slated", "shipped"},
        "drafted": {"drafted", "slated", "shipped"},
        "slated": {"slated", "shipped"},
    }
    verified_total = sum(status_counts.get(s, 0) for s in downstream_of["verified"])
    cheap_passed = sum(status_counts.get(s, 0) for s in downstream_of["cheap_passed"])
    gate_passed = sum(status_counts.get(s, 0) for s in downstream_of["gate_passed"])
    drafted = sum(status_counts.get(s, 0) for s in downstream_of["drafted"])
    slated = sum(status_counts.get(s, 0) for s in downstream_of["slated"])

    stages = [
        GateStage(key="discovered", label="Discovered", count=total),
        GateStage(key="verified", label="Verified", count=verified_total),
        GateStage(key="cheap_passed", label="Cheap gates passed", count=cheap_passed),
        GateStage(key="gate_passed", label="ICP/Analyst passed", count=gate_passed),
        GateStage(key="drafted", label="Drafted", count=drafted),
        GateStage(key="slated", label="Slated", count=slated),
    ]

    grouped: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    cursor = db.candidates.find(
        {**q, "status": {"$in": ["gate_dropped", "rejected_url_mismatch", "rejected_inline"]}},
        {
            "status": 1, "drop_reason": 1, "gate_results": 1,
            "post_url": 1, "author_name": 1, "author_title": 1,
            "post_text": 1, "source": 1, "matched_keyword": 1,
        },
    ).sort("updated_at", -1)
    async for c in cursor:
        key = _classify_drop_reason(c.get("status"), c.get("drop_reason"))
        counts[key] = counts.get(key, 0) + 1
        bucket = grouped.setdefault(key, [])
        if len(bucket) < posts_per_reason:
            bucket.append(c)

    drops: list[GateDropGroup] = []
    order = ["inline_geo", "inline_no_profile", "inline_rubric",
             "comments_disabled", "rejected_url_mismatch", "non_buyer", "post_quality",
             "analyst", "icp_low", "drafter_error", "validator",
             "validator_retries_exhausted",
             "drafter_no_cofounder", "gate_error_unexpected",
             "error_non_buyer", "error_quality", "error_analyst",
             "error_icp", "other"]
    seen_keys: set[str] = set()
    for key in [k for k in order if k in counts] + [k for k in counts if k not in order]:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        bucket_docs = grouped.get(key, [])
        posts = [
            GateDropPost(
                candidate_id=str(c["_id"]),
                post_url=c.get("post_url"),
                author_name=c.get("author_name"),
                author_title=c.get("author_title"),
                post_text=(c.get("post_text") or "")[:500],
                drop_reason=c.get("drop_reason") or c.get("status") or "",
                source=c.get("source"),
                matched_keyword=c.get("matched_keyword"),
                gate_rationale=_extract_rationale(c.get("gate_results"), key),
            )
            for c in bucket_docs
        ]
        meta = _REASON_META.get(key, {"label": key, "description": ""})
        drops.append(
            GateDropGroup(
                stage=_REASON_TO_STAGE.get(key, "verification" if key == "rejected_url_mismatch" else "other"),
                reason=key,
                label=meta["label"],
                description=meta["description"],
                count=counts[key],
                posts=posts,
            )
        )

    return GateFunnelResponse(
        slate_run_id=run_meta["id"],
        run_date=run_meta["run_date_iso"],
        status=run_meta["status"],
        scope=scope,
        range=range if scope == "aggregate" else None,
        runs_included=run_meta["runs_included"],
        stages=stages,
        drops=drops,
    )


# ---------------------------------------------------------------- comment lifecycle


class OurCommentRow(BaseModel):
    id: str
    comment_id: str | None
    parent_post_url: str
    status: str
    text: str
    posted_at: datetime | None
    latest_reaction_count: int
    latest_reply_count: int
    candidate_id: str


class CommentsListResponse(BaseModel):
    range: str
    items: list[OurCommentRow]


@router.get("/comments", response_model=CommentsListResponse)
async def list_our_comments(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
) -> CommentsListResponse:
    operator_id: ObjectId = user["_id"]
    cutoff = _cutoff(range)
    cursor = db.our_comments.find(
        {"operator_id": operator_id, "created_at": {"$gte": cutoff}}
    ).sort("created_at", -1).limit(200)
    items: list[OurCommentRow] = []
    async for row in cursor:
        items.append(
            OurCommentRow(
                id=str(row["_id"]),
                comment_id=row.get("comment_id"),
                parent_post_url=row.get("parent_post_url") or "",
                status=str(row.get("status") or ""),
                text=(row.get("text") or "")[:2000],
                posted_at=row.get("posted_at"),
                latest_reaction_count=int(row.get("latest_reaction_count") or 0),
                latest_reply_count=int(row.get("latest_reply_count") or 0),
                candidate_id=str(row.get("candidate_id") or ""),
            )
        )
    return CommentsListResponse(range=range, items=items)


class CommentTimeseriesPoint(BaseModel):
    polled_at: datetime
    reaction_count: int
    reply_count: int


class CommentTimeseriesResponse(BaseModel):
    comment_id: str
    points: list[CommentTimeseriesPoint]


@router.get("/comments/{comment_id}/timeseries", response_model=CommentTimeseriesResponse)
async def comment_timeseries(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    comment_id: str,
) -> CommentTimeseriesResponse:
    operator_id: ObjectId = user["_id"]
    oc = await db.our_comments.find_one(
        {"operator_id": operator_id, "comment_id": comment_id}
    )
    if not oc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    pts: list[CommentTimeseriesPoint] = []
    cursor = db.comment_engagement_snapshots.find({"comment_id": comment_id}).sort(
        "polled_at", 1
    ).limit(500)
    async for s in cursor:
        pts.append(
            CommentTimeseriesPoint(
                polled_at=s["polled_at"],
                reaction_count=int(s.get("reaction_count") or 0),
                reply_count=int(s.get("reply_count") or 0),
            )
        )
    return CommentTimeseriesResponse(comment_id=comment_id, points=pts)


class OperatorCommentSummary(BaseModel):
    range: str
    comments_sent: int
    replies_detected: int
    avg_reactions: float


@router.get("/operator-summary", response_model=OperatorCommentSummary)
async def operator_comment_summary(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",
) -> OperatorCommentSummary:
    operator_id: ObjectId = user["_id"]
    cutoff = _cutoff(range)
    sent = await db.our_comments.count_documents(
        {
            "operator_id": operator_id,
            "status": "sent",
            "posted_at": {"$gte": cutoff},
        }
    )
    replies_n = await db.replies.count_documents(
        {"operator_id": operator_id, "detected_at": {"$gte": cutoff}}
    )
    pipeline = [
        {
            "$match": {
                "operator_id": operator_id,
                "status": "sent",
                "posted_at": {"$gte": cutoff},
            }
        },
        {"$group": {"_id": None, "avg": {"$avg": "$latest_reaction_count"}}},
    ]
    agg = await db.our_comments.aggregate(pipeline).to_list(length=1)
    avg_r = float(agg[0]["avg"]) if agg and agg[0].get("avg") is not None else 0.0
    return OperatorCommentSummary(
        range=range,
        comments_sent=sent,
        replies_detected=replies_n,
        avg_reactions=round(avg_r, 3),
    )


class PostThreadMessage(BaseModel):
    kind: str
    at: datetime | None
    text: str
    author: str | None = None


class PostThreadResponse(BaseModel):
    candidate_id: str
    messages: list[PostThreadMessage]


@router.get("/post-thread/{candidate_id}", response_model=PostThreadResponse)
async def post_thread(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    candidate_id: str,
) -> PostThreadResponse:
    if not ObjectId.is_valid(candidate_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid candidate id")
    oid = ObjectId(candidate_id)
    c = await db.candidates.find_one({"_id": oid, "operator_id": user["_id"]})
    if not c:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    msgs: list[PostThreadMessage] = []
    msgs.append(
        PostThreadMessage(
            kind="post",
            at=c.get("post_published_at"),
            text=(c.get("post_text") or "")[:4000],
            author=c.get("author_name"),
        )
    )
    if c.get("comment_text"):
        msgs.append(
            PostThreadMessage(
                kind="our_draft",
                at=c.get("updated_at"),
                text=c.get("comment_text") or "",
                author=None,
            )
        )
    async for r in db.replies.find({"candidate_id": oid}).sort("detected_at", 1):
        msgs.append(
            PostThreadMessage(
                kind="reply",
                at=r.get("detected_at"),
                text=r.get("reply_text") or "",
                author=r.get("reply_author_name"),
            )
        )
    return PostThreadResponse(candidate_id=candidate_id, messages=msgs)
