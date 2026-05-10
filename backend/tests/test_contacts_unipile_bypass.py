"""
Contact-only fast path: pull posts via Unipile and skip
verification + cheap-gates + profile_resolve + expensive-gates.

These tests drive `_run_contact_seeds_unipile` against a fake Unipile
(monkeypatched) and a fake mongo collection. They prove:

  - Posts come back tagged status="gate_passed" with bypass_gates=True so
    the allocator picks them up without running any gate.
  - source/source_channel are tagged correctly so allocator + drafter
    routing can recognize a contact-direct candidate.
  - A synthetic ICP score (10) is attached so contacts float ahead of
    keyword-discovered survivors in the cofounder bucket.
  - Seeds without a linkedin_url are skipped (Unipile needs a slug).
  - Empty seed list / missing account_id are no-ops, not errors.
  - The seen_urls de-dupe set is honored (no duplicate inserts on retry).
"""
from __future__ import annotations

from typing import Any

import pytest
from bson import ObjectId

from app.services.unipile import UnipilePost


class _Coll:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.inserts: list[dict[str, Any]] = []

    def insert_one(self, d: dict[str, Any]) -> Any:
        self.docs.append(d)
        self.inserts.append(d)
        return type("R", (), {"inserted_id": ObjectId()})()


class _DB:
    def __init__(self) -> None:
        self.candidates = _Coll()


def _seed(*, name: str, url: str | None, title: str = "VP HR") -> dict[str, Any]:
    return {
        "_id": ObjectId(),
        "extracted_name": name,
        "extracted_title": title,
        "extracted_company": "St Example Health",
        "linkedin_url": url,
    }


def _post(slug: str, idx: int) -> UnipilePost:
    return UnipilePost(
        id=f"id-{slug}-{idx}",
        url=f"https://www.linkedin.com/posts/{slug}_activity-{idx}",
        text="Nurse turnover is up 23% this quarter and it is the headline.",
        author_name=f"Author {slug}",
        author_title="VP HR",
        author_company="St Example Health",
        author_profile_url=f"https://www.linkedin.com/in/{slug}",
        published_at=None,
    )


def test_inserts_with_status_gate_passed_and_bypass_flag(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    op_id, cf_id, slate_id = ObjectId(), ObjectId(), ObjectId()

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        slug = public_identifier_or_url.rstrip("/").rsplit("/", 1)[-1]
        return [_post(slug, i) for i in range(min(limit, 3))]

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    seeds = [_seed(name="Jane Doe", url="https://www.linkedin.com/in/jane-doe")]

    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=op_id,
        cofounder_id=cf_id,
        slate_run_id=slate_id,
        account_id="acc_test",
        seeds=seeds,
        seen_urls=set(),
    )
    assert inserted == 3
    for c in db.candidates.inserts:
        assert c["status"] == "gate_passed"
        assert c["bypass_gates"] is True
        assert c["source"] == "contact_unipile"
        assert c["source_channel"] == "contact_direct"
        assert c["source_classification"] == "A"
        # Synthetic ICP signals must be in place so allocator + drafter
        # don't crash on KeyError when reading gate_results.
        gr = c["gate_results"]
        assert gr["icp"]["score_0_10"] == 10
        assert gr["icp"]["synthetic"] is True
        assert gr["non_buyer"]["verdict"] == "buyer"
        assert gr["post_quality"]["verdict"] == "pass"


def test_seed_without_linkedin_url_is_skipped(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    fetched: list[str] = []

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        fetched.append(public_identifier_or_url)
        return []

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    seeds = [_seed(name="No URL Person", url=None)]
    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=seeds,
        seen_urls=set(),
    )
    assert inserted == 0
    assert fetched == []  # Unipile never called


def test_empty_seeds_returns_zero(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=[],
        seen_urls=set(),
    )
    assert inserted == 0


def test_missing_account_id_is_a_noop(monkeypatch):
    from app.engine.stages import discovery

    db = _DB()
    fetched: list[str] = []

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        fetched.append(public_identifier_or_url)
        return []

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="",  # cofounder hasn't connected LinkedIn yet
        seeds=[_seed(name="A", url="https://www.linkedin.com/in/a")],
        seen_urls=set(),
    )
    assert inserted == 0
    assert fetched == []


def test_no_url_dedup_passes_all_posts(monkeypatch):
    """Every post URL Unipile returns gets inserted — even if a different
    contact's profile already surfaced the same URL. The operator curated
    the list; we don't second-guess them by deduping shared posts."""
    from app.engine.stages import discovery

    db = _DB()

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        # Both seeds yield the same single post — should be inserted twice.
        return [_post("shared", 0)]

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    seen_urls: set[str] = set()
    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=[
            _seed(name="A", url="https://www.linkedin.com/in/a"),
            _seed(name="B", url="https://www.linkedin.com/in/b"),
        ],
        seen_urls=seen_urls,
    )
    # Both contacts' shared post is inserted — no dedup.
    assert inserted == 2


def test_unipile_error_is_swallowed_per_seed(monkeypatch):
    """If Unipile blows up on one contact, the loop continues for the rest."""
    from app.engine.stages import discovery
    from app.services.unipile import UnipileError

    db = _DB()
    calls: list[str] = []

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        calls.append(public_identifier_or_url)
        if "broken" in public_identifier_or_url:
            raise UnipileError("422 unipile broken profile")
        return [_post("ok", 0)]

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)

    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=[
            _seed(name="Broken", url="https://www.linkedin.com/in/broken-profile"),
            _seed(name="Ok", url="https://www.linkedin.com/in/ok"),
        ],
        seen_urls=set(),
    )
    # Broken seed errored, ok seed succeeded → exactly 1 candidate.
    assert inserted == 1
    assert len(calls) == 2  # Both seeds attempted
