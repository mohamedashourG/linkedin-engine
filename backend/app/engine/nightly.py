"""
Nightly batch (Celery beat at 23:00 operator-local in prod, also runnable
on-demand right after the operator submits EOD).

Steps:
  1. Process today's EOD log → advance lead stages.
  2. Update exhaustion ledger from today's shipped candidates.
  3. Run embedded harvester (mine dropped posts for ICP names).
  4. STALL detection: ACTIVE leads with no touch in 30 days → STALLED.
"""
from __future__ import annotations

import logging
from datetime import date as date_type, datetime, time, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.engine.constants import EXHAUSTION_LOOKBACK_DAYS
from app.engine.harvester import harvest_from_dropped
from app.models.common import utcnow

log = logging.getLogger(__name__)

_STALL_DAYS = 30


def run_nightly_for_operator(db: Database, operator_id: ObjectId) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    summary["eod_advancements"] = _advance_from_eod(db, operator_id)
    summary["exhaustion_updates"] = _update_exhaustion_ledger(db, operator_id)
    summary["harvester"] = harvest_from_dropped(db, operator_id)
    summary["stalled"] = _detect_stalled(db, operator_id)
    log.info("nightly: operator=%s %s", operator_id, summary)
    return summary


def _advance_from_eod(db: Database, operator_id: ObjectId) -> dict[str, int]:
    today_start = datetime.combine(
        date_type.today(), time.min, tzinfo=timezone.utc
    )
    eod = db.eod_logs.find_one(
        {
            "operator_id": operator_id,
            "log_date": today_start,
        }
    )
    if not eod:
        return {"crs": 0, "accepted": 0, "dms": 0}

    counts = {"crs": 0, "accepted": 0, "dms": 0}
    per = eod.get("per_cofounder") or {}
    for _cf_id, data in per.items():
        for url in data.get("crs_sent") or []:
            if _set_lead_stage(
                db,
                operator_id,
                url,
                target_stage="S4",
                set_field="cr_sent_at",
            ):
                counts["crs"] += 1
        for url in data.get("connections_accepted") or []:
            if _set_lead_stage(
                db,
                operator_id,
                url,
                target_stage="S5",
                set_field="cr_accepted_at",
            ):
                counts["accepted"] += 1
        for url in data.get("dms_sent") or []:
            if _set_lead_stage(
                db,
                operator_id,
                url,
                target_stage="S6",
                set_field="dm_sent_at",
            ):
                counts["dms"] += 1
    return counts


def _set_lead_stage(
    db: Database,
    operator_id: ObjectId,
    linkedin_url: str,
    *,
    target_stage: str,
    set_field: str,
) -> bool:
    if not linkedin_url:
        return False
    now = utcnow()
    result = db.leads.update_one(
        {"operator_id": operator_id, "linkedin_url": linkedin_url},
        {
            "$set": {
                "current_stage": target_stage,
                set_field: now,
                "last_touched_at": now,
                "updated_at": now,
            }
        },
    )
    return result.matched_count > 0


def _update_exhaustion_ledger(db: Database, operator_id: ObjectId) -> int:
    """Upsert ``exhaustion_ledger`` from **shipped** candidates today (EOD path).

    Discovery-time touches use ``last_seen_discovery_at`` via
    ``discovery._discovery_record_insert`` — see that helper for cross-run
    dedupe when runs abort before ship.
    """
    today_start = datetime.combine(
        date_type.today(), time.min, tzinfo=timezone.utc
    )
    cursor = db.candidates.find(
        {
            "operator_id": operator_id,
            "status": "shipped",
            "shipped_at": {"$gte": today_start},
        }
    )
    now = utcnow()
    expires = now + timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    n = 0
    for c in cursor:
        url = c.get("author_linkedin_url")
        if not url:
            continue
        db.exhaustion_ledger.update_one(
            {"operator_id": operator_id, "linkedin_url": url},
            {
                "$set": {"last_engaged_at": now, "expires_at": expires, "updated_at": now},
                "$inc": {"engagement_count": 1},
                "$setOnInsert": {
                    "operator_id": operator_id,
                    "linkedin_url": url,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        n += 1
    return n


def _detect_stalled(db: Database, operator_id: ObjectId) -> int:
    cutoff = utcnow() - timedelta(days=_STALL_DAYS)
    result = db.leads.update_many(
        {
            "operator_id": operator_id,
            "status": "ACTIVE",
            "last_touched_at": {"$lt": cutoff},
        },
        {"$set": {"status": "STALLED", "updated_at": utcnow()}},
    )
    return result.modified_count
