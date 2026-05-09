"""
Gate 2 (primary model): drop analyst-reportage posts.

These are posts where the author isn't sharing their own work — they're
relaying a Gartner / McKinsey / Forrester / press-release summary. The engine's
voice doesn't fit that genre and replies tend to feel performative.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


class _Verdict(BaseModel):
    drop: bool = Field(
        description=(
            "True if the post is primarily summarizing or quoting third-party analyst "
            "research (Gartner/McKinsey/Forrester/Deloitte/PWC/IDC etc) or a press release, "
            "rather than the author's own opinion or work."
        )
    )
    reason: str = Field(
        description="One short clause naming what's being summarized."
    )


_SYSTEM = """You are filtering LinkedIn posts for a B2B GTM engine.

Drop posts that are PRIMARILY analyst reportage:
- "Gartner just released..." / "McKinsey's new study shows..."
- Long quotes from analyst PDFs.
- Press-release summaries.
- News-aggregator posts where the author adds little of their own view.

KEEP posts where the author cites a stat or report briefly but the bulk of the content is their own take, story, or critique.

Be conservative. Most posts have at least one stat — that doesn't make them analyst reportage. Only drop when the post is ABOUT what an analyst said, not when an analyst stat is one supporting detail."""


def evaluate(*, post_text: str, author_name: str | None) -> _Verdict:
    return parse_structured_sync(
        model_tier="primary",
        system=_SYSTEM,
        user=(
            f"Author: {author_name or '(unknown)'}\n\n"
            f"Post:\n{post_text}"
        ),
        schema=_Verdict,
    )
