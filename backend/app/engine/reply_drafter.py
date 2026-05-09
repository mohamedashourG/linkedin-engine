"""
Suggested-reply drafter. Given an incoming comment, produces a single follow-up
reply in the cofounder's voice.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.engine.constants import COMMENT_TYPE_DESCRIPTIONS
from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


class _ReplyDraft(BaseModel):
    suggested_reply: str = Field(
        description=(
            "Follow-up reply in the cofounder's voice. No em-dashes. No "
            "sycophantic openers. 60+ characters. Conversational and specific."
        )
    )
    suggested_reply_type: str = Field(
        description="One of A, B, C, D, E, F matching the comment-type taxonomy."
    )


def draft_reply(
    *,
    voice_template: str,
    original_post: str,
    our_comment: str,
    their_reply: str,
    they_are_post_author: bool,
) -> tuple[str, str]:
    """
    Returns (suggested_reply, suggested_reply_type).
    """
    system = (
        f"{voice_template}\n\n"
        "---\n"
        "You're drafting a follow-up reply on a LinkedIn comment thread. The flow:\n"
        "  1. Someone posted (ORIGINAL POST below).\n"
        "  2. The cofounder commented on it (OUR COMMENT below).\n"
        "  3. Someone replied to the cofounder's comment (THEIR REPLY below).\n"
        "  4. You're drafting the cofounder's response to that reply.\n\n"
        "Pick the comment type that fits the moment:\n"
        + "\n".join(f"  {k}: {v}" for k, v in COMMENT_TYPE_DESCRIPTIONS.items())
        + "\n\n"
        "Hard rules: no em-dashes (use commas/periods), 60+ characters, no "
        "sycophantic openers, sounds like a single specific human."
    )

    relationship = (
        "The replier IS the post author themselves."
        if they_are_post_author
        else "The replier is a third party (not the post author)."
    )
    user = (
        f"ORIGINAL POST:\n{original_post}\n\n"
        f"OUR COMMENT:\n{our_comment}\n\n"
        f"THEIR REPLY:\n{their_reply}\n\n"
        f"Context: {relationship}"
    )
    result = parse_structured_sync(
        model_tier="primary",
        system=system,
        user=user,
        schema=_ReplyDraft,
    )
    rtype = result.suggested_reply_type.strip().upper()[:1]
    if rtype not in {"A", "B", "C", "D", "E", "F"}:
        rtype = "A"
    return result.suggested_reply.strip(), rtype
