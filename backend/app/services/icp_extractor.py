"""
Extract ICP / keyword tiers from an operator's free-text product description.

Returns:
  - product_extracted (the structured view the user reviews & edits)
  - icp_rubric (derived from extracted titles/industries with default scoring)

Both shapes match the user document structure in the spec.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.llm import parse_structured


class _KeywordTiers(BaseModel):
    tier_1: list[str] = Field(
        description=(
            "Strong, narrow keywords ICP buyers use unprompted. 6-12 phrases, each 2-5 "
            "words. These are the top-of-funnel discovery queries."
        )
    )
    tier_2: list[str] = Field(
        description=(
            "Medium-specificity adjacent keywords. 4-8 phrases. Used as fallback when "
            "tier-1 pool is exhausted."
        )
    )
    tier_3: list[str] = Field(
        description=(
            "Broad keywords that may surface ICP-adjacent content. 3-6 phrases."
        )
    )


class _TitleTiers(BaseModel):
    tier_1: list[str] = Field(
        description="Bullseye buyer titles (exact ICP). 4-10 phrases."
    )
    tier_2: list[str] = Field(
        description="Adjacent decision-makers / champions. 3-8 phrases."
    )
    tier_3: list[str] = Field(
        description="Peripheral roles that occasionally convert. 2-6 phrases."
    )


class _ProductExtraction(BaseModel):
    target_industries: list[str] = Field(
        description="3-8 industry labels the product serves (e.g. 'B2B SaaS', 'fintech')."
    )
    target_titles: _TitleTiers
    target_geographies: list[str] = Field(
        description=(
            "1-5 geography labels. Use 'Global' if no geo focus, otherwise specific "
            "regions (e.g. 'United States', 'EU', 'SF Bay Area')."
        )
    )
    target_pain_points: list[str] = Field(
        description="3-7 short phrases describing the pain the product alleviates."
    )
    suggested_keywords: _KeywordTiers


_SYSTEM = """You are an expert B2B GTM analyst. Given a free-text product description, extract a structured ICP and tiered keyword pools that a LinkedIn engagement engine will use to discover prospects.

Rules:
- Be specific, not generic. "VP of Engineering at Series B SaaS" beats "engineering leader".
- Tier 1 keywords are what ICP buyers literally type / post about. Tier 2 is adjacent. Tier 3 is broad.
- Tier 1 titles are the exact buyer. Tier 2 is champion. Tier 3 is peripheral influencer.
- Geographies should be regions, not cities, unless the product is geo-specific.
- Pain points are short phrases ("manual lead routing", "cold outbound burnout"), not paragraphs.
"""


def _flatten_titles(tiers: _TitleTiers) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in (*tiers.tier_1, *tiers.tier_2, *tiers.tier_3):
        norm = t.strip()
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out


def _build_rubric(extraction: _ProductExtraction) -> dict:
    """Derive icp_rubric from extracted titles/industries with default scores."""
    return {
        "title": {
            "tiers": [
                {"matches": extraction.target_titles.tier_1, "score": 5},
                {"matches": extraction.target_titles.tier_2, "score": 3},
                {"matches": extraction.target_titles.tier_3, "score": 1},
            ]
        },
        "industry": {
            "tiers": [{"matches": extraction.target_industries, "score": 3}]
        },
        "geography": {
            "tiers": [{"matches": extraction.target_geographies, "score": 2}]
        },
        "stage": {
            "tiers": [
                {
                    "matches": ["seed", "series-a", "series-b"],
                    "score": 3,
                }
            ]
        },
        "threshold": 6,
    }


async def extract(free_text: str) -> dict:
    """
    Extract ICP and keyword tiers from a free-text product description.

    Returns a dict shaped like:
      {
        "product_extracted": {
          target_industries, target_titles (flat), target_geographies,
          target_pain_points, suggested_keywords: {tier_1, tier_2, tier_3}
        },
        "icp_rubric": { title, industry, geography, stage, threshold }
      }
    """
    extraction = await parse_structured(
        model_tier="primary",
        system=_SYSTEM,
        user=f"Product description:\n\n{free_text.strip()}",
        schema=_ProductExtraction,
    )
    rubric = _build_rubric(extraction)
    return {
        "product_extracted": {
            "target_industries": extraction.target_industries,
            "target_titles": _flatten_titles(extraction.target_titles),
            "target_geographies": extraction.target_geographies,
            "target_pain_points": extraction.target_pain_points,
            "suggested_keywords": extraction.suggested_keywords.model_dump(),
        },
        "icp_rubric": rubric,
    }
