"""
Auto-CR (PERMANENT RULE 5). When a lead replies twice in S2/S3 with no CR yet:
  1. Extract a 3-7 word topic phrase from the candidate's post + our comment
     (gpt-4.1-mini).
  2. Draft a CR note with that phrase as the specific_signal (gpt-5.4).
  3. Validate the note (no generic placeholder phrases, no buzzwords, ≤200 chars).
  4. Send via Unipile, advance lead → S4.
"""
from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pydantic import BaseModel, Field
from pymongo.database import Database

from app.engine.stages.validator import (
    ValidationResult,
    has_generic_cr_phrase,
    validate_cr_note,
)
from app.models.common import utcnow
from app.services.openai_client import OpenAINotConfigured, parse_structured_sync
from app.services.unipile import (
    UnipileError,
    UnipileNotConfigured,
    resolve_profile,
    send_invite,
)

log = logging.getLogger(__name__)

_FALLBACK_TEMPLATE = (
    "Hi {first}, enjoyed your perspective on {signal}. "
    "We have been mapping the same pattern at our product and would love to "
    "stay in touch. {sender_first}"
)


class _CRNote(BaseModel):
    note: str = Field(
        description=(
            "The connection-request note body. <=200 chars. Opens with "
            "'Hi {first},' or 'Hey {first},'. References the specific signal "
            "as a real topic phrase. Soft connect ask close. First-name "
            "sign-off. No em-dashes, no ellipses, no buzzwords."
        )
    )


class _Topic(BaseModel):
    topic_phrase: str = Field(
        description=(
            "A 3-7 word phrase capturing what the post is about. Should be "
            "specific enough that 'enjoyed your post on {topic_phrase}' would "
            "read like a real reference. Example: 'GLP-1 cash-pay prescriber "
            "data' or 'platform engineer hiring on golden paths'."
        )
    )


_TOPIC_EXTRACTION_PROMPT = """\
You distill a LinkedIn post into a 3-7 word topic phrase that another reader could use to reference it specifically. Avoid generic words like "post" or "thread" in your output. Capture the actual subject.

Examples:
- A post about Amazon One Medical's GLP-1 program and cash-pay prescriber behaviour -> "Amazon One Medical GLP-1 cash-pay shift"
- A post about hiring a platform engineer to build internal developer platforms -> "platform engineer hiring on golden paths"
- A post about MSL strategy and whether scientific insight ever changes a brand decision -> "MSL insight to brand decision loop"
- A post about CSO integrity and HCP call-list quality -> "CSO call-list integrity at launch"

Output the phrase only in the `topic_phrase` field. No leading article. Lowercase except proper nouns.
"""


CR_NOTE_SYSTEM_PROMPT = """\
You draft a LinkedIn connection request note (max 200 chars) in {cofounder_name}'s voice.

# CONTEXT

Recipient: {prospect_name}, {prospect_title} at {prospect_company}
What they did: {trigger_action}
Specific signal to reference: {specific_signal}
Sender first name: {sender_first}

# VERBATIM EXEMPLARS

These are CR notes that produced accepts and led to booked meetings.

Awanish Pandey (post-like signal):
Hi Awanish, enjoyed your GLP-1 post on the Amazon One Medical program. We've been mapping cash-pay prescriber behaviour at G LNK and would love to stay in touch. Alex

Matteo Favilli (post-reply signal):
Hey Matteo, really enjoyed your reply on the Novo data-trust thread. Would be great to stay in touch and swap notes on where pharma commercial teams actually act vs. ignore their data. Alex

Myriam Cherif (post-reply signal):
Hi Myriam, enjoyed the sparring-partner framing on your AI post. Would love to stay in touch and compare notes on how medical affairs teams are using AI for KOL prep. Alex

Davera Gabriel (follow-only signal):
Hi Davera, thanks for the follow. Your take on fixing the data layer before the AI layer is exactly how we see it too at our product. Would love to connect. Alex

Bruce Copeland (author-like signal):
Hi Bruce, really enjoyed your evidence-negotiations framing. Commented on your post yesterday and saw the like. Would love to stay in touch on RWE and launch strategy. Alex

# STRUCTURAL RULES

1. Max 200 characters total.
2. Open with "Hi {prospect_first}," or "Hey {prospect_first},".
3. Reference the specific signal in one phrase. The signal is "{specific_signal}". Use it as the ACTUAL topic — never write "your recent post" or "the thread we engaged on" as a placeholder.
4. Claim a sliver of relevance ("we have been mapping X at our product" / "the angle that has changed for us is Y" / "we see this exact pattern with Z").
5. End with soft connect ask: "would love to stay in touch", "would love to connect", "would be great to swap notes". NEVER "let's hop on a call".
6. Sign with first name only ({sender_first}).
7. NO dashes anywhere (RULE 5): em-dash (—), en-dash (–), or double-hyphen (--). NO ellipses ("..." or "…"). NO buzzwords.
8. Output the CR note text only in the `note` field.
"""


