"""
Gate 3 (primary model): score the post's author against the operator's
icp_rubric across 4 axes (title, industry, geography, stage). Sum the per-tier
scores to a total; drop if the total is below the rubric's threshold.

The model is asked to return per-axis breakdowns so we can audit-trail why a
candidate scored what it did (this lands in candidate.gate_results.icp).
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


class _AxisBreakdown(BaseModel):
    matched_terms: list[str] = Field(
        description="Rubric matches the model recognized in the post / author signal."
    )
    score: int = Field(description="Sum of scores from rubric tiers that matched.")


class _IcpScore(BaseModel):
    title: _AxisBreakdown
    industry: _AxisBreakdown
    geography: _AxisBreakdown
    stage: _AxisBreakdown
    total: int = Field(description="Sum of all four axis scores.")
    rationale: str = Field(
        description="One short sentence explaining the dominant signal."
    )


_SYSTEM = """You are scoring a LinkedIn post's AUTHOR against an ICP rubric.

You will receive a rubric with four axes (title, industry, geography, stage), each with tiered match lists and per-tier scores. For each axis, identify which rubric terms (if any) are evidenced by what you can see, and SUM the matched tiers' scores.

Evidence priority (use the strongest available, don't double-count):
1. EXPLICIT — the author's known title/company/location appears in the AUTHOR DATA block.
2. POST-EXPLICIT — the post text directly states the author's title, company, industry, or geography.
3. INFERRED — strong signals in HOW the author writes (vocabulary, scope of decisions described, problems they've owned). Inference is allowed, but score one tier LOWER than the rubric tier you'd otherwise credit. Example: a post that reads like a VP of Engineering wrote it, when the highest-applicable tier is tier-1 (score 5), should score the tier-2 amount instead.

Rules:
- A given axis matches at most ONE tier — the highest you can justify with the available evidence.
- If no evidence at all on an axis, score=0 and matched_terms=[].
- The rationale is for audit. Keep under 20 words and name the evidence type ('explicit', 'post-explicit', 'inferred')."""


def evaluate(
    *,
    post_text: str,
    author_name: str | None,
    author_title: str | None = None,
    author_company: str | None = None,
    icp_rubric: dict[str, Any],
) -> _IcpScore:
    rubric_text = _format_rubric(icp_rubric)
    author_block: list[str] = []
    if author_name:
        author_block.append(f"Name: {author_name}")
    if author_title:
        author_block.append(f"Title: {author_title}")
    if author_company:
        author_block.append(f"Company: {author_company}")
    if not author_block:
        author_block.append("(no enriched data available — infer from post)")

    user_msg = (
        f"Rubric:\n{rubric_text}\n\n"
        f"AUTHOR DATA:\n{chr(10).join(author_block)}\n\n"
        f"POST:\n{post_text}"
    )
    return parse_structured_sync(
        model_tier="primary",
        system=_SYSTEM,
        user=user_msg,
        schema=_IcpScore,
    )


def _format_rubric(rubric: dict[str, Any]) -> str:
    lines: list[str] = []
    for axis in ("title", "industry", "geography", "stage"):
        axis_block = rubric.get(axis) or {}
        tiers = axis_block.get("tiers") or []
        lines.append(f"# {axis}")
        if not tiers:
            lines.append("  (no tiers defined)")
            continue
        for i, tier in enumerate(tiers, start=1):
            matches = tier.get("matches") or []
            score = tier.get("score", 0)
            lines.append(
                f"  tier {i} (score {score}): {', '.join(matches) if matches else '—'}"
            )
    threshold = rubric.get("threshold", 0)
    lines.append(f"\nThreshold (drop if total < threshold): {threshold}")
    return "\n".join(lines)
