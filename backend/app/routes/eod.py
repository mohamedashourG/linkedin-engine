"""
EOD form. GET pre-fills with today's auto-counts; POST persists the log and
queues the nightly batch for the operator.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth.deps import CurrentUser
from app.celery_app import nightly_run as nightly_run_task
from app.database import get_db
from app.models.common import utcnow
from app.models.eod import (
    EodCofounderPrefill,
    EodPrefillResponse,
    EodSubmitRequest,
    EodSubmitResponse,
)

router = APIRouter(prefix="/api/eod", tags=["eod"])


@router.get("/today", response_model=EodPrefillResponse)
async def prefill(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> EodPrefillResponse:
    today_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
    cofounders = await db.cofounders.find(
        {"operator_id": user["_id"], "active": True}
    ).to_list(length=None)

    per_rows: list[EodCofounderPrefill] = []
    for cf in cofounders:
        cf_id = cf["_id"]
        shipped = await db.candidates.count_documents(
            {
                "operator_id": user["_id"],
                "cofounder_id": cf_id,
                "status": "shipped",
                "shipped_at": {"$gte": today_start},
            }
        )
        dropped = await db.candidates.count_documents(
            {
                "operator_id": user["_id"],
                "cofounder_id": cf_id,
                "status": "dropped_by_user",
                "updated_at": {"$gte": today_start},
            }
        )
        edited = await db.candidates.count_documents(
            {
                "operator_id": user["_id"],
                "cofounder_id": cf_id,
                "user_action": "edited",
                "updated_at": {"$gte": today_start},
            }
        )
        replies_count = await db.replies.count_documents(
            {
                "operator_id": user["_id"],
                "cofounder_id": cf_id,
                "detected_at": {"$gte": today_start},
            }
        )
        bookings_count = await db.bookings.count_documents(
            {
                "operator_id": user["_id"],
                "cofounder_id": cf_id,
                "booked_at": {"$gte": today_start},
            }
        )
        per_rows.append(
            EodCofounderPrefill(
                cofounder_id=str(cf_id),
                cofounder_name=cf["display_name"],
                shipped=shipped,
                dropped=dropped,
                edited=edited,
                replies_count=replies_count,
                bookings_count=bookings_count,
            )
        )

    last = await db.eod_logs.find_one(
        {"operator_id": user["_id"]}, sort=[("submitted_at", -1)]
    )
    return EodPrefillResponse(
        log_date=date.today(),
        per_cofounder=per_rows,
        last_submitted_at=(last or {}).get("submitted_at"),
    )


@router.post("/submit", response_model=EodSubmitResponse)
async def submit(
    payload: EodSubmitRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> EodSubmitResponse:
    operator_id: ObjectId = user["_id"]
    today_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
    now = utcnow()

    per_cofounder: dict[str, dict[str, Any]] = {}
    for entry in payload.per_cofounder:
        if not ObjectId.is_valid(entry.cofounder_id):
            raise HTTPException(400, f"Invalid cofounder_id {entry.cofounder_id}")
        per_cofounder[entry.cofounder_id] = {
            "crs_sent": [str(u) for u in entry.crs_sent],
            "dms_sent": [str(u) for u in entry.dms_sent],
            "connections_accepted": [str(u) for u in entry.connections_accepted],
            "replies_received_manual": [
                {"linkedin_url": str(r.linkedin_url), "text": r.text}
                for r in entry.replies_received_manual
            ],
            "bookings_manual": [
                {"linkedin_url": str(b.linkedin_url), "meeting_at": b.meeting_at}
                for b in entry.bookings_manual
            ],
            "anomalies": entry.anomalies,
            "notes": entry.notes,
        }

    result = await db.eod_logs.find_one_and_update(
        {"operator_id": operator_id, "log_date": today_start},
        {
            "$set": {
                "operator_id": operator_id,
                "log_date": today_start,
                "per_cofounder": per_cofounder,
                "submitted_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=True,
    )
    log_id = result["_id"] if result else None

    task = nightly_run_task.delay(str(operator_id))
    await db.audit_records.insert_one(
        {
            "operator_id": operator_id,
            "event_type": "eod_submitted",
            "details": {
                "log_id": str(log_id) if log_id else None,
                "task_id": task.id,
            },
            "severity": "info",
            "created_at": now,
        }
    )
    return EodSubmitResponse(
        log_id=str(log_id) if log_id else "",
        nightly_task_id=task.id,
    )
