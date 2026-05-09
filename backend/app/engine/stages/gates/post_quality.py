"""
Gate 4 (cheap model): drop posts whose CONTENT quality is too thin or
manipulative for substantive engagement.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


class _Verdict(BaseModel):
    drop: bool = Field(
        description="True if the post is engagement-bait, vague platitudes, or otherwise un-engageable."
    )
    reason: str = Field(description="Short clause naming the quality issue.")


_SYSTEM = """You are filtering LinkedIn posts for a B2B GTM engine.

DROP if the post is:
- Engagement bait ("type AGREE if you think...", "comment a 1 if...", broken-rhythm fishing).
- Vague platitudes with no specific claim ("leadership is about people", "consistency wins" with no example).
- Pure self-promotion / event announcements with nothing to discuss.
- Lyrics, religious sermons, motivational quotes pasted from elsewhere.
- Posts under 200 chars that say nothing actionable.

KEEP if the post has a specific claim, story, or question — even if you disagree with it. Quality means engageable, not agreeable.

Be conservative — only drop when the post is genuinely un-engageable. Mediocre but specific posts are fine."""


def evaluate(*, post_text: str) -> _Verdict:
    return parse_structured_sync(
        model_tier="cheap",
        system=_SYSTEM,
        user=f"Post:\n{post_text}",
        schema=_Verdict,
    )
