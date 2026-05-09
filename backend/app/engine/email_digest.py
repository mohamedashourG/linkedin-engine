"""
Reply-digest email — fires from the reply monitor whenever new replies land
across one or more shipped comments for an operator.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.services.email import EmailNotConfigured, send_email

log = logging.getLogger(__name__)


def send_reply_digest(
    db: Database,
    *,
    operator_id: ObjectId,
    reply_ids: list[ObjectId],
) -> str | None:
    if not reply_ids:
        return None
    operator = db.users.find_one({"_id": operator_id})
    if not operator:
        return None

    replies = list(db.replies.find({"_id": {"$in": reply_ids}}))
    if not replies:
        return None

    candidates = {
        c["_id"]: c
        for c in db.candidates.find({"_id": {"$in": list({r["candidate_id"] for r in replies})}})
    }
    cofounders = {
        cf["_id"]: cf
        for cf in db.cofounders.find({"_id": {"$in": list({r["cofounder_id"] for r in replies})}})
    }

    subject = (
        f"{len(replies)} new reply{'s' if len(replies) != 1 else ''} on your LinkedIn slate"
    )
    app_url = settings.app_url.rstrip("/")
    rows = "\n".join(
        _render_row(r, candidates.get(r["candidate_id"]), cofounders.get(r["cofounder_id"]))
        for r in replies
    )
    html = (
        "<html><body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width:720px; margin:0 auto; color:#0f172a\">"
        f"<h1 style='font-size:22px'>{escape(subject)}</h1>"
        f"<p style='color:#64748b'>{len(replies)} new reply{'s' if len(replies) != 1 else ''} since the last check.</p>"
        f"{rows}"
        f"<p style='margin-top:32px;color:#64748b;font-size:13px'>"
        f"<a href='{app_url}/replies' style='color:#0f172a'>Open Replies dashboard →</a>"
        "</p>"
        "</body></html>"
    )

    try:
        msg_id = send_email(to=operator["email"], subject=subject, html=html)
    except EmailNotConfigured as err:
        log.warning("digest email skipped: %s", err)
        return None
    log.info("reply digest sent to=%s id=%s replies=%d", operator["email"], msg_id, len(replies))
    return msg_id


def _render_row(
    reply: dict[str, Any],
    candidate: dict[str, Any] | None,
    cofounder: dict[str, Any] | None,
) -> str:
    cf_name = escape((cofounder or {}).get("display_name", ""))
    author = escape(reply.get("reply_author_name") or "Unknown")
    their_text = escape((reply.get("reply_text") or "")[:600])
    suggested = escape((reply.get("suggested_reply") or "")[:1000])
    post_text = escape(((candidate or {}).get("post_text") or "")[:200])
    return (
        "<div style='border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:12px 0'>"
        f"<div style='font-size:12px;color:#64748b;margin-bottom:8px'>"
        f"<strong>{cf_name}</strong>'s thread · reply from {author}"
        "</div>"
        f"<div style='font-size:12px;color:#94a3b8;margin-bottom:8px'>"
        f"On post: {post_text}…"
        "</div>"
        f"<div style='background:#f8fafc;border-radius:6px;padding:12px;font-size:13px;color:#475569;margin-bottom:8px'>"
        f"<div style='font-weight:600;margin-bottom:4px;color:#0f172a'>Their reply</div>{their_text}"
        "</div>"
        f"<div style='background:#0f172a;color:#f8fafc;border-radius:6px;padding:12px;font-size:14px;white-space:pre-wrap;line-height:1.5'>"
        f"<div style='font-size:12px;color:#94a3b8;margin-bottom:6px'>Suggested follow-up</div>{suggested}"
        "</div>"
        "</div>"
    )
