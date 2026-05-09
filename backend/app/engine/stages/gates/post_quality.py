"""
Gate 4 (cheap model): drop posts whose content quality bars us from engaging.

RULE 19 binary pass/fail:
  1. AVOID list (engagement bait, platitudes, lyrics, sub-200-char filler).
  2. Then the post must offer at least ONE qualifying signal we can engage on:
     direct_expertise, adjacent_buyer, career_update, icp_author_personal,
     conference_mention.

Posts that survive AVOID but offer no qualifying signal still drop. The
specific signal type is persisted so RULE 25 (ICP-author primacy over
post-topic) can decide whether to rescue a high-ICP author whose post is
off-topic.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


_QualifyingSignal = Literal[
    "direct_expertise",
    "adjacent_buyer",
    "career_update",
    "icp_author_personal",
    "conference_mention",
    "none",
]


class _Verdict(BaseModel):
    drop: bool = Field(
        description=(
            "True if the post is engagement-bait, vague platitudes, lyrics, "
            "<200 chars of filler, OR offers no qualifying signal."
        )
    )
    reason: str = Field(description="Short clause naming the quality issue.")
    qualifying_signal: _QualifyingSignal = Field(
        description=(
            "Which positive signal this post offers (or 'none'). The five "
            "signals match the audit's RULE 19 list. Persisted for audit "
            "trail and for RULE 25's off-topic ICP-author rescue path."
        )
    )


_SYSTEM = """You are filtering LinkedIn posts for a B2B GTM engine. Two-step gate:

# STEP 1 — AVOID list (drop if any of these match)

- Engagement bait ("type AGREE if you think...", "comment a 1 if...", broken-rhythm fishing).
- Vague platitudes with no specific claim ("leadership is about people", "consistency wins" with no example).
- Pure self-promotion / event announcements with nothing to discuss.
- Lyrics, religious sermons, motivational quotes pasted from elsewhere.
- Posts under 200 chars that say nothing actionable.

# STEP 2 — qualifying-signal check

If the post survives STEP 1, it must offer AT LEAST ONE of these positive signals. Pick the strongest one:

- **direct_expertise** — the author describes their own work / data / decisions in the operator's ICP space.
- **adjacent_buyer** — the author works in a function or industry adjacent to the ICP and the post touches the operator's product space.
- **career_update** — promotion, new role, or move to / from an ICP company.
- **icp_author_personal** — a personal narrative (career reflection, gratitude, life event) from an ICP-fit author. Engagement here is warm-light, not substantive.
- **conference_mention** — author is speaking at, attending, or recapping a conference relevant to the ICP.
- **none** — the post is specific and non-baity but offers no anchor for our voice.

# OUTPUT RULES

- If STEP 1 matches → drop=true, qualifying_signal="none", reason names the AVOID category.
- If STEP 1 doesn't match but no qualifying signal applies → drop=true, qualifying_signal="none", reason="no qualifying signal".
- If a qualifying signal applies → drop=false, qualifying_signal=<the one>, reason names it.

Be conservative — when a post is mediocre but specific and the author is plausibly ICP, prefer 'icp_author_personal' or 'adjacent_buyer' over dropping. The audit's RULE 25 path will rescue high-ICP authors whose posts are off-topic, so a borderline drop here is recoverable."""


def evaluate(*, post_text: str) -> _Verdict:
    return parse_structured_sync(
        model_tier="cheap",
        system=_SYSTEM,
        user=f"Post:\n{post_text}",
        schema=_Verdict,
    )
