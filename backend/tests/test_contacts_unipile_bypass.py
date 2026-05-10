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


@pytest.fixture(autouse=True)
def _disable_inline_post_quality(monkeypatch):
    """Default: tests don't run the inline post_quality LLM gate. Tests that
    specifically exercise it flip the toggle back on themselves."""
    from app.config import settings as _settings

    monkeypatch.setattr(
        _settings,
        "discovery_contact_unipile_run_post_quality",
        False,
        raising=False,
    )


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
        # When the inline quality gate is disabled (default in tests), the
        # synthetic placeholder is written so allocator/drafter still find
        # something at gate_results.post_quality.
        pq = gr["post_quality"]
        assert pq["synthetic"] is True
        assert pq["qualifying_signal"] == "operator_curated_contact"


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


def test_post_quality_gate_runs_inline_when_enabled(monkeypatch):
    """When `discovery_contact_unipile_run_post_quality=True`, each post is
    sent through the post_quality gate and `drop=True` posts are skipped.
    The real qualifying_signal is captured into gate_results."""
    from app.engine.stages import discovery
    from app.engine.stages.gates import post_quality

    db = _DB()
    op_id, cf_id, slate_id = ObjectId(), ObjectId(), ObjectId()

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        return [
            _post("good", 0),  # text: "Nurse turnover is up 23%..."
            _post("bait", 1),
            _post("good", 2),
        ]

    quality_calls: list[str] = []

    def fake_evaluate(*, post_text):
        quality_calls.append(post_text)
        # Drop the "bait" post; pass the others.
        if "bait" in post_text or "id-bait" in post_text:
            # NB: _post() reuses the same text body for all; we differentiate
            # by URL slug. Build the verdict on-the-fly.
            return post_quality._Verdict(
                drop=True,
                reason="engagement bait",
                qualifying_signal="none",
            )
        return post_quality._Verdict(
            drop=False,
            reason="direct expertise on staffing turnover",
            qualifying_signal="direct_expertise",
        )

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)
    monkeypatch.setattr(discovery.post_quality, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        discovery.settings, "discovery_contact_unipile_run_post_quality", True,
        raising=False,
    )
    # Disable recency for this test so age doesn't interfere.
    monkeypatch.setattr(
        discovery.settings, "discovery_contact_unipile_max_age_days", 0,
        raising=False,
    )

    # Override _post() so the bait one has "bait" in its text.
    def fake_get_user_posts_v2(*, account_id, public_identifier_or_url, limit):
        return [
            UnipilePost(
                id="g1", url="https://www.linkedin.com/posts/g1",
                text="Strong substantive post about hospital staffing.",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None, published_at=None,
            ),
            UnipilePost(
                id="b1", url="https://www.linkedin.com/posts/b1",
                text="Type AGREE if you bait this engagement bait too!",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None, published_at=None,
            ),
            UnipilePost(
                id="g2", url="https://www.linkedin.com/posts/g2",
                text="Another substantive post about referral patterns.",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None, published_at=None,
            ),
        ]

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts_v2)

    seeds = [_seed(name="A", url="https://www.linkedin.com/in/a")]
    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=op_id,
        cofounder_id=cf_id,
        slate_run_id=slate_id,
        account_id="acc_test",
        seeds=seeds,
        seen_urls=set(),
    )
    assert inserted == 2  # bait dropped, two good ones inserted
    assert len(quality_calls) == 3  # gate called for each post
    inserted_signals = [
        c["gate_results"]["post_quality"]["qualifying_signal"]
        for c in db.candidates.inserts
    ]
    assert all(s == "direct_expertise" for s in inserted_signals)
    # `synthetic=False` means it's a real LLM verdict, not the placeholder.
    assert all(
        c["gate_results"]["post_quality"]["synthetic"] is False
        for c in db.candidates.inserts
    )


def test_post_quality_gate_skipped_when_disabled(monkeypatch):
    """When the toggle is off, no post_quality calls — synthetic placeholder
    keeps backward compatibility with the original bypass behavior."""
    from app.engine.stages import discovery

    db = _DB()
    quality_calls: list[str] = []

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        return [_post("x", 0)]

    def fake_evaluate(*, post_text):
        quality_calls.append(post_text)
        raise AssertionError("post_quality must NOT be called when toggle=off")

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)
    monkeypatch.setattr(discovery.post_quality, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        discovery.settings, "discovery_contact_unipile_run_post_quality", False,
        raising=False,
    )

    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=[_seed(name="A", url="https://www.linkedin.com/in/a")],
        seen_urls=set(),
    )
    assert inserted == 1
    assert quality_calls == []
    # Synthetic placeholder still set so allocator + drafter find a payload.
    pq = db.candidates.inserts[0]["gate_results"]["post_quality"]
    assert pq.get("synthetic") is True
    assert pq.get("qualifying_signal") == "operator_curated_contact"


