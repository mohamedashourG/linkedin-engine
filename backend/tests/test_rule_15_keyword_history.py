"""
RULE 15 — keyword content search with 14-day no-repeat ledger.

Tests filter_unused / mark_used in app.engine.keyword_history, plus the
candidate-doc tagging via source_channel="keyword_topical" in
discovery._candidate_doc / _doc_from_apidirect / _doc_from_exa.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId

from app.engine import keyword_history
from app.engine.stages.discovery import _candidate_doc


# ---- in-memory fake of keyword_history collection ----


class _Coll:
    def __init__(self):
        self.docs: list[dict] = []

    def find(self, query: dict, projection: dict | None = None):
        for d in self.docs:
            if _matches(d, query):
                yield d

    def update_one(self, query: dict, update: dict, upsert: bool = False):
        for d in self.docs:
            if _matches(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                return type("R", (), {"matched_count": 1})()
        if upsert:
            new = {}
            for k, v in query.items():
                if not k.startswith("$"):
                    new[k] = v
            for op_key in ("$setOnInsert", "$set"):
                for k, v in (update.get(op_key) or {}).items():
                    new.setdefault(k, v) if op_key == "$setOnInsert" else new.update({k: v})
            self.docs.append(new)
        return type("R", (), {"matched_count": 0})()


def _matches(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if k.startswith("$"):
            continue
        if isinstance(v, dict):
            # support {$gte: dt}, {$in: [...]}
            for op, expected in v.items():
                actual = doc.get(k)
                if op == "$gte":
                    if actual is None or actual < expected:
                        return False
                elif op == "$in":
                    if actual not in expected:
                        return False
                else:
                    return False
        else:
            if doc.get(k) != v:
                return False
    return True


class _DB:
    def __init__(self):
        self.keyword_history = _Coll()


# ---- filter_unused / mark_used ----


def test_filter_unused_returns_all_when_history_empty():
    db = _DB()
    op = ObjectId()
    queries = ["pharma launch readiness", "VP Sales biotech", "MSL strategy"]
    out = keyword_history.filter_unused(
        db, operator_id=op, source_channel="keyword_topical", queries=queries
    )
    assert out == queries


def test_mark_used_then_filter_excludes():
    db = _DB()
    op = ObjectId()
    keyword_history.mark_used(
        db, operator_id=op, source_channel="keyword_topical", query="pharma launch readiness"
    )
    out = keyword_history.filter_unused(
        db,
        operator_id=op,
        source_channel="keyword_topical",
        queries=["pharma launch readiness", "VP Sales biotech"],
    )
    assert out == ["VP Sales biotech"]


def test_mark_used_for_different_channel_does_not_block():
    """A query used on the keyword_topical channel must not block the same
    query on the title_search channel."""
    db = _DB()
    op = ObjectId()
    keyword_history.mark_used(
        db, operator_id=op, source_channel="keyword_topical", query="VP Sales biotech"
    )
    out = keyword_history.filter_unused(
        db,
        operator_id=op,
        source_channel="title_search",
        queries=["VP Sales biotech"],
    )
    assert out == ["VP Sales biotech"]


def test_mark_used_for_different_operator_does_not_block():
    db = _DB()
    op_a, op_b = ObjectId(), ObjectId()
    keyword_history.mark_used(
        db, operator_id=op_a, source_channel="keyword_topical", query="pharma launch"
    )
    out = keyword_history.filter_unused(
        db, operator_id=op_b, source_channel="keyword_topical", queries=["pharma launch"]
    )
    assert out == ["pharma launch"]


def test_filter_with_old_history_doesnt_block():
    """Entries older than the lookback window should not block a query."""
    db = _DB()
    op = ObjectId()
    # Manually insert a stale row (15 days ago).
    stale = datetime.now(timezone.utc) - timedelta(days=15)
    db.keyword_history.docs.append(
        {
            "operator_id": op,
            "source_channel": "keyword_topical",
            "query": "old query",
            "last_used_at": stale,
        }
    )
    out = keyword_history.filter_unused(
        db, operator_id=op, source_channel="keyword_topical", queries=["old query"]
    )
    assert out == ["old query"]


def test_filter_preserves_input_order():
    db = _DB()
    op = ObjectId()
    queries = ["a", "b", "c", "d"]
    keyword_history.mark_used(
        db, operator_id=op, source_channel="keyword_topical", query="b"
    )
    out = keyword_history.filter_unused(
        db, operator_id=op, source_channel="keyword_topical", queries=queries
    )
    assert out == ["a", "c", "d"]


def test_filter_skips_blank_queries():
    db = _DB()
    op = ObjectId()
    out = keyword_history.filter_unused(
        db, operator_id=op, source_channel="keyword_topical", queries=["", "real query", None]
    )
    assert out == ["real query"]


def test_mark_used_is_idempotent():
    db = _DB()
    op = ObjectId()
    keyword_history.mark_used(
        db, operator_id=op, source_channel="keyword_topical", query="x"
    )
    keyword_history.mark_used(
        db, operator_id=op, source_channel="keyword_topical", query="x"
    )
    # Second call should update, not insert a duplicate.
    matching = [d for d in db.keyword_history.docs if d.get("query") == "x"]
    assert len(matching) == 1


# ---- candidate doc tagging ----


def test_candidate_doc_carries_source_channel():
    op, cf, slate = ObjectId(), ObjectId(), ObjectId()
    doc = _candidate_doc(
        operator_id=op,
        cofounder_id=cf,
        slate_run_id=slate,
        post_url="https://www.linkedin.com/posts/x_y-activity-1",
        post_id=None,
        author_name="Jane",
        author_title="VP",
        author_company="Co",
        author_linkedin_url="https://www.linkedin.com/in/jane",
        post_text="...",
        post_published_at=None,
        source="apidirect_kw",
        source_keyword="pharma launch readiness",
        source_classification="A",
        source_channel="keyword_topical",
    )
    assert doc["source_channel"] == "keyword_topical"
    # Vendor `source` and audit `source_channel` are independent.
    assert doc["source"] == "apidirect_kw"


def test_candidate_doc_default_source_channel_is_blank():
    """Sources that aren't keyword content search (manual seeds, harvester,
    crustdata inbox) should not carry a source_channel by default — keeping
    the field empty signals 'unclassified' to downstream routers."""
    op, cf, slate = ObjectId(), ObjectId(), ObjectId()
    doc = _candidate_doc(
        operator_id=op,
        cofounder_id=cf,
        slate_run_id=slate,
        post_url="x",
        post_id=None,
        author_name=None,
        author_title=None,
        author_company=None,
        author_linkedin_url=None,
        post_text="",
        post_published_at=None,
        source="manual_seed",
        source_keyword="",
        source_classification="B",
    )
    assert doc["source_channel"] == ""
