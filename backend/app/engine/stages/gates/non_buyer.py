"""
Gate 1 (cheap model): drop posts where the author is clearly NOT a buyer for
this product — job seekers, vendors selling to us, students, students of, etc.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


class _Verdict(BaseModel):
    drop: bool = Field(
        description="True if the author is plainly not a buyer for the operator's product."
    )
    reason: str = Field(
        description="One short clause naming the disqualifier (e.g. 'job seeker', 'vendor pitch')."
    )


_SYSTEM = """You are filtering LinkedIn posts for a B2B GTM engine. Your single job is to drop authors who are NOT buyers for the product.

Drop categories:
- Job seekers ("looking for an SDR role", "open to opportunities", "happy to chat with hiring managers").
- Vendors selling INTO the operator (not buyers).
- Students, interns, recent grads with no decision-making power.
- Recruiters posting jobs.
- People posting personal life content with no business signal.
- People at competitor vendors.

Keep:
- Practitioners describing their work, frustrations, or wins.
- Buyers / decision-makers in the target ICP.
- Anyone who could realistically buy or champion the product, even tangentially.

Be aggressive only on clear-cut disqualifiers. When in doubt, KEEP."""


def evaluate(*, post_text: str, author_name: str | None, product_summary: str) -> _Verdict:
    user_msg = (
        f"Operator's product:\n{product_summary}\n\n"
        f"Author: {author_name or '(unknown)'}\n\n"
        f"Post:\n{post_text}"
    )
    return parse_structured_sync(
        model_tier="cheap",
        system=_SYSTEM,
        user=user_msg,
        schema=_Verdict,
    )
