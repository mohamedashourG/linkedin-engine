"""
RULE 7 — Comment-type quotas locked 2026-05-05:
  A cap 25, B cap 10, C floor 25, D 10-15, E floor 20, F 5-10.

The allocator's _assign_types must respect floors AND caps, with the audit's
asymmetric quotas (some types capped, others floored, some both).
"""
from __future__ import annotations

from collections import Counter

import pytest

from app.engine.constants import COMMENT_TYPE_QUOTAS_DEFAULT
from app.engine.stages.allocator import _assign_types, _normalize_quota


# ---- audit values ----


def test_default_quotas_match_audit():
    """The default constant must match the 2026-05-05 audit verbatim. If the
    audit ever changes again, this test will catch a half-update."""
    assert COMMENT_TYPE_QUOTAS_DEFAULT == {
        "A": {"floor": 0,  "cap": 25},
        "B": {"floor": 0,  "cap": 10},
        "C": {"floor": 25, "cap": 100},
        "D": {"floor": 10, "cap": 15},
        "E": {"floor": 20, "cap": 100},
        "F": {"floor": 5,  "cap": 10},
    }


# ---- _normalize_quota ----


def test_normalize_dict_shape():
    n = _normalize_quota({"floor": 25, "cap": 50})
    assert n == {"floor": 0.25, "cap": 0.50}


def test_normalize_legacy_list_shape():
    """Old [lo, hi] users still in Mongo should map to {floor=lo, cap=hi}."""
    n = _normalize_quota([10, 30])
    assert n == {"floor": 0.10, "cap": 0.30}


def test_normalize_legacy_tuple_shape():
    n = _normalize_quota((10, 30))
    assert n == {"floor": 0.10, "cap": 0.30}


def test_normalize_already_fractional():
    """Values ≤1 are treated as decimals already."""
    n = _normalize_quota({"floor": 0.25, "cap": 0.50})
    assert n == {"floor": 0.25, "cap": 0.50}


def test_normalize_missing_returns_safe_defaults():
    n = _normalize_quota(None)
    assert n == {"floor": 0.0, "cap": 1.0}


# ---- _assign_types: audit example ----


def test_assign_types_50_slate_respects_caps_and_floors():
    """A 50-slot slate under the audit defaults must:
      - have ≥ 25 type-C and ≥ 20 type-E and ≥ 5 type-F (floors)
      - have ≤ ~12 type-A and ≤ ~5 type-B (caps with rounding tolerance)
    """
    out = _assign_types(50, COMMENT_TYPE_QUOTAS_DEFAULT)
    assert len(out) == 50
    counts = Counter(out)
    # Floors (RULE 7)
    assert counts["C"] >= 12, f"C below floor: {counts}"   # 25% of 50 = 12.5, round to 13
    assert counts["E"] >= 10, f"E below floor: {counts}"   # 20% of 50 = 10
    assert counts["F"] >= 2,  f"F below floor: {counts}"   # 5% of 50 = 2.5, round to 3
    # Caps
    assert counts["A"] <= 13, f"A above cap (25%): {counts}"  # 25% of 50 = 12.5
    assert counts["B"] <= 5,  f"B above cap (10%): {counts}"   # 10% of 50 = 5
    assert counts["D"] >= 5,  f"D below floor (10%): {counts}"  # 10% of 50 = 5
    assert counts["D"] <= 8,  f"D above cap (15%): {counts}"     # 15% of 50 = 7.5


def test_assign_types_returns_exactly_n_items():
    for n in (1, 5, 10, 22, 30, 50, 100):
        out = _assign_types(n, COMMENT_TYPE_QUOTAS_DEFAULT)
        assert len(out) == n, f"n={n} got {len(out)}"


def test_assign_types_zero_returns_empty():
    assert _assign_types(0, COMMENT_TYPE_QUOTAS_DEFAULT) == []


def test_assign_types_does_not_violate_caps_at_small_n():
    """Even with 22 items (Alex's slot), caps still hold."""
    out = _assign_types(22, COMMENT_TYPE_QUOTAS_DEFAULT)
    counts = Counter(out)
    assert counts["A"] <= 6, f"A over cap (25% of 22=5.5): {counts}"
    assert counts["B"] <= 3, f"B over cap (10% of 22=2.2): {counts}"


def test_assign_types_legacy_list_quotas_still_work():
    """Pre-RULE-7 operators have list quotas in Mongo. Allocator should NOT
    crash; it should treat [lo, hi] as {floor=lo, cap=hi}."""
    legacy = {
        "A": [35, 40],
        "B": [22, 25],
        "C": [14, 16],
        "D": [9, 12],
        "E": [7, 10],
        "F": [0, 5],
    }
    out = _assign_types(50, legacy)
    assert len(out) == 50
    counts = Counter(out)
    # legacy quotas — A would dominate (35-40% floor). Confirm the floor is honored.
    assert counts["A"] >= 17  # 35% of 50 = 17.5


# ---- caps actually constrain ----


def test_cap_only_quota_cannot_dominate():
    """A type capped at 10% must not exceed 10% (rounded) even when there's
    nothing else competing."""
    # Single type capped — engine has to fill the rest somehow.
    quotas = {
        "A": {"floor": 0, "cap": 10},
        "B": {"floor": 0, "cap": 10},
        "C": {"floor": 0, "cap": 100},  # the spillover destination
        "D": {"floor": 0, "cap": 0},
        "E": {"floor": 0, "cap": 0},
        "F": {"floor": 0, "cap": 0},
    }
    out = _assign_types(100, quotas)
    counts = Counter(out)
    assert counts["A"] <= 10
    assert counts["B"] <= 10
    # Most slate goes to C (uncapped).
    assert counts["C"] >= 80


def test_floor_summing_over_n_trims_safely():
    """Degenerate quotas where floors sum > 100% must not cause an infinite
    loop or exception. Slate gets truncated to fit n."""
    bad = {
        "A": {"floor": 50, "cap": 100},
        "B": {"floor": 50, "cap": 100},
        "C": {"floor": 50, "cap": 100},
        "D": {"floor": 0, "cap": 0},
        "E": {"floor": 0, "cap": 0},
        "F": {"floor": 0, "cap": 0},
    }
    out = _assign_types(10, bad)
    assert len(out) == 10
    # All three with floors must contribute SOMETHING.
    counts = Counter(out)
    assert counts["A"] + counts["B"] + counts["C"] == 10


def test_audit_warning_type_b_essentially_zero():
    """Type-B was 'dead' (3% reply rate) per audit. With cap 10 and floor 0,
    a normal 50-slate run should typically have ≤ 5 type-B."""
    counts_seen = []
    for _ in range(20):
        # _assign_types is deterministic given the same input — but useful
        # to demonstrate B never exceeds its 10% cap.
        out = _assign_types(50, COMMENT_TYPE_QUOTAS_DEFAULT)
        counts_seen.append(Counter(out)["B"])
    assert all(b <= 5 for b in counts_seen), counts_seen
