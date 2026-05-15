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
                if not oc or not oc.get("candidate_id"):
                    continue
                # Reuse the same snapshot helper the on-demand tracker calls.
                # Keeps both paths writing identical fields to our_comments
                # and comment_engagement_snapshots. Pass post_url so
                # APIdirect parent-post engagement gets refreshed in the
                # 2h beat too (not just on-demand).
                snap = _update_our_comment_snapshot(
                    db,
                    candidate_id=oc["candidate_id"],
                    comments=comments,
                    parent_post_url=post_url,
                )
                if snap.get("our_comment_status") == "detected" and snap.get("our_comment_id"):
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


def _fetch_parent_post_engagement(post_url: str) -> dict[str, Any]:
    """Call APIdirect's `/v1/linkedin/post` endpoint to read aggregate
    engagement on the ORIGINAL post (likes, comments-total, shares, +
    reactions breakdown by type — like/celebrate/support/love/etc).
    Unipile gives us per-comment counts only; APIdirect fills in the
    parent-post emoji reactions LinkedIn shows under the post itself.

    Returns a flat dict with keys parent_post_{likes,comments_total,
    shares,reactions,polled_at}. Returns zeros on any failure — never
    raises, so a flaky APIdirect call doesn't take down a Unipile poll.
    """
    from app.services import apidirect as _apidirect

    out: dict[str, Any] = {
        "parent_post_likes": 0,
        "parent_post_comments_total": 0,
        "parent_post_shares": 0,
        "parent_post_reactions": None,
        "parent_post_polled_at": None,
    }
    if not post_url:
        return out
    try:
        details = _apidirect.get_linkedin_post_details(post_url)
    except _apidirect.ApiDirectQuotaExhausted:
        log.warning("apidirect quota exhausted — skipping parent-post fetch")
        return out
    except _apidirect.ApiDirectError as err:
        log.warning("apidirect post details error: %s", err)
        return out
    except _apidirect.ApiDirectNotConfigured:
        return out
    except Exception as err:  # noqa: BLE001
        log.warning("apidirect post details unexpected error: %s", err)
        return out

    if details is None:
        return out
    out["parent_post_likes"] = int(details.likes or 0)
    out["parent_post_comments_total"] = int(details.comments or 0)
    out["parent_post_shares"] = int(details.shares or 0)
    out["parent_post_reactions"] = details.reactions_by_type or None
    out["parent_post_polled_at"] = utcnow()
    return out


def _update_our_comment_snapshot(
    db: Database,
    *,
    candidate_id: ObjectId,
    comments: list[UnipileComment],
    parent_post_url: str = "",
) -> dict[str, Any]:
    """Sync latest engagement counts onto the candidate's our_comments doc.

    Mirrors the for-loop body in `poll_engagement_and_replies_for_all_operators`
    lines 125–156 — same Mongo writes, same snapshot collection — but
    callable per-candidate from the on-demand tracker endpoints.

    Also fetches parent-post engagement (likes, comments-total, shares,
    reactions breakdown) via APIdirect when `parent_post_url` is set, and
    persists those fields on the same our_comments doc so the tracker UI
    can render LinkedIn-style emoji reactions on the original post.

    Returns the snapshot fields (`our_comment_id`, `latest_reaction_count`,
    `latest_reply_count`, `our_comment_status`, and the parent_post_*
    fields) so the route can echo them back to the frontend without a
    second query.
    """
    out: dict[str, Any] = {
        "our_comment_id": None,
        "latest_reaction_count": 0,
        "latest_reply_count": 0,
        "our_comment_status": "not_found",
        "parent_post_likes": 0,
        "parent_post_comments_total": 0,
        "parent_post_shares": 0,
        "parent_post_reactions": None,
        "parent_post_polled_at": None,
    }

    # 1. Parent-post engagement via APIdirect. Persisted on the candidate
    # doc so the tracker can read both shipped + unshipped candidates'
    # parent-post engagement from one place. Best-effort — wrapped in
    # try/except inside _fetch_parent_post_engagement, never raises.
    if parent_post_url:
        parent = _fetch_parent_post_engagement(parent_post_url)
        if parent["parent_post_polled_at"] is not None:
            db.candidates.update_one(
                {"_id": candidate_id},
                {
                    "$set": {
                        "parent_post_likes": parent["parent_post_likes"],
                        "parent_post_comments_total": parent["parent_post_comments_total"],
                        "parent_post_shares": parent["parent_post_shares"],
                        "parent_post_reactions": parent["parent_post_reactions"],
                        "parent_post_polled_at": parent["parent_post_polled_at"],
                        "updated_at": utcnow(),
                    }
                },
            )
            out.update(parent)

    # 2. Per-our-comment engagement via Unipile (the existing flow).
    oc = db.our_comments.find_one({"candidate_id": candidate_id})
    if not oc:
        return out
    cid = oc.get("comment_id")
    out["our_comment_id"] = cid
    out["our_comment_status"] = "sent" if cid else (oc.get("status") or "queued")
    if not cid:
        return out

    match = next((cm for cm in comments if cm.comment_id == cid), None)
    if not match:
        # Comment id known but Unipile didn't return it on this poll —
        # could be a transient missing-page result or a deleted comment.
        # Carry forward the last-known values without touching mongo.
        out["latest_reaction_count"] = int(oc.get("latest_reaction_count") or 0)
        out["latest_reply_count"] = int(oc.get("latest_reply_count") or 0)
        out["our_comment_status"] = "detected"
        return out

    now = utcnow()
    out["latest_reaction_count"] = int(match.reaction_count or 0)
    out["latest_reply_count"] = int(match.reply_count or 0)
    out["our_comment_status"] = "detected"
    db.our_comments.update_one(
        {"_id": oc["_id"]},
        {
            "$set": {
                "latest_reaction_count": out["latest_reaction_count"],
                "latest_reply_count": out["latest_reply_count"],
                "latest_polled_at": now,
                "updated_at": now,
            }
        },
    )
    db.comment_engagement_snapshots.insert_one(
        {
            "comment_id": cid,
            "polled_at": now,
            "reaction_count": out["latest_reaction_count"],
            "reactions_by_type": None,
            "reply_count": out["latest_reply_count"],
        }
    )
    return out


