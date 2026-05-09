"""
Reply drafter — handles outbound LinkedIn message contexts (PUBLIC_REPLY_BACK,
POST_CR_DM_HOT, POST_CR_DM_WARM, STAGE_6_DM). Validates the LLM output against
the context-specific rules and retries once with a hardened prompt on failure.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.engine.stages.validator import (
    ValidationResult,
    validate_dm,
    validate_public_reply_back,
)
from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


ReplyContext = Literal[
    "PUBLIC_REPLY_BACK",
    "POST_CR_DM_HOT",
    "POST_CR_DM_WARM",
    "STAGE_6_DM",
]


class _ReplyDraft(BaseModel):
    suggested_reply: str = Field(
        description=(
            "The outbound message body. No em-dashes in public comments. No "
            "'...' ellipses anywhere. Sign DMs with first name only. Calendly "
            "URL on its own line in DMs."
        )
    )
    suggested_reply_type: str = Field(
        default="A",
        description=(
            "One of A/B/C/D/E/F for public reply-backs. For DM contexts return "
            "'A' as a placeholder."
        ),
    )


REPLY_DRAFTER_SYSTEM_PROMPT = """\
You draft outbound LinkedIn messages in {cofounder_name}'s voice. There are four distinct contexts; use the right one for the {reply_context} signaled below.

# COFOUNDER VOICE

Tone: {cofounder_tone_description}
Calendly URL: {cofounder_calendly_url}
First name (for sign-off): {cofounder_first_name}

# CONTEXT

Reply context: {reply_context}
Original post by: {author_name} ({author_title} at {author_company})

Original post:
---
{post_text}
---

Our previous comment:
---
{our_comment}
---

What they did:
{their_action}

Prospect first name (for direct sign-off): {prospect_first}

# CONTEXT-SPECIFIC TEMPLATES — match these exactly

## PUBLIC_REPLY_BACK

3-4 sentences. Validate their specific point with a sharper observation, add one piece of new evidence, end with curiosity-question or implicit-DM-bridge. Never put the calendly URL in a public thread reply. Never start with "Exactly", "Spot on", "Great point", "Love this", "Couldn't agree more", "100%", "Well said", "Agreed".

Example (Shahad Stage-3 reply-back, on her "insight only matters if it drives action" reply):
The teams that close that loop in our experience are the ones who track which insights changed a brand decision versus which got logged. The 6 month retro is usually where it shows up, you can see which insights moved an HCP target list and which only moved a slide. Curious how you handle the loop on your side.

## POST_CR_DM_HOT (CR just accepted, prospect engaged hot)

The locked Alex calendly-direct voice. ONE short sentence plus the calendly URL on its own line. No agenda. No pitch.

Pattern (use this exact shape; vary only the name):
Sure {prospect_first}, lets do that, feel free to put some time on my calendar.

{cofounder_calendly_url}

Verbatim Sam Dyer DM (canonical):
Sure Samuel, lets do that, feel free to put some time on my calendar.
https://calendly.com/alexander-glnkco/meeting-with-alex

Note: lowercase "lets" (not "let's"). This is intentional.

## POST_CR_DM_WARM

3-4 sentences. Reference a specific thing they liked or posted, claim a non-obvious data observation tied to it, offer to share, sign off. NO calendly URL in this DM.

Verbatim Awanish post-accept DM:
Hi Awanish, thanks for the accept. The Amazon GLP-1 plus One Medical thread you posted is one we keep coming back to. Cash-pay behaviour at the prescriber level is a signal most brands still are not measuring. Happy to share what we are seeing on the commercial side if useful. Alex

## STAGE_6_DM

The "loved our exchange" template. 2 sentences plus calendly URL on its own line. MUST contain "loved our exchange" or "really enjoyed our exchange".

Pattern:
Hey {prospect_first}, thanks for connecting. Really enjoyed our exchange on {{topic}}.

If a 20 minute call works, here's my calendar: {cofounder_calendly_url}

# RULES

1. Match the {reply_context} template structurally.
2. No dashes anywhere (RULE 5): em-dash (—), en-dash (–), and double-hyphen (--) are all banned in public replies AND DMs.
3. No "..." ellipses anywhere.
4. Calendly URL goes on its own line in DMs, never inline mid-sentence.
5. Sign DMs with first name only ("{cofounder_first_name}").
6. Lowercase "lets" (not "let's") in HOT.
7. No buzzwords ("leverage", "synergy", "value-add", "best-in-class", etc.).
8. Output the message text only in the `suggested_reply` field.
{additional_constraints}

