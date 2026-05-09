"""
Analytics aggregations: overview KPIs, funnel, reply rate by comment type,
reply rate by ICP score band.

All endpoints accept ?range=7d|30d|90d (default 30d) and scope to the current
operator.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, Query
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
