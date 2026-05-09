"""
Reply monitor. Every 2h:
  1. For each shipped candidate (last 14d), call Unipile to fetch comments on
     its post URL.
  2. For each new comment (after `last_reply_check_at`, not by the cofounder),
     create a `replies` doc, draft a suggested follow-up via gpt-5.4, upsert
     the lead, and check the auto-CR trigger.
  3. If any new replies landed across operators, queue a digest email.

Mock mode: Unipile returns canned comments per shipped post URL so the loop
can be exercised end-to-end without a connected LinkedIn account.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.engine import auto_cr, reply_drafter
from app.engine.leads import upsert_lead
from app.models.common import utcnow
from app.services.openai_client import OpenAINotConfigured
from app.services.unipile import (
    UnipileComment,
    UnipileError,
    UnipileNotConfigured,
    get_post_comments,
)

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 14


def poll_replies_for_all_operators(db: Database) -> dict[str, Any]:
    """Top-level entrypoint called by Celery beat every 2h."""
    cutoff = utcnow() - timedelta(days=_LOOKBACK_DAYS)
    operators = list(
        db.users.find({"paused": {"$ne": True}}, {"_id": 1, "email": 1, "name": 1})
    )
    summary: dict[str, Any] = {"operators": 0, "new_replies": 0, "auto_crs": 0}
    new_replies_by_operator: dict[ObjectId, list[ObjectId]] = {}

    for op in operators:
        operator_id = op["_id"]
        cofounders = {
            cf["_id"]: cf
            for cf in db.cofounders.find({"operator_id": operator_id, "active": True})
        }
        if not cofounders:
            continue

        candidates = list(
            db.candidates.find(
                {
                    "operator_id": operator_id,
                    "status": "shipped",
                    "shipped_at": {"$gte": cutoff},
                }
            )
        )
        for c in candidates:
            cofounder = cofounders.get(c["cofounder_id"])
            if not cofounder:
                continue
            try:
                new_reply_ids = _process_candidate(
                    db,
                    operator_id=operator_id,
                    cofounder=cofounder,
                    candidate=c,
                )
            except UnipileNotConfigured as err:
                log.warning("reply_monitor: unipile unconfigured, skipping: %s", err)
                continue
            except UnipileError as err:
                log.warning("reply_monitor: unipile error on %s: %s", c["_id"], err)
                continue
            if new_reply_ids:
                new_replies_by_operator.setdefault(operator_id, []).extend(new_reply_ids)
                summary["new_replies"] += len(new_reply_ids)

        summary["operators"] += 1

    # email digests
    for operator_id, reply_ids in new_replies_by_operator.items():
        try:
            from app.engine.email_digest import send_reply_digest

            send_reply_digest(db, operator_id=operator_id, reply_ids=reply_ids)
        except Exception as err:
            log.warning("reply_monitor: digest send failed for %s: %s", operator_id, err)

    log.info("reply_monitor: %s", summary)
    return summary


def _process_candidate(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder: dict[str, Any],
    candidate: dict[str, Any],
) -> list[ObjectId]:
    """Returns the list of newly created reply _ids for this candidate."""
    account_id = cofounder.get("unipile_account_id")
    if not account_id:
        return []

    last_check = candidate.get("last_reply_check_at") or candidate.get("shipped_at")
    if last_check and last_check.tzinfo is None:
        last_check = last_check.replace(tzinfo=timezone.utc)

    comments: list[UnipileComment] = get_post_comments(
        account_id=account_id, post_url=candidate.get("post_url", "")
    )

    cofounder_url = (cofounder.get("linkedin_url") or "").rstrip("/")
    new_reply_ids: list[ObjectId] = []
    voice = cofounder.get("voice_profile") or {}
    voice_tone = voice.get("tone_description") or ""
    voice_examples = voice.get("examples") or []
    cofounder_first = (cofounder.get("display_name") or "").strip().split(" ", 1)[0] or ""

    for c in comments:
        # Skip our own comment.
        author_url = (c.author_profile_url or "").rstrip("/")
        if author_url and cofounder_url and author_url == cofounder_url:
            continue
        published = c.published_at
        if published and last_check and published <= last_check:
            continue
        if not c.text or not c.text.strip():
            continue

        # Avoid double-recording the same comment.
        existing = db.replies.find_one(
            {"candidate_id": candidate["_id"], "comment_id": c.comment_id}
        )
        if existing:
            continue

        author_lead_url = c.author_profile_url
        is_post_owner = bool(
            author_lead_url
            and candidate.get("author_linkedin_url")
            and author_lead_url.rstrip("/") == candidate["author_linkedin_url"].rstrip("/")
        )
        lead_id = upsert_lead(
            db,
            operator_id=operator_id,
            linkedin_url=author_lead_url,
            name=c.author_name,
            candidate_id=candidate["_id"],
            advance_stage_to="S3",  # received reply
        )

        # Draft a public reply-back. (DM contexts fire from the auto-CR path,
        # not here.)
        suggested_text = ""
        suggested_type = "A"
        if voice_tone or voice_examples:
            try:
                suggested_text, suggested_type = reply_drafter.draft_reply(
                    reply_context="PUBLIC_REPLY_BACK",
                    cofounder_name=cofounder.get("display_name") or "",
                    cofounder_tone=voice_tone,
                    cofounder_first_name=cofounder_first,
                    cofounder_calendly_url=cofounder.get("calendly_url") or "",
                    author_name=candidate.get("author_name"),
                    author_title=candidate.get("author_title"),
                    author_company=candidate.get("author_company"),
                    original_post=candidate.get("post_text") or "",
                    our_comment=candidate.get("comment_text") or "",
                    their_reply=c.text,
                    they_are_post_author=is_post_owner,
                    prospect_name=c.author_name,
                )
            except OpenAINotConfigured:
                pass
            except Exception as err:
                log.warning("reply drafter failed: %s", err)

        now = utcnow()
        reply_doc = {
            "operator_id": operator_id,
            "candidate_id": candidate["_id"],
            "lead_id": lead_id,
            "cofounder_id": cofounder["_id"],
            "comment_id": c.comment_id,
            "reply_text": c.text,
            "reply_author_name": c.author_name,
            "reply_author_linkedin_url": c.author_profile_url,
            "reply_author_provider_id": c.author_provider_id,
            "reply_author_is_post_owner": is_post_owner,
            "reply_published_at": c.published_at,
            "detected_at": now,
            "suggested_reply": suggested_text,
            "suggested_reply_type": suggested_type,
            "user_action": "pending",
            "acted_at": None,
            "created_at": now,
            "updated_at": now,
        }
        result = db.replies.insert_one(reply_doc)
        new_reply_ids.append(result.inserted_id)

        # Bump lead.reply_count and check auto-CR.
        if lead_id:
            lead = db.leads.find_one_and_update(
                {"_id": lead_id},
                {
                    "$inc": {"reply_count": 1},
                    "$addToSet": {"reply_ids": result.inserted_id},
                    "$set": {"last_touched_at": now, "updated_at": now},
                },
                return_document=True,
            )
            if lead:
                auto_cr.maybe_trigger(
                    db, operator_id=operator_id, cofounder=cofounder, lead=lead
                )

        # Bump the candidate's reply_count for quick filters in the UI.
        db.candidates.update_one(
            {"_id": candidate["_id"]},
            {"$inc": {"reply_count": 1}, "$set": {"updated_at": now}},
        )

    db.candidates.update_one(
        {"_id": candidate["_id"]},
        {"$set": {"last_reply_check_at": utcnow()}},
    )
    return new_reply_ids
