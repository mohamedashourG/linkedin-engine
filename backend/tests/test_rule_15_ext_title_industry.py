"""
RULE 15-EXT — title-plus-industry queries through the LinkedIn content
search (Unipile path).

Tests:
  - Per-client config provides the title_industry pool
  - keyword_history filters on a separate channel from topical
  - DISCOVERY_TITLE_INDUSTRY_PER_RUN is the audit-locked 4
"""
from __future__ import annotations

from bson import ObjectId

from app.engine import keyword_history
from app.engine.constants import DISCOVERY_TITLE_INDUSTRY_PER_RUN
from app.services import client_config


# ---- pool sourced from per-client config ----


def test_glnk_keyword_pool_has_title_industry():
    cfg = client_config.load("glnk")
    pool = cfg.keyword_pools.get("title_industry") or []
    assert len(pool) >= 14, "RULE 15-EXT calls for a 14-query pool"
    assert all(isinstance(q, str) and q.strip() for q in pool)


def test_edge_keyword_pool_has_title_industry():
    cfg = client_config.load("edge")
    pool = cfg.keyword_pools.get("title_industry") or []
    # Edge's pool can be smaller than 14 (it's a smaller market) but must
    # have at least 6 so a 4-per-day rotation can run for 1+ weeks.
    assert len(pool) >= 6
    # Spot-check that the pool's vocabulary is health-system-side (not pharma).
    joined = " ".join(pool).lower()
    assert "hospital" in joined or "health system" in joined


def test_taiga_keyword_pool_has_title_industry():
    cfg = client_config.load("taiga")
    pool = cfg.keyword_pools.get("title_industry") or []
    assert len(pool) >= 6
    joined = " ".join(pool).lower()
    assert "physician" in joined or "practice" in joined


# ---- 14-day ledger keeps topical and title_industry separate ----


def test_topical_use_does_not_block_title_industry_use():
    """The ledger must scope to channel. A query in BOTH pools tracks
    independently."""
    from tests.test_rule_15_keyword_history import _DB
    db = _DB()
    op = ObjectId()
    keyword_history.mark_used(
        db, operator_id=op, source_channel="keyword_topical", query="VP Sales biotech"
    )
    out = keyword_history.filter_unused(
        db,
        operator_id=op,
        source_channel="keyword_title_industry",
        queries=["VP Sales biotech"],
    )
    assert out == ["VP Sales biotech"]


def test_title_industry_use_then_filter_excludes_same_channel():
    from tests.test_rule_15_keyword_history import _DB
    db = _DB()
    op = ObjectId()
    keyword_history.mark_used(
        db,
        operator_id=op,
        source_channel="keyword_title_industry",
        query="CHRO health system",
    )
    out = keyword_history.filter_unused(
        db,
        operator_id=op,
        source_channel="keyword_title_industry",
        queries=["CHRO health system", "VP Operations hospital"],
    )
    assert out == ["VP Operations hospital"]


# ---- audit-locked daily mix value ----


def test_title_industry_per_run_is_4():
    """The audit explicitly locks the daily mix at 4 topical + 4
    title-industry. The constant is the single source of truth — if it
    drifts, this test catches it."""
    assert DISCOVERY_TITLE_INDUSTRY_PER_RUN == 4
