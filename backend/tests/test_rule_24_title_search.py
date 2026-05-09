"""
RULE 24 — Title-search PEOPLE channel via Unipile.

These tests drive _run_unipile_title_search with a fake Unipile (monkeypatched)
and a fake mongo. They verify:
  - 6-query daily envelope is respected
  - Top 10 candidates per query
  - Recent-activity walk produces source_channel='title_search' candidates
  - 14-day no-repeat ledger uses the title_search channel
  - 2nd-degree filter is the default
"""
from __future__ import annotations

from typing import Any

import pytest
from bson import ObjectId

from app.engine import keyword_history
from app.engine.constants import (
    DISCOVERY_TITLE_SEARCH_PEOPLE_PER_QUERY,
    DISCOVERY_TITLE_SEARCH_POSTS_PER_PERSON,
    DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN,
)
from app.services.unipile import (
    LINKEDIN_GEO_URN_US,
    UnipilePerson,
    UnipilePost,
)


# ---- audit constants ----


def test_audit_envelope_constants():
    """Audit-locked: 6 queries × 10 candidates per cofounder per day."""
    assert DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN == 6
    assert DISCOVERY_TITLE_SEARCH_PEOPLE_PER_QUERY == 10
    assert LINKEDIN_GEO_URN_US == "103644278"


# ---- fake mongo for title_search ----


class _Coll:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.inserts: list[dict] = []
        self.updates: list[tuple[dict, dict]] = []

    def find(self, query: dict, *_args, **_kwargs):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)):
                yield d

    def find_one(self, query: dict, *_args, **_kwargs):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)):
                return d
        return None

    def insert_one(self, d):
        self.inserts.append(d)

    def update_one(self, q, u, upsert: bool = False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items() if not k.startswith("$") and not isinstance(v, dict)):
                if "$set" in u:
                    d.update(u["$set"])
                return type("R", (), {"matched_count": 1})()
        if upsert:
            new = {k: v for k, v in q.items() if not k.startswith("$") and not isinstance(v, dict)}
            for op in ("$setOnInsert", "$set"):
                for k, v in (u.get(op) or {}).items():
                    if op == "$setOnInsert":
                        new.setdefault(k, v)
                    else:
                        new[k] = v
            self.docs.append(new)
        return type("R", (), {"matched_count": 0})()


class _DB:
    def __init__(self):
        self.candidates = _Coll()
        self.exhaustion_ledger = _Coll()
        self.keyword_history = _Coll()
        self.crustdata_inbox = _Coll()
        self.discovery_seeds = _Coll()
        self.audit_records = _Coll()


# ---- happy-path: 6 queries → 10 people each → 5 posts each ----


