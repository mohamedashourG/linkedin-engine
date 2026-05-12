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
- **Short title acronyms** (about 4 characters or fewer: CFO, CRO, COO, CIO, CHRO, CEO, etc.): credit a title-tier term only when it appears as a **whole word/token** in the headline or post (word boundaries), not as a substring inside an unrelated word (e.g. do not treat "cro" inside "across" as CRO).
- The rationale is for audit. Keep under 20 words and name the evidence type ('explicit', 'post-explicit', 'inferred')."""


def max_possible_total(rubric: dict[str, Any]) -> int:
    """Sum of top-tier scores across the four axes. Used to normalize
    `total` onto the audit's 0-10 scale (RULE 14)."""
    out = 0
    for axis in ("title", "industry", "geography", "stage"):
        tiers = (rubric.get(axis) or {}).get("tiers") or []
        if not tiers:
            continue
        out += max(
            (int(t.get("score", 0) or 0) for t in tiers if isinstance(t, dict)),
            default=0,
        )
    return out


def compute_score_0_10(total: int, rubric: dict[str, Any]) -> int:
    """RULE 14 — collapse raw axis sums onto a 0-10 score.

    Linear projection: score_0_10 = round(total / max_possible * 10), clamped
    to [0, 10]. A rubric with no tiers (or all-zero scores) maps every
    candidate to 0."""
    cap = max_possible_total(rubric)
    if cap <= 0:
        return 0
    return max(0, min(10, round(total * 10 / cap)))


def _top_tier_score(rubric: dict[str, Any], axis: str) -> tuple[int, list[str]]:
    """Return (score, matched_terms) for the highest tier on the given axis."""
    tiers = (rubric.get(axis) or {}).get("tiers") or []
    if not tiers:
        return 0, []
    best = max(tiers, key=lambda t: int(t.get("score", 0) or 0))
    return int(best.get("score", 0) or 0), list(best.get("matches") or [])


def evaluate(
    *,
    post_text: str,
    author_name: str | None,
    author_title: str | None = None,
    author_company: str | None = None,
    author_location: str | None = None,
    author_title_levels: list[str] | None = None,
    author_company_industry: str | None = None,
    author_company_description: str | None = None,
    author_company_employee_range: str | None = None,
    author_company_employees: int | None = None,
    author_company_founded_year: int | None = None,
    author_company_specialities: list[str] | None = None,
    icp_rubric: dict[str, Any],
    geo_verified_at_source: bool = False,
    industry_verified_at_source: bool = False,
) -> _IcpScore:
    """Score author against the operator's ICP rubric.

    When ``geo_verified_at_source`` is True the candidate came from a source
    that already enforced geography server-side (Unipile RULE 24 people-search
    with geoUrn, Crustdata watcher with AUTHOR_LOCATION). We bypass the LLM's
    re-evaluation of the geo axis and credit it at the rubric's top tier —
    the LLM scoring is rubric-faithful, so a too-narrow rubric (e.g.
    ``["United States", "US", "USA"]``) can wrongly drop verified-US authors
    whose location string is region-shaped (`"Atlanta Metropolitan Area"`).

    Same for ``industry_verified_at_source`` — when the people-search applied
    the INDUSTRY filter, all returned candidates are guaranteed in-industry.

    ``author_company_*`` fields come from APIDirect /v1/linkedin/company when
    enrichment ran. They provide structured industry classification (vs
    inferring from a company name), employee count for stage scoring, and
    specialties/description for richer industry signals. All optional —
    when the structured fetch failed the LLM falls back to inferring from
    the headline + post text as before.
    """
    rubric_text = _format_rubric(icp_rubric)
    author_block: list[str] = []
    if author_name:
        author_block.append(f"Name: {author_name}")
    if author_title:
        author_block.append(f"Title: {author_title}")
    if author_company:
        author_block.append(f"Company: {author_company}")
    if author_location:
        author_block.append(f"Location: {author_location}")
    if author_title_levels:
        author_block.append(
            "Seniority / title levels (enrichment): "
            + ", ".join(str(x) for x in author_title_levels if x)
        )
    # Structured company facts — when present, the LLM should weight them
    # higher than what it infers from the headline alone (EXPLICIT tier per
    # the system prompt's evidence priority).
    if author_company_industry:
        author_block.append(f"Company industry (LinkedIn-structured): {author_company_industry}")
    if author_company_employee_range:
        author_block.append(f"Company size (employee range): {author_company_employee_range}")
    elif author_company_employees:
        author_block.append(f"Company size (employees): {author_company_employees}")
    if author_company_founded_year:
        author_block.append(f"Company founded: {author_company_founded_year}")
    if author_company_specialities:
        specs = ", ".join(str(x) for x in author_company_specialities[:8] if x)
        if specs:
            author_block.append(f"Company specialties: {specs}")
    if author_company_description:
        desc = author_company_description[:300]
        author_block.append(f"Company description: {desc}")
    if not author_block:
        author_block.append("(no enriched data available — infer from post)")

    user_msg = (
        f"Rubric:\n{rubric_text}\n\n"
        f"AUTHOR DATA:\n{chr(10).join(author_block)}\n\n"
        f"POST:\n{post_text}"
    )
    result = parse_structured_sync(
        model_tier="primary",
        system=_SYSTEM,
        user=user_msg,
        schema=_IcpScore,
    )

    # Server-side filter overrides — only credit when the LLM didn't already
    # score the axis at top tier (avoid double-counting if it did match).
    overridden_axes: list[str] = []
    if geo_verified_at_source:
        top, matches = _top_tier_score(icp_rubric, "geography")
        if top > result.geography.score:
            result.geography.score = top
            # First few rubric terms shown for audit. Real evidence is the
            # source-side filter that guaranteed the geography match.
            result.geography.matched_terms = [
                "verified at source (server-side geoUrn)",
                *matches[:2],
            ]
            overridden_axes.append("geography")
    if industry_verified_at_source:
        top, matches = _top_tier_score(icp_rubric, "industry")
        if top > result.industry.score:
            result.industry.score = top
            result.industry.matched_terms = [
                "verified at source (server-side INDUSTRY filter)",
                *matches[:2],
            ]
            overridden_axes.append("industry")
    if overridden_axes:
        result.total = (
            result.title.score
            + result.industry.score
            + result.geography.score
            + result.stage.score
        )
        suffix = f" [auto-credited at top tier: {', '.join(overridden_axes)}]"
        result.rationale = (result.rationale or "") + suffix

    return result


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