def poll_one_candidate(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """On-demand single-candidate poll for the tracker UI.

    Performs the same Unipile fetch + reply detection + our_comments
    snapshot as the every-2h beat, but for ONE candidate. Returns a
    flat dict the tracker route can hand to the frontend:

      shipped:                 bool
      polled_at:               datetime
      new_reply_ids:           list[str]
      our_comment_id:          str | None
      latest_reaction_count:   int
      latest_reply_count:      int
      our_comment_status:      "queued" | "sent" | "detected" | "not_found"
      error:                   str | None   # set only on Unipile failure

    Missing Unipile account or post_url yields `shipped=False`. The full-
    tenant beat (`poll_engagement_and_replies_for_all_operators`) still
    dedups Unipile calls across candidates that share a post; this helper
    is intentionally per-candidate since the on-demand flow is single-
    selection driven.
    """
    account_id = cofounder.get("unipile_account_id") or ""
    post_url = (candidate.get("post_url") or "").strip()
    if not account_id or not post_url:
        return {
            "shipped": False,
            "polled_at": None,
            "new_reply_ids": [],
            "our_comment_id": None,
            "latest_reaction_count": 0,
            "latest_reply_count": 0,
            "our_comment_status": "not_found",
            "error": "missing_unipile_or_url",
        }
    try:
        comments = get_post_comments(account_id=account_id, post_url=post_url)
    except UnipileNotConfigured as err:
        return {
            "shipped": True, "polled_at": utcnow(), "new_reply_ids": [],
            "our_comment_id": None, "latest_reaction_count": 0,
            "latest_reply_count": 0, "our_comment_status": "not_found",
            "error": f"unipile_not_configured: {err}",
        }
    except UnipileError as err:
        return {
            "shipped": True, "polled_at": utcnow(), "new_reply_ids": [],
            "our_comment_id": None, "latest_reaction_count": 0,
            "latest_reply_count": 0, "our_comment_status": "not_found",
            "error": f"unipile_error: {err}",
        }

    try:
        new_reply_ids = _process_candidate_replies(
            db,
            operator_id=operator_id,
            cofounder=cofounder,
            candidate=candidate,
            comments=comments,
        )
    except Exception as err:  # noqa: BLE001 — never let one bad candidate kill the route
        log.warning("poll_one_candidate: reply processing failed: %s", err)
        new_reply_ids = []

    snapshot = _update_our_comment_snapshot(
        db,
        candidate_id=candidate["_id"],
        comments=comments,
        parent_post_url=post_url,
    )
    return {
        "shipped": True,
        "polled_at": utcnow(),
        "new_reply_ids": [str(x) for x in new_reply_ids],
        **snapshot,
    }


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