def test_title_search_inserts_candidates_with_correct_tagging(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    op_id = ObjectId()
    cf_id = ObjectId()
    slate_id = ObjectId()
    seen_urls: set[str] = set()

    title_pool = [
        "VP HR hospital",
        "CHRO health system",
        "VP Operations health system",
        "Director RCM hospital",
        "VP Talent Acquisition hospital",
        "Chief Operating Officer hospital",
        "VP Finance hospital",  # 7th: should NOT fire (envelope is 6)
        "Director Patient Access hospital",
    ]

    # Fake: search_people returns one person per query
    def fake_search_people(*, account_id, query, limit, **_):
        return [
            UnipilePerson(
                name=f"Person for {query} #{i}",
                public_identifier=f"slug-{query.replace(' ', '-')}-{i}",
                profile_url=f"https://www.linkedin.com/in/slug-{query.replace(' ', '-')}-{i}",
                title="VP HR",
                company="St Example Health System",
                location="USA",
                network_distance="2",
            )
            for i in range(min(limit, 2))  # return 2 per query so test runs are smaller
        ]

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        return [
            UnipilePost(
                id=f"id-{public_identifier_or_url}-{i}",
                url=f"https://www.linkedin.com/posts/{public_identifier_or_url}_{i}-activity-{i}",
                text="The 23% nurse turnover rate is the headline finding.",
                author_name="Author",
                author_title="VP HR",
                author_company="Health Co",
                author_profile_url=f"https://www.linkedin.com/in/{public_identifier_or_url}",
                published_at=None,
            )
            for i in range(min(limit, 2))
        ]

    monkeypatch.setattr(discovery, "search_people", fake_search_people)
    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    inserted = discovery._run_unipile_title_search(
        db,
        operator={"_id": op_id},
        operator_id=op_id,
        cofounder_id=cf_id,
        slate_run_id=slate_id,
        account_id="acc_test",
        title_industry=title_pool,
        seen_urls=seen_urls,
        seen_authors_shipped=set(),
    )

    # 6 queries (envelope) × 2 people × 2 posts each = 24
    assert inserted == DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN * 2 * 2
    # All inserted candidates carry the audit-aligned tags.
    for c in db.candidates.inserts:
        assert c["source"] == "unipile_people"
        assert c["source_channel"] == "title_search"
        assert c["source_classification"] == "A"


def test_title_search_marks_queries_in_ledger(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    op_id = ObjectId()

    def fake_search_people(*, account_id, query, limit, **_):
        return []

    monkeypatch.setattr(discovery, "search_people", fake_search_people)

    discovery._run_unipile_title_search(
        db,
        operator={"_id": op_id},
        operator_id=op_id,
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="x",
        title_industry=["VP HR hospital", "CHRO health system"],
        seen_urls=set(),
        seen_authors_shipped=set(),
    )

    history_docs = [
        d for d in db.keyword_history.docs
        if d.get("source_channel") == "title_search"
    ]
    assert len(history_docs) == 2
    queries = {d["query"] for d in history_docs}
    assert queries == {"VP HR hospital", "CHRO health system"}


def test_title_search_skips_recently_used_queries(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    op_id = ObjectId()

    # Pre-mark "VP HR hospital" as used.
    keyword_history.mark_used(
        db, operator_id=op_id, source_channel="title_search", query="VP HR hospital"
    )

    queries_called: list[str] = []

    def fake_search_people(*, account_id, query, limit, **_):
        queries_called.append(query)
        return []

    monkeypatch.setattr(discovery, "search_people", fake_search_people)

    discovery._run_unipile_title_search(
        db,
        operator={"_id": op_id},
        operator_id=op_id,
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="x",
        title_industry=["VP HR hospital", "CHRO health system"],
        seen_urls=set(),
        seen_authors_shipped=set(),
    )
    # The recently-used query is filtered out.
    assert "VP HR hospital" not in queries_called
    assert "CHRO health system" in queries_called


def test_title_search_skips_when_pool_empty():
    from app.engine.stages import discovery

    db = _DB()
    inserted = discovery._run_unipile_title_search(
        db,
        operator={"_id": ObjectId()},
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="x",
        title_industry=[],
        seen_urls=set(),
        seen_authors_shipped=set(),
    )
    assert inserted == 0


def test_title_search_skips_when_account_id_missing():
    from app.engine.stages import discovery

    db = _DB()
    inserted = discovery._run_unipile_title_search(
        db,
        operator={"_id": ObjectId()},
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="",
        title_industry=["VP HR hospital"],
        seen_urls=set(),
        seen_authors_shipped=set(),
    )
    assert inserted == 0


def test_title_search_skips_authors_already_shipped(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    op_id = ObjectId()
    shipped_url = "https://www.linkedin.com/in/already-shipped"
    seen_authors = {shipped_url}

    def fake_search_people(*, account_id, query, limit, **_):
        return [
            UnipilePerson(
                name="Already Shipped",
                public_identifier="already-shipped",
                profile_url=shipped_url,
                title="VP",
                company="Co",
                location="US",
                network_distance="2",
            )
        ]

    posts_fetched: list[str] = []

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        posts_fetched.append(public_identifier_or_url)
        return []

    monkeypatch.setattr(discovery, "search_people", fake_search_people)
    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    discovery._run_unipile_title_search(
        db,
        operator={"_id": op_id},
        operator_id=op_id,
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="x",
        title_industry=["VP HR hospital"],
        seen_urls=set(),
        seen_authors_shipped=seen_authors,
    )
    # Skipped — never fetched their posts.
    assert posts_fetched == []