def _first_name(full: str | None) -> str:
    if not full:
        return "there"
    parts = full.strip().split()
    return parts[0] if parts else "there"


def extract_topic_phrase(*, post_text: str, our_comment: str | None = None) -> str:
    """gpt-4.1-mini extracts a 3-7 word topic phrase. Returns '' on failure."""
    if not (post_text or "").strip():
        return ""
    user = f"Post:\n{post_text.strip()}"
    if (our_comment or "").strip():
        user += f"\n\nOur comment on it:\n{our_comment.strip()}"
    try:
        result = parse_structured_sync(
            model_tier="cheap",
            system=_TOPIC_EXTRACTION_PROMPT,
            user=user,
            schema=_Topic,
        )
        phrase = (result.topic_phrase or "").strip().strip('"').strip("'")
        return phrase[:80]
    except Exception as err:
        log.warning("topic extractor failed: %s", err)
        return ""


def _is_generic_signal(signal: str) -> bool:
    s = (signal or "").strip().lower()
    if not s or len(s) < 6:
        return True
    has_generic, _ = has_generic_cr_phrase(s)
    return has_generic


def _draft_with_retry(
    system_kwargs: dict[str, Any], retry_constraint: str | None = None
) -> tuple[str, ValidationResult]:
    system = CR_NOTE_SYSTEM_PROMPT.format(**system_kwargs)
    if retry_constraint:
        system += f"\n\nADDITIONAL CONSTRAINT (retry):\n{retry_constraint}\n"
    result = parse_structured_sync(
        model_tier="primary",
        system=system,
        user="Draft the CR note now. Output the body only in the `note` field.",
        schema=_CRNote,
    )
    note = result.note.strip()
    return note, validate_cr_note(note)


def draft_cr_note(
    *,
    cofounder_name: str,
    sender_first: str,
    prospect_name: str | None,
    prospect_title: str | None,
    prospect_company: str | None,
    trigger_action: str,
    specific_signal: str,
) -> tuple[str, ValidationResult]:
    """
    Returns (note, validation_result). Raises ValueError if `specific_signal`
    is empty / None / generic.
    """
    if not specific_signal or not specific_signal.strip():
        raise ValueError("specific_signal is required")
    if _is_generic_signal(specific_signal):
        raise ValueError(
            f"specific_signal is too generic: {specific_signal!r}. Pass a real "
            "topic phrase from the candidate (e.g. 'GLP-1 cash-pay prescriber data')."
        )

    system_kwargs = dict(
        cofounder_name=cofounder_name or "the cofounder",
        prospect_name=prospect_name or "(unknown)",
        prospect_first=_first_name(prospect_name),
        prospect_title=prospect_title or "(unknown title)",
        prospect_company=prospect_company or "(unknown company)",
        trigger_action=trigger_action or "Replied 2+ times on a shipped comment thread",
        specific_signal=specific_signal,
        sender_first=sender_first or "Alex",
    )

    try:
        note, result = _draft_with_retry(system_kwargs)
    except OpenAINotConfigured:
        return (
            _FALLBACK_TEMPLATE.format(
                first=_first_name(prospect_name),
                signal=specific_signal,
                sender_first=sender_first or "Alex",
            ),
            ValidationResult(False, "openai_not_configured"),
        )

    if not result.ok:
        log.warning(
            "CR note failed validator (%s): %r — retrying with hardened constraint",
            result.reason,
            note,
        )
        retry_constraint = (
            f"The previous draft failed: {result.reason}. "
            f"Use the topic phrase '{specific_signal}' in the body — do NOT use "
            "placeholder phrases like 'the product thread', 'the post we engaged "
            "on', 'your recent post', 'the thread we engaged on'. Make sure the "
            "note is <=200 chars."
        )
        try:
            note, result = _draft_with_retry(system_kwargs, retry_constraint=retry_constraint)
        except OpenAINotConfigured:
            pass

    return note, result


