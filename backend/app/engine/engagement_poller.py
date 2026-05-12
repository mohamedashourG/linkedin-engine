"""
Merged Unipile poll: shipped-candidate reply detection + our_comments engagement.

Single ``get_post_comments`` call per (account_id, post_url) services both flows.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.engine import auto_cr, reply_drafter
from app.engine.leads import upsert_lead
from app.engine.outbox import ensure_outbox_indexes
from app.models.common import utcnow
from app.services.openai_client import OpenAINotConfigured
from app.services.unipile import UnipileComment, UnipileError, UnipileNotConfigured, get_post_comments

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 14
_HOT_POLL_HOURS = 48


def poll_engagement_and_replies_for_all_operators(db: Database) -> dict[str, Any]:
    """Celery entry: reply monitor + lightweight our_comments stats refresh."""
    ensure_outbox_indexes(db)
    cutoff = utcnow() - timedelta(days=_LOOKBACK_DAYS)
    hot_cutoff = utcnow() - timedelta(hours=_HOT_POLL_HOURS)

    operators = list(
        db.users.find({"paused": {"$ne": True}}, {"_id": 1, "email": 1, "name": 1})
    )
    summary: dict[str, Any] = {
        "operators": 0,
        "new_replies": 0,
        "auto_crs": 0,
        "our_comments_polled": 0,
    }
    new_replies_by_operator: dict[ObjectId, list[ObjectId]] = {}

    for op in operators:
        operator_id = op["_id"]
        cofounders = {
            cf["_id"]: cf
            for cf in db.cofounders.find({"operator_id": operator_id, "active": True})
        }
        if not cofounders:
            continue

        shipped = list(
            db.candidates.find(
                {
                    "operator_id": operator_id,
                    "status": "shipped",
                    "shipped_at": {"$gte": cutoff},
                }
            )
        )
        our_sent = list(
            db.our_comments.find(
                {
                    "operator_id": operator_id,
                    "status": "sent",
                    "comment_id": {"$nin": [None, ""]},
                    "$or": [
                        {"latest_polled_at": None},
                        {"latest_polled_at": {"$lt": utcnow() - timedelta(minutes=25)}},
                        {"posted_at": {"$gte": hot_cutoff}},
                    ],
                }
            ).limit(200)
        )

        by_post: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"candidates": [], "our_ids": []}
        )
        for c in shipped:
            cf = cofounders.get(c.get("cofounder_id"))
            if not cf:
                continue
            aid = cf.get("unipile_account_id") or ""
            pu = (c.get("post_url") or "").strip()
            if aid and pu:
                by_post[(aid, pu)]["candidates"].append((cf, c))
        for oc in our_sent:
            aid = oc.get("unipile_account_id") or ""
            pu = (oc.get("parent_post_url") or "").strip()
            if aid and pu:
                entry = by_post[(aid, pu)]
                entry["our_ids"].append(oc["_id"])

        for (account_id, post_url), bundle in by_post.items():
            if not bundle["candidates"] and not bundle["our_ids"]:
                continue
            try:
                comments = get_post_comments(account_id=account_id, post_url=post_url)
            except UnipileNotConfigured as err:
                log.warning("engagement_poller: unipile unconfigured: %s", err)
                continue
            except UnipileError as err:
                log.warning("engagement_poller: unipile error post=%s: %s", post_url[:80], err)
                continue

            for cofounder, cand in bundle["candidates"]:
                try:
                    new_ids = _process_candidate_replies(
                        db,
                        operator_id=operator_id,
                        cofounder=cofounder,
                        candidate=cand,
                        comments=comments,
                    )
                except Exception as err:
                    log.warning("engagement_poller: reply processing failed: %s", err)
                    new_ids = []
                if new_ids:
                    new_replies_by_operator.setdefault(operator_id, []).extend(new_ids)
                    summary["new_replies"] += len(new_ids)

            for oc_id in bundle["our_ids"]:
                oc = db.our_comments.find_one({"_id": oc_id})
                if not oc:
                    continue
                cid = oc.get("comment_id")
                if not cid:
                    continue
                match = next((cm for cm in comments if cm.comment_id == cid), None)
                if not match:
                    continue
                now = utcnow()
                db.our_comments.update_one(
                    {"_id": oc_id},
                    {
                        "$set": {
                            "latest_reaction_count": int(match.reaction_count or 0),
                            "latest_reply_count": int(match.reply_count or 0),
                            "latest_polled_at": now,
                            "updated_at": now,
                        }
                    },
                )
                db.comment_engagement_snapshots.insert_one(
                    {
                        "comment_id": cid,
                        "polled_at": now,
                        "reaction_count": int(match.reaction_count or 0),
                        "reactions_by_type": None,
                        "reply_count": int(match.reply_count or 0),
                    }
                )
                summary["our_comments_polled"] += 1

        summary["operators"] += 1

    for operator_id, reply_ids in new_replies_by_operator.items():
        try:
            from app.engine.email_digest import send_reply_digest

            send_reply_digest(db, operator_id=operator_id, reply_ids=reply_ids)
        except Exception as err:
            log.warning("engagement_poller: digest send failed for %s: %s", operator_id, err)

    log.info("engagement_poller: %s", summary)
    return summary


def _process_candidate_replies(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder: dict[str, Any],
    candidate: dict[str, Any],
    comments: list[UnipileComment],
) -> list[ObjectId]:
    """Ported from reply_monitor._process_candidate but reuses pre-fetched comments."""
    account_id = cofounder.get("unipile_account_id")
    if not account_id:
        return []

    last_check = candidate.get("last_reply_check_at") or candidate.get("shipped_at")
    if last_check and last_check.tzinfo is None:
        from datetime import timezone as tz

        last_check = last_check.replace(tzinfo=tz.utc)

    cofounder_url = (cofounder.get("linkedin_url") or "").rstrip("/")
    new_reply_ids: list[ObjectId] = []
    voice = cofounder.get("voice_profile") or {}
    voice_tone = voice.get("tone_description") or ""
    voice_examples = voice.get("examples") or []
    cofounder_first = (cofounder.get("display_name") or "").strip().split(" ", 1)[0] or ""

    for c in comments:
        author_url = (c.author_profile_url or "").rstrip("/")
        if author_url and cofounder_url and author_url == cofounder_url:
            continue
        published = c.published_at
        if published and last_check and published <= last_check:
            continue
        if not c.text or not c.text.strip():
            continue

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
            advance_stage_to="S3",
        )

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
            "parent_our_comment_id": None,
            "received_at": now,
            "processed": False,
            "our_reply_id": None,
            "created_at": now,
            "updated_at": now,
        }
        result = db.replies.insert_one(reply_doc)
        new_reply_ids.append(result.inserted_id)

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

        db.candidates.update_one(
            {"_id": candidate["_id"]},
            {"$inc": {"reply_count": 1}, "$set": {"updated_at": now}},
        )

    db.candidates.update_one(
        {"_id": candidate["_id"]},
        {"$set": {"last_reply_check_at": utcnow()}},
    )
    return new_reply_ids
