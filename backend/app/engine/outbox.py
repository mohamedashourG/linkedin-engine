"""
Outbound comment queue: ``our_comments`` rows drained by Celery ``process_outbox``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.database import Database

from app.config import settings
from app.models.common import utcnow
from app.services.unipile import UnipileError, UnipileNotConfigured, post_comment

log = logging.getLogger(__name__)


def ensure_outbox_indexes(db: Database) -> None:
    """Idempotent index creation (sync Celery / one-off)."""
    db.our_comments.create_index(
        [("comment_id", ASCENDING)], unique=True, sparse=True
    )
    db.our_comments.create_index(
        [
            ("candidate_id", ASCENDING),
            ("parent_comment_id", ASCENDING),
        ],
        unique=True,
    )
    db.our_comments.create_index([("operator_id", ASCENDING), ("posted_at", ASCENDING)])
    db.our_comments.create_index([("parent_post_url", ASCENDING), ("posted_at", ASCENDING)])
    db.our_comments.create_index([("status", ASCENDING), ("latest_polled_at", ASCENDING)])
    db.comment_engagement_snapshots.create_index(
        [("comment_id", ASCENDING), ("polled_at", ASCENDING)]
    )
    db.parent_post_snapshots.create_index(
        [("parent_post_url", ASCENDING), ("polled_at", ASCENDING)]
    )


def enqueue_comment(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    unipile_account_id: str,
    candidate_id: ObjectId,
    slate_run_id: ObjectId,
    parent_post_url: str,
    parent_post_id: str | None,
    parent_post_author_provider_id: str | None,
    text: str,
    context: str = "INITIAL_COMMENT",
    posted_via: str = "human_approved",
    parent_comment_id: str | None = None,
    parent_post_reaction_count_at_send: int = 0,
    parent_post_comment_count_at_send: int = 0,
    parent_post_repost_count_at_send: int = 0,
) -> ObjectId | None:
    """Insert ``status=queued`` when not already queued/sent for this target."""
    parent_storage = parent_comment_id or "__root__"
    existing = db.our_comments.find_one(
        {
            "candidate_id": candidate_id,
            "parent_comment_id": parent_storage,
            "status": {"$in": ["queued", "sent"]},
        }
    )
    if existing:
        return existing["_id"]

    now = utcnow()
    doc: dict[str, Any] = {
        "comment_id": None,
        "parent_comment_id": parent_storage,
        "parent_post_url": parent_post_url,
        "parent_post_id": parent_post_id or "",
        "parent_post_author_provider_id": parent_post_author_provider_id or "",
        "operator_id": operator_id,
        "cofounder_id": cofounder_id,
        "unipile_account_id": unipile_account_id,
        "candidate_id": candidate_id,
        "slate_run_id": slate_run_id,
        "text": text,
        "context": context,
        "posted_at": None,
        "posted_via": posted_via,
        "send_attempts": [],
        "status": "queued",
        "failure_reason": None,
        "parent_post_reaction_count_at_send": int(parent_post_reaction_count_at_send),
        "parent_post_comment_count_at_send": int(parent_post_comment_count_at_send),
        "parent_post_repost_count_at_send": int(parent_post_repost_count_at_send),
        "latest_reaction_count": 0,
        "latest_reactions_by_type": None,
        "latest_reply_count": 0,
        "latest_polled_at": None,
        "created_at": now,
        "updated_at": now,
    }
    return db.our_comments.insert_one(doc).inserted_id


def _quota_key(cofounder_id: ObjectId) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    return {"cofounder_id": cofounder_id, "date": today}


def _under_initial_quota(db: Database, cofounder_id: ObjectId) -> bool:
    cap = max(0, int(settings.cofounder_daily_initial_comment_quota))
    if cap <= 0:
        return False
    doc = db.cofounder_send_quotas.find_one({"_id": _quota_key(cofounder_id)})
    used = int((doc or {}).get("initial_comments_sent") or 0)
    return used < cap


def _inc_quota_sent(db: Database, cofounder_id: ObjectId) -> None:
    now = utcnow()
    db.cofounder_send_quotas.update_one(
        {"_id": _quota_key(cofounder_id)},
        {
            "$inc": {"initial_comments_sent": 1},
            "$set": {"last_updated": now},
            "$setOnInsert": {
                "replies_sent": 0,
                "dms_sent": 0,
                "created_at": now,
            },
        },
        upsert=True,
    )


def process_outbox(db: Database, *, batch_size: int = 20) -> dict[str, int]:
    """Drain queued comments: quota check → Unipile post_comment → status update."""
    out: dict[str, int] = {
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "quota_blocked": 0,
        "skipped_paused": 0,
    }
    ensure_outbox_indexes(db)
    cursor = db.our_comments.find({"status": "queued"}).sort("created_at", ASCENDING).limit(
        batch_size
    )
    for row in cursor:
        out["processed"] += 1
        oid = row.get("operator_id")
        if oid and db.users.find_one({"_id": oid, "paused": True}):
            out["skipped_paused"] += 1
            continue
        cf_id = row["cofounder_id"]
        if not _under_initial_quota(db, cf_id):
            out["quota_blocked"] += 1
            continue
        account_id = row.get("unipile_account_id") or ""
        post_url = row.get("parent_post_url") or ""
        text = (row.get("text") or "").strip()
        if not account_id or not post_url or not text:
            db.our_comments.update_one(
                {"_id": row["_id"]},
                {
                    "$set": {
                        "status": "failed",
                        "failure_reason": "missing_account_post_or_text",
                        "updated_at": utcnow(),
                    }
                },
            )
            out["failed"] += 1
            continue
        p_raw = row.get("parent_comment_id")
        parent_for_api = (
            None
            if not p_raw or p_raw == "__root__"
            else str(p_raw)
        )
        try:
            result = post_comment(
                account_id=account_id,
                post_url=post_url,
                text=text,
                parent_comment_id=parent_for_api,
            )
        except (UnipileError, UnipileNotConfigured) as err:
            attempts = list(row.get("send_attempts") or [])
            attempts.append(
                {"attempted_at": utcnow(), "success": False, "error_msg": str(err)[:500]}
            )
            terminal = len(attempts) >= 3
            db.our_comments.update_one(
                {"_id": row["_id"]},
                {
                    "$set": {
                        "send_attempts": attempts,
                        "status": "failed" if terminal else "queued",
                        "failure_reason": str(err)[:500] if terminal else None,
                        "updated_at": utcnow(),
                    }
                },
            )
            if terminal:
                out["failed"] += 1
            continue
        now = utcnow()
        db.our_comments.update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "comment_id": result.comment_id,
                    "posted_at": result.posted_at or now,
                    "status": "sent",
                    "failure_reason": None,
                    "updated_at": now,
                    "send_attempts": list(row.get("send_attempts") or [])
                    + [{"attempted_at": now, "success": True, "error_msg": None}],
                }
            },
        )
        _inc_quota_sent(db, cf_id)
        out["sent"] += 1
    return out
