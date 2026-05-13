"""
Render and send the morning slate email.

Layout:
  1. Pipeline state (CONVERTED today + Stage counts) — Phase 5 will fill this
     in. For now: a brief header.
  2. Today's slate, grouped by cofounder, with copy-paste-ready boxes.
  3. EOD form link.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.models.common import utcnow
from app.services.email import send_email

log = logging.getLogger(__name__)


def render_slate_email(
    db: Database,
    *,
    operator: dict[str, Any],
    cofounders: list[dict[str, Any]],
    slate_run_id: ObjectId,
) -> tuple[str, str]:
    """Returns (subject, html). Pure render, no side effects."""
    slated = list(
        db.candidates.find({"slate_run_id": slate_run_id, "status": "slated"})
        .sort([("cofounder_id", 1), ("comment_type", 1)])
    )
    cofounder_by_id = {cf["_id"]: cf for cf in cofounders}
    by_cf: dict[Any, list[dict[str, Any]]] = {}
    for c in slated:
        by_cf.setdefault(c["cofounder_id"], []).append(c)

    company = (operator.get("company_name") or "").strip()
    if company:
        subject = f"Today's comments for {company} · {len(slated)} comments"
        header_text = f"Today's comments for {escape(company)} · {len(slated)}"
    else:
        subject = f"Your daily LinkedIn slate · {len(slated)} comments"
        header_text = f"Today's slate · {len(slated)} comments"
    html_parts: list[str] = [
        "<html><body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 720px; margin: 0 auto; color: #0f172a;\">",
        f"<h1 style=\"font-size: 22px;\">{header_text}</h1>",
        f"<p style=\"color:#64748b\">Run for {escape(operator.get('name', ''))}.</p>",
    ]

    for cf_id, candidates in by_cf.items():
        cofounder = cofounder_by_id.get(cf_id, {})
        cf_name = escape(cofounder.get("display_name") or "Unknown")
        html_parts.append(
            f"<h2 style=\"margin-top:32px;border-top:1px solid #e2e8f0;padding-top:16px;font-size:18px\">"
            f"{cf_name} · {len(candidates)} comments</h2>"
        )
        for c in candidates:
            html_parts.append(_render_candidate(c))

    app_url = settings.app_url.rstrip("/")
    html_parts.append(
        f"<hr style='margin:32px 0;border:0;border-top:1px solid #e2e8f0' />"
        f"<p style='color:#64748b;font-size:13px'>"
        f"<a href=\"{app_url}/eod\" style='color:#0f172a'>Submit EOD form</a> · "
        f"<a href=\"{app_url}/\" style='color:#0f172a'>Open dashboard</a>"
        f"</p>"
    )
    html_parts.append("</body></html>")
    return subject, "\n".join(html_parts)


def _source_badge(c: dict[str, Any]) -> str:
    """Render a small pill that tells the operator whether this comment
    came from one of their curated contacts vs from the engine's keyword/
    discovery search. Contact-sourced posts skip the LLM gate funnel —
    we want that fact visible at a glance.

    A leading non-breaking space (&nbsp;) ensures the author name and the
    badge are visually separated even if the email client strips CSS
    (Gmail and Outlook are inconsistent about preserving margin-left on
    inline-block spans). Without it, the text reads "Schneiderfrom
    keyword search" — one squished blob.
    """
    source = (c.get("source") or "").strip()
    is_contact = source in ("contact_unipile", "contact_seed")
    label = "from your contacts" if is_contact else "from keyword search"
    bg = "#dcfce7" if is_contact else "#e0e7ff"
    fg = "#166534" if is_contact else "#3730a3"
    return (
        "&nbsp;"
        f"<span style=\"display:inline-block;background:{bg};color:{fg};"
        f"padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;"
        f"vertical-align:middle\">"
        f"{label}</span>"
    )


def _icp_label(c: dict[str, Any]) -> str:
    """Mirror the today-page logic: prefer the audit's normalized 0-10
    score, fall back to legacy `total` for pre-RULE-14 slates. Show
    "spared" when the inline rubric pre-qualified the candidate and the
    LLM ICP gate was skipped (no numeric score exists for those)."""
    icp = (c.get("gate_results") or {}).get("icp") or {}
    s = icp.get("score_0_10")
    if s is None:
        s = icp.get("total")
    if s is None:
        return "spared" if icp.get("spared_inline_icp") else "—"
    return f"{s}/10"


def _render_candidate(c: dict[str, Any]) -> str:
    score_label = _icp_label(c)
    ctype = c.get("comment_type") or "?"
    author = escape(c.get("author_name") or "Unknown author")
    post_text = escape((c.get("post_text") or "")[:400])
    if len(c.get("post_text") or "") > 400:
        post_text += "…"
    comment = escape(c.get("comment_text") or "")
    post_url = escape(c.get("post_url") or "#")
    source_badge = _source_badge(c)

    return (
        "<div style=\"border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:12px 0\">"
        f"<div style=\"font-size:12px;color:#64748b;margin-bottom:8px\">"
        f"<strong>Type {ctype}</strong> · ICP {score_label} · {author}{source_badge}"
        "</div>"
        f"<div style=\"background:#f8fafc;padding:12px;border-radius:6px;font-size:13px;color:#475569\">"
        f"<div style=\"font-weight:600;margin-bottom:4px;color:#0f172a\">Original post</div>"
        f"{post_text}"
        "</div>"
        f"<div style=\"background:#0f172a;color:#f8fafc;padding:12px;border-radius:6px;margin-top:8px;white-space:pre-wrap;font-size:14px;line-height:1.5\">"
        f"{comment}"
        "</div>"
        f"<div style=\"margin-top:8px;font-size:12px\"><a href=\"{post_url}\" style='color:#0f172a'>Open post on LinkedIn →</a></div>"
        "</div>"
    )


def send_slate_email(
    db: Database,
    *,
    operator: dict[str, Any],
    cofounders: list[dict[str, Any]],
    slate_run_id: ObjectId,
) -> str:
    subject, html = render_slate_email(
        db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
    )
    # Operator's email is always included; additional recipients come from
    # user.slate_recipients (settings page).
    extras = operator.get("slate_recipients") or []
    recipients: list[str] = [operator["email"], *(extras or [])]
    msg_id = send_email(to=recipients, subject=subject, html=html)
    db.slate_runs.update_one(
        {"_id": slate_run_id},
        {
            "$set": {
                "email_sent": True,
                "email_message_id": msg_id,
                "updated_at": utcnow(),
            }
        },
    )
    return msg_id
