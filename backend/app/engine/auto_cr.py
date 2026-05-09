"""
Auto-CR (connection request) trigger. Per spec PERMANENT RULE 5:
when a lead's reply_count reaches 2 and they're still in S2/S3 with no CR yet,
queue a connection request via Unipile and advance the lead to S4.

The connect-message template comes from the cofounder; if not set, we use a
short, neutral default.
"""
from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.models.common import utcnow
from app.services.unipile import (
    UnipileError,
    UnipileNotConfigured,
    resolve_profile,
    send_invite,
)

log = logging.getLogger(__name__)

_DEFAULT_MESSAGE = (
    "Enjoyed your perspective on the thread. Would love to stay connected if "
    "you're open to it."
)


def maybe_trigger(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder: dict[str, Any],
    lead: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Run the PERMANENT-RULE-5 check. Returns a result dict on action, None when
    no action was taken.
    """
    if (lead.get("reply_count") or 0) < 2:
        return None
    if lead.get("current_stage") not in ("S2", "S3"):
        return None
    if lead.get("cr_sent_at"):
        return None

    account_id = cofounder.get("unipile_account_id")
    if not account_id:
        log.warning(
            "auto-CR skipped: cofounder %s has no unipile_account_id",
            cofounder.get("_id"),
        )
        return {"status": "skipped_no_unipile_account"}

    linkedin_url: str | None = lead.get("linkedin_url")
    if not linkedin_url:
        return {"status": "skipped_no_linkedin_url"}

    message = (
        cofounder.get("connect_message_template") or _DEFAULT_MESSAGE
    ).strip()[:300]

    try:
        profile = resolve_profile(
            account_id=account_id, public_identifier_or_url=linkedin_url
        )
        provider_id = profile.get("provider_id") or profile.get("member_urn")
        if not provider_id:
            return {"status": "no_provider_id"}
        invitation_id = send_invite(
            account_id=account_id, provider_id=provider_id, message=message
        )
    except UnipileNotConfigured as err:
        log.warning("auto-CR skipped: unipile not configured: %s", err)
        return {"status": "unipile_not_configured"}
    except UnipileError as err:
        log.error("auto-CR failed for lead %s: %s", lead.get("_id"), err)
        return {"status": "unipile_error", "error": str(err)}

    now = utcnow()
    db.leads.update_one(
        {"_id": lead["_id"]},
        {
            "$set": {
                "current_stage": "S4",
                "cr_sent_at": now,
                "cr_invitation_id": invitation_id,
                "last_touched_at": now,
                "updated_at": now,
            }
        },
    )
    db.audit_records.insert_one(
        {
            "operator_id": operator_id,
            "event_type": "stage_complete",
            "stage": "auto_cr",
            "details": {
                "lead_id": str(lead["_id"]),
                "invitation_id": invitation_id,
                "linkedin_url": linkedin_url,
            },
            "severity": "info",
            "created_at": now,
        }
    )
    log.info(
        "auto-CR sent: lead=%s invitation_id=%s", lead["_id"], invitation_id
    )
    return {
        "status": "sent",
        "invitation_id": invitation_id,
        "lead_id": str(lead["_id"]),
    }