def _signal_from_candidate(
    db: Database, lead: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Find the candidate that triggered this lead's auto-CR and extract a
    topic phrase from its post."""
    candidate_ids = lead.get("candidate_ids") or []
    if not candidate_ids:
        return "", None
    cand = db.candidates.find_one(
        {"_id": {"$in": candidate_ids}, "status": {"$in": ["shipped", "slated", "drafted"]}},
        sort=[("shipped_at", -1)],
    ) or db.candidates.find_one({"_id": {"$in": candidate_ids}})
    if not cand:
        return "", None
    phrase = extract_topic_phrase(
        post_text=cand.get("post_text") or "",
        our_comment=cand.get("comment_text"),
    )
    return phrase, cand


def maybe_trigger(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder: dict[str, Any],
    lead: dict[str, Any],
) -> dict[str, Any] | None:
    """PERMANENT-RULE-5 check. Drafts via LLM (with topic extracted) unless
    cofounder.connect_message_template is pinned (operator override)."""
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

    pinned = (cofounder.get("connect_message_template") or "").strip()
    sender_first = _first_name(cofounder.get("display_name"))
    cr_validation: ValidationResult | None = None

    if pinned:
        message = pinned[:300]
    else:
        signal, _cand = _signal_from_candidate(db, lead)
        if not signal:
            log.warning(
                "auto-CR skipped: could not extract topic phrase for lead=%s",
                lead.get("_id"),
            )
            return {"status": "skipped_no_topic_signal"}
        try:
            note, cr_validation = draft_cr_note(
                cofounder_name=cofounder.get("display_name") or "",
                sender_first=sender_first,
                prospect_name=lead.get("name"),
                prospect_title=lead.get("title"),
                prospect_company=lead.get("company"),
                trigger_action="Replied 2+ times on a shipped comment thread",
                specific_signal=signal,
            )
        except ValueError as err:
            log.warning("auto-CR skipped: %s", err)
            return {"status": "skipped_invalid_signal", "error": str(err)}
        except Exception as err:
            log.warning("CR-note drafter failed for lead=%s: %s", lead.get("_id"), err)
            return {"status": "skipped_drafter_error", "error": str(err)}
        message = note[:300]

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
        return {"status": "unipile_not_configured", "error": str(err)}
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
                "cr_note": message,
                "cr_note_validation": cr_validation.reason if cr_validation else None,
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
                "note_chars": len(message),
                "validation_ok": (cr_validation.ok if cr_validation else None),
                "validation_reason": (cr_validation.reason if cr_validation else None),
            },
            "severity": "info",
            "created_at": now,
        }
    )
    log.info("auto-CR sent: lead=%s invitation_id=%s", lead["_id"], invitation_id)
    return {
        "status": "sent",
        "invitation_id": invitation_id,
        "lead_id": str(lead["_id"]),
        "note": message,
    }
