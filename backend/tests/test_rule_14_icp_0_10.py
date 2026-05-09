"""
RULE 14 — ICP scoring projects to a 0-10 scale; drop threshold ≤5.

The LLM still returns per-axis breakdowns and a raw `total`; we project that
total onto the 0-10 audit scale via compute_score_0_10. The expensive-gates
threshold check uses the projected score; the allocator sorts on it too.
"""
from __future__ import annotations

from app.engine.stages.allocator import _candidate_score
from app.engine.stages.gates.icp_scoring import (
    compute_score_0_10,
    max_possible_total,
)


# A representative rubric — top tiers sum to 13 (matches the shipped glnk
# baseline: title 5 + industry 3 + geography 2 + stage 3).
_RUBRIC_13 = {
    "title": {"tiers": [{"score": 5}, {"score": 3}, {"score": 1}]},
    "industry": {"tiers": [{"score": 3}]},
    "geography": {"tiers": [{"score": 2}]},
    "stage": {"tiers": [{"score": 3}]},
    "threshold": 6,
}


# ---- max_possible_total ----


def test_max_possible_total_sums_top_tier_per_axis():
    assert max_possible_total(_RUBRIC_13) == 13


def test_max_possible_total_handles_missing_axes():
    """A rubric with only title still has a sane max — useful when an
    operator's onboarding extraction omits stage / geography axes."""
    rubric = {"title": {"tiers": [{"score": 5}]}}
    assert max_possible_total(rubric) == 5


def test_max_possible_total_zero_for_empty_rubric():
    assert max_possible_total({}) == 0
    assert max_possible_total({"title": {"tiers": []}}) == 0


# ---- compute_score_0_10 ----


def test_score_0_10_at_max_is_10():
    assert compute_score_0_10(13, _RUBRIC_13) == 10


def test_score_0_10_at_zero_is_zero():
    assert compute_score_0_10(0, _RUBRIC_13) == 0


def test_score_0_10_clamps_above_max():
    """If the LLM somehow returns total > max_possible (shouldn't happen,
    but never trust LLM math), we clamp at 10."""
    assert compute_score_0_10(99, _RUBRIC_13) == 10


def test_score_0_10_clamps_negative():
    assert compute_score_0_10(-5, _RUBRIC_13) == 0


def test_score_0_10_audit_drop_threshold():
    """Drop ≤5: score_0_10 of 5 should be the highest score that fails the
    threshold-6 check."""
    # Working backwards: round(total * 10 / 13) = 5 → total ∈ [6, 7]
    # Round-half-to-even rule (banker's rounding) means 6.5→6 in Python.
    # Score for total=7: round(70/13) = round(5.38) = 5 → drops.
    # Score for total=8: round(80/13) = round(6.15) = 6 → passes.
    assert compute_score_0_10(7, _RUBRIC_13) == 5  # drops at threshold 6
    assert compute_score_0_10(8, _RUBRIC_13) == 6  # passes


def test_score_0_10_zero_max_zero_score():
    assert compute_score_0_10(5, {}) == 0


# ---- allocator picks score_0_10 over total ----


def test_allocator_score_uses_0_10_when_present():
    c = {"gate_results": {"icp": {"total": 13, "score_0_10": 10}}}
    assert _candidate_score(c) == 10


def test_allocator_falls_back_to_total_for_legacy_candidates():
    """Pre-RULE-14 candidates have `total` but no `score_0_10`. Allocator
    must still rank them, just on the raw total."""
    c = {"gate_results": {"icp": {"total": 11}}}
    assert _candidate_score(c) == 11


def test_allocator_handles_missing_gate_results():
    assert _candidate_score({}) == 0
    assert _candidate_score({"gate_results": {}}) == 0
    assert _candidate_score({"gate_results": {"icp": {}}}) == 0


# ---- realistic rubric examples ----


def test_score_0_10_glnk_default_threshold_works():
    """A candidate that hits title-tier1 (5) + industry (3) + geography (2)
    = total 10 should comfortably pass on the glnk default rubric."""
    rubric = {
        "title": {"tiers": [{"score": 5}, {"score": 3}, {"score": 1}]},
        "industry": {"tiers": [{"score": 3}]},
        "geography": {"tiers": [{"score": 2}]},
        "stage": {"tiers": [{"score": 3}]},
        "threshold": 6,
    }
    assert compute_score_0_10(10, rubric) == 8  # comfortably above 6


def test_score_0_10_distinguishes_marginal_from_strong():
    """Marginal hit (just title-tier3 + industry = 4 raw) should land at the
    drop boundary, not above it."""
    rubric = {
        "title": {"tiers": [{"score": 5}, {"score": 3}, {"score": 1}]},
        "industry": {"tiers": [{"score": 3}]},
        "geography": {"tiers": [{"score": 2}]},
        "stage": {"tiers": [{"score": 3}]},
    }
    # title tier3 (1) + industry (3) = 4 raw → score = round(40/13) = 3 → drops
    assert compute_score_0_10(4, rubric) == 3