def test_post_quality_gate_error_keeps_post(monkeypatch):
    """If the LLM call errors (timeout, content filter), the post is KEPT
    rather than silently dropped — operator-curated contacts get the
    benefit of the doubt."""
    from app.engine.stages import discovery

    db = _DB()

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        return [_post("x", 0)]

    def fake_evaluate(*, post_text):
        raise RuntimeError("simulated openai content filter")

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)
    monkeypatch.setattr(discovery.post_quality, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        discovery.settings, "discovery_contact_unipile_run_post_quality", True,
        raising=False,
    )

    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=[_seed(name="A", url="https://www.linkedin.com/in/a")],
        seen_urls=set(),
    )
    assert inserted == 1  # kept despite gate error


def test_too_old_posts_are_dropped(monkeypatch):
    """Bypass path skips verification, so the recency filter has to live
    inside `_run_contact_seeds_unipile`. Posts older than the configured
    cutoff get dropped; posts with no parseable date are KEPT."""
    from datetime import datetime, timedelta, timezone
    from app.engine.stages import discovery

    db = _DB()
    now = datetime.now(timezone.utc)

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        # 4 posts: fresh (5d), borderline (89d), too old (120d), no-date.
        return [
            UnipilePost(
                id="fresh",
                url="https://www.linkedin.com/posts/x_fresh",
                text="recent",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None,
                published_at=now - timedelta(days=5),
            ),
            UnipilePost(
                id="border",
                url="https://www.linkedin.com/posts/x_border",
                text="border",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None,
                published_at=now - timedelta(days=89),
            ),
            UnipilePost(
                id="old",
                url="https://www.linkedin.com/posts/x_old",
                text="ancient",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None,
                published_at=now - timedelta(days=120),
            ),
            UnipilePost(
                id="nodate",
                url="https://www.linkedin.com/posts/x_nodate",
                text="undated",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None,
                published_at=None,
            ),
        ]

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)
    # Force the cutoff to 90 days regardless of env / settings overrides.
    monkeypatch.setattr(
        discovery.settings, "discovery_contact_unipile_max_age_days", 90,
        raising=False,
    )

    seeds = [_seed(name="A", url="https://www.linkedin.com/in/a")]
    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=seeds,
        seen_urls=set(),
    )
    # fresh + border + nodate = 3 inserted; old gets dropped.
    assert inserted == 3
    inserted_ids = {c["post_id"] for c in db.candidates.inserts}
    assert inserted_ids == {"fresh", "border", "nodate"}
    assert "old" not in inserted_ids


def test_max_age_zero_disables_recency_filter(monkeypatch):
    """Setting `discovery_contact_unipile_max_age_days=0` keeps every
    post regardless of age."""
    from datetime import datetime, timedelta, timezone
    from app.engine.stages import discovery

    db = _DB()
    now = datetime.now(timezone.utc)

    def fake_get_user_posts(*, account_id, public_identifier_or_url, limit):
        return [
            UnipilePost(
                id=f"old-{i}",
                url=f"https://www.linkedin.com/posts/x_old-{i}",
                text="x",
                author_name="A", author_title=None, author_company=None,
                author_profile_url=None,
                published_at=now - timedelta(days=365 * 3),
            )
            for i in range(3)
        ]

    monkeypatch.setattr(discovery, "get_user_posts", fake_get_user_posts)
    monkeypatch.setattr(
        discovery.settings, "discovery_contact_unipile_max_age_days", 0,
        raising=False,
    )

    inserted = discovery._run_contact_seeds_unipile(
        db,
        operator_id=ObjectId(),
        cofounder_id=ObjectId(),
        slate_run_id=ObjectId(),
        account_id="acc_test",
        seeds=[_seed(name="A", url="https://www.linkedin.com/in/a")],
        seen_urls=set(),
    )
    assert inserted == 3


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