# YOUR TASK

Draft ONE message for the {reply_context} context above. Follow the template exactly.
"""


def _first_name(full: str | None) -> str:
    if not full:
        return ""
    parts = full.strip().split()
    return parts[0] if parts else ""


def _build_constraints(context: str, retry_reason: str | None) -> str:
    if not retry_reason:
        return ""
    parts = [
        "RETRY HARDENING (the previous attempt failed validation):",
        f"  reason: {retry_reason}",
    ]
    if context == "POST_CR_DM_HOT":
        parts += [
            "  - Use the EXACT pattern: 'Sure <first>, lets do that, feel free to put some time on my calendar.' (lowercase lets).",
            "  - Then a blank line, then the calendly URL on its own line.",
            "  - No additional sentences, no agenda, no pitch.",
        ]
    elif context == "POST_CR_DM_WARM":
        parts += [
            "  - DO NOT include any calendly URL.",
            "  - 3-4 sentences. Reference their post specifically, claim a non-obvious observation, offer to share, sign off.",
        ]
    elif context == "STAGE_6_DM":
        parts += [
            "  - MUST contain 'loved our exchange' or 'really enjoyed our exchange'.",
            "  - MUST contain the calendly URL on its own line.",
        ]
    elif context == "PUBLIC_REPLY_BACK":
        parts += [
            "  - DO NOT start with 'Exactly', 'Spot on', 'Great point', 'Love this', 'Couldn't agree more', '100%', 'Well said', 'Agreed'.",
            "  - First sentence must reframe or add a sharper observation.",
        ]
    return "\n".join(parts)


def _validate(reply_context: str, text: str) -> ValidationResult:
    if reply_context == "PUBLIC_REPLY_BACK":
        return validate_public_reply_back(text)
    return validate_dm(text, reply_context)


def draft_reply(
    *,
    reply_context: ReplyContext = "PUBLIC_REPLY_BACK",
    cofounder_name: str,
    cofounder_tone: str,
    cofounder_first_name: str,
    cofounder_calendly_url: str,
    author_name: str | None,
    author_title: str | None,
    author_company: str | None,
    original_post: str,
    our_comment: str,
    their_reply: str,
    they_are_post_author: bool,
    prospect_name: str | None = None,
) -> tuple[str, str, ValidationResult]:
    """
    Returns (text, type, validation_result). The caller persists
    `validation_result` and can surface failures in the dashboard for manual
    override.
    """
    relationship = (
        "The replier IS the post author themselves."
        if they_are_post_author
        else "The replier is a third party (not the post author)."
    )
    their_action = f'Replied: "{their_reply.strip()}"\nContext: {relationship}'
    prospect_first = _first_name(prospect_name) or _first_name(author_name)

    def render(retry_reason: str | None = None) -> str:
        return REPLY_DRAFTER_SYSTEM_PROMPT.format(
            cofounder_name=cofounder_name or "the cofounder",
            cofounder_tone_description=cofounder_tone or "(not provided)",
            cofounder_calendly_url=cofounder_calendly_url or "(not set)",
            cofounder_first_name=cofounder_first_name or "Alex",
            reply_context=reply_context,
            author_name=author_name or "(unknown)",
            author_title=author_title or "(unknown title)",
            author_company=author_company or "(unknown company)",
            post_text=original_post,
            our_comment=our_comment,
            their_action=their_action,
            prospect_first=prospect_first or "there",
            additional_constraints=_build_constraints(reply_context, retry_reason),
        )

    def call(system: str) -> _ReplyDraft:
        return parse_structured_sync(
            model_tier="primary",
            system=system,
            user="Draft the message now. Output the message body only in the `suggested_reply` field.",
            schema=_ReplyDraft,
        )

    # First attempt.
    result = call(render())
    text = (result.suggested_reply or "").strip()
    rtype = (result.suggested_reply_type or "A").strip().upper()[:1]
    if rtype not in {"A", "B", "C", "D", "E", "F"}:
        rtype = "A"
    validation = _validate(reply_context, text)
    if validation.ok:
        return text, rtype, validation

    log.warning(
        "reply_drafter validator failed (%s): %s — retrying with hardened prompt",
        reply_context,
        validation.reason,
    )

    # One retry with hardened prompt.
    result = call(render(retry_reason=validation.reason))
    text = (result.suggested_reply or "").strip()
    validation = _validate(reply_context, text)
    if not validation.ok:
        log.error(
            "reply_drafter validator failed twice (%s): %s",
            reply_context,
            validation.reason,
        )
    return text, rtype, validation
