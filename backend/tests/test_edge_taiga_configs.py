"""
Smoke + invariant tests for the Edge and Taiga client configs (RULE 8 + 17).

These verify:
  - JSON parses
  - Tier-1 titles include the buy-side roles each client sells to
  - Vertical buckets exist (RULE 8 pharma split for glnk; specialty buckets for taiga)
  - Per-client drafter guardrails carry the expected anchors / banned phrases
"""
from __future__ import annotations

from app.services.client_config import load, reset_cache


def setup_function(_):
    reset_cache()


# ---- Edge ----


def test_edge_config_loads():
    cfg = load("edge")
    assert cfg.slug == "edge"
    assert cfg.display_name == "Edge"


def test_edge_targets_health_systems_at_tier1():
    """Edge sells INTO health systems / hospitals — buy-side roles must score
    tier-1, not tier-3."""
    cfg = load("edge")
    tier1 = cfg.icp_rubric["title"]["tiers"][0]
    assert tier1["score"] == 5
    matches = {m.lower() for m in tier1["matches"]}
    # Spot-check the three lanes Edge runs (HR, RCM, Operations).
    assert "chro" in matches
    assert "vp human resources" in matches
    assert "vp revenue cycle" in matches
    assert "chief operating officer" in matches


def test_edge_industry_includes_health_systems_not_pharma():
    cfg = load("edge")
    industries = " ".join(
        m for tier in cfg.icp_rubric["industry"]["tiers"] for m in tier["matches"]
    ).lower()
    assert "hospitals" in industries or "health systems" in industries
    assert "pharmaceuticals" not in industries
    assert "biotechnology" not in industries


def test_edge_drafter_guardrails_include_anchors_and_staffing_ban():
    cfg = load("edge")
    guard = cfg.drafter_guardrails
    banned = [s.lower() for s in guard.get("extra_banned_phrases", [])]
    assert any("staffing agency" in s for s in banned), (
        "Edge config must ban 'staffing agency' (per-client guardrail)"
    )
    anchors = " ".join(guard.get("anchors", [])).lower()
    assert "rif paradox" in anchors
    assert "becker" in anchors
    assert "71%" in anchors


def test_edge_keyword_pool_has_three_lanes():
    """Edge runs three lanes (HR / RCM / Ops). Vertical buckets reflect that."""
    cfg = load("edge")
    buckets = cfg.keyword_pools["vertical_buckets"]
    assert "edge_hr" in buckets
    assert "edge_rcm" in buckets
    assert "edge_operations" in buckets


# ---- Taiga ----


def test_taiga_config_loads():
    cfg = load("taiga")
    assert cfg.slug == "taiga"
    assert cfg.display_name == "Taiga"


def test_taiga_physicians_score_tier1():
    """Taiga sells INTO physicians — physician-owner titles must NOT be
    excluded. They're tier-1 buyers."""
    cfg = load("taiga")
    tier1 = cfg.icp_rubric["title"]["tiers"][0]
    assert tier1["score"] == 5
    matches = {m.lower() for m in tier1["matches"]}
    assert "physician owner" in matches or "practice owner" in matches
    assert "solo practitioner" in matches
    assert "practice administrator" in matches


def test_taiga_specialty_anchors_cover_audit_specialties():
    """The user's prompt specified six specialties. All six must have a
    drafter angle."""
    cfg = load("taiga")
    specialties = cfg.drafter_guardrails["specialty_anchors"]
    expected = {
        "taiga_psychiatry",
        "taiga_dermatology",
        "taiga_podiatry",
        "taiga_cardiology",
        "taiga_oncology",
        "taiga_internal_medicine",
    }
    assert expected.issubset(set(specialties.keys())), (
        f"missing taiga specialties: {expected - set(specialties.keys())}"
    )


def test_taiga_drafter_guardrails_lead_with_50pct_anchor():
    cfg = load("taiga")
    anchors = " ".join(cfg.drafter_guardrails.get("anchors", [])).lower()
    assert "50%" in anchors and "mental-health" in anchors, (
        "Taiga drafter must surface the 50% mental-health denial anchor"
    )
    assert "2% vs 5" in anchors or "2%" in anchors, (
        "Taiga drafter must surface the 2% vs 5-10% denial-rate anchor"
    )


def test_taiga_keyword_pool_has_specialty_buckets():
    cfg = load("taiga")
    buckets = cfg.keyword_pools["vertical_buckets"]
    for tag in (
        "taiga_psychiatry",
        "taiga_dermatology",
        "taiga_cardiology",
        "taiga_oncology",
        "taiga_internal_medicine",
        "taiga_podiatry",
    ):
        assert tag in buckets, f"missing keyword bucket {tag}"


# ---- glnk RULE 8 vertical bucket cleanup ----


def test_glnk_retired_pharma_commercial_bucket():
    """RULE 8: pharma_commercial split into 5 sub-buckets. Verify the new
    five exist and the old catch-all is gone."""
    cfg = load("glnk")
    buckets = cfg.keyword_pools.get("vertical_buckets", {})
    expected = {
        "pharma_data",
        "pharma_marketing",
        "pharma_launch_readiness",
        "pharma_commercial_strategy",
        "pharma_AI_omnichannel",
    }
    assert expected.issubset(set(buckets.keys())), (
        f"missing glnk pharma sub-buckets: {expected - set(buckets.keys())}"
    )
    assert "pharma_commercial" not in buckets, (
        "RULE 8 retired the catch-all pharma_commercial bucket"
    )
