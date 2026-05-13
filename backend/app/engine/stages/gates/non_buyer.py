"""
Gate 1 (cheap model): drop posts where the author is clearly NOT a buyer for
this product — job seekers, vendors selling to us, students, students of, etc.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.services.llm import parse_structured_sync

log = logging.getLogger(__name__)


class _Verdict(BaseModel):
    drop: bool = Field(
        description="True if the author is plainly not a buyer for the operator's product."
    )
    reason: str = Field(
        description="One short clause naming the disqualifier (e.g. 'job seeker', 'vendor pitch')."
    )


_SYSTEM = """You are filtering LinkedIn posts for a B2B GTM engine. The operator sells a product to a specific buyer persona. Your single job is to decide whether this author is plausibly a BUYER for the operator's product — not a competitor, not a vendor selling into the same buyers, not a job seeker.

PRIMARY DROP RULE — Competitor / parallel-vendor:
The single biggest miss in this gate is letting through authors whose company SELLS INTO the same buyer the operator targets. For a healthcare RCM operator, examples include: other RCM platforms, AI medical scribe vendors, prior-auth automation startups, EHR vendors, healthcare-AI consultancies, billing-tech vendors. These authors look "buyer-shaped" by title (VP Sales, Founder, Head of Product, Chief Revenue Officer) but they compete for the operator's buyer's budget — DROP.

Use the operator's product description + the author's company name + the author's title to make this call. If the company name unmistakably points at a competitor vendor in the same category as the operator's product, DROP regardless of how the post reads.

PRIMARY KEEP RULE — In-industry buyer:
If the author works at a company in one of the operator's target industries AND the author's title matches (or is a peer of) one of the operator's target titles, KEEP — even if the post is commentary rather than a clear buying-signal.

Other drop categories (secondary):
- Job seekers ("open to opportunities", "happy to chat with hiring managers").
- Students, interns, recent grads with no decision-making power.
- Recruiters posting jobs.
- Personal life content with no business signal whatsoever.

Other keep categories (secondary):
- Practitioners describing their work, frustrations, or wins.
- Decision-makers in the target ICP.

Be aggressive on competitors. Be lenient on possible buyers — when in doubt about whether someone IS a competitor (vs a buyer), KEEP."""


def evaluate(
    *,
    post_text: str,
    author_name: str | None,
    author_title: str | None = None,
    author_company: str | None = None,
    product_summary: str,
    target_industries: list[str] | None = None,
    target_titles: list[str] | None = None,
) -> _Verdict:
    industries = ", ".join(target_industries or []) or "(unspecified)"
    # Cap target titles list — long lists add prompt cost with diminishing
    # marginal signal past ~30 entries.
    titles_capped = (target_titles or [])[:30]
    titles = ", ".join(titles_capped) or "(unspecified)"
    user_msg = (
        f"Operator's product:\n{product_summary}\n\n"
        f"Operator's target buyer profile:\n"
        f"  industries: {industries}\n"
        f"  titles: {titles}\n\n"
        f"Author:\n"
        f"  name: {author_name or '(unknown)'}\n"
        f"  title: {author_title or '(unknown)'}\n"
        f"  company: {author_company or '(unknown)'}\n\n"
        f"Post:\n{post_text}"
    )
    return parse_structured_sync(
        model_tier="cheap",
        system=_SYSTEM,
        user=user_msg,
        schema=_Verdict,
    )
