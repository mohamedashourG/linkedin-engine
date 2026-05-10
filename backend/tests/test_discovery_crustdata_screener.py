"""Crustdata screener path in discovery."""

from __future__ import annotations

from bson import ObjectId

from app.engine.stages import discovery


def _fake_db_track_candidates():
    inserted: list[dict] = []

    class _FakeCursor:
        def sort(self, *_a, **_k):
            return self

        def __iter__(self):
            return iter([])

    class _FakeColl:
        def find(self, *_a, **_kw):
            return _FakeCursor()

        def insert_one(self, d):
            inserted.append(d)
            class _R:
                inserted_id = ObjectId()

            return _R()

        def update_many(self, *_a, **_kw):
            class _R:
                modified_count = 0

            return _R()

        def aggregate(self, *_a, **_kw):
            return iter([])

        def count_documents(self, *_a, **_kw):
            return 0

        def find_one(self, *_a, **_kw):
            return None

    class _FakeDB:
        def __init__(self):
            self.discovery_seeds = _FakeColl()
            self.candidates = _FakeColl()
            self.audit_records = _FakeColl()
            self.keyword_history = _FakeColl()
            self.exhaustion_ledger = _FakeColl()
            self.crustdata_inbox = _FakeColl()
            self.profiles = _FakeColl()

    db = _FakeDB()
    return db, inserted


def test_crustdata_screener_inserts_candidates(monkeypatch):
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = {
        "_id": op_id,
        "product_description": "x",
        "product_extracted": {
            "suggested_keywords": {"tier_1": ["alpha"], "tier_2": ["beta"]},
        },
    }
    cofounder = {
        "_id": cf_id,
        "display_name": "c",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {
            "tone_description": "t" * 30,
            "examples": [{"post": "p", "comment": "c"}],
        },
    }

    call_n = [0]

    def fake_screener(*, keyword, **_kw):
        call_n[0] += 1
        return [
            {
                "share_url": f"https://www.linkedin.com/posts/example_post_{call_n[0]}",
                "uid": f"u{call_n[0]}",
                "actor_name": "Test Author",
                "text": f"hello {keyword[:20]}",
                "date_posted": "2026-05-01",
                "person_details": {
                    "person_linkedin_flagship_profile_url": "https://www.linkedin.com/in/test-author",
                    "current_employers": [
                        {"employer_name": "Acme", "employee_title": "CEO"},
                    ],
                },
            }
        ]

    monkeypatch.setattr(discovery, "screener_keyword_search_posts", fake_screener)
    monkeypatch.setattr(discovery, "_run_apidirect", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_exa", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_unipile", lambda **_k: 0)
    monkeypatch.setattr(
        discovery,
        "settings",
        discovery.settings.model_copy(
            update={
                "discovery_use_crustdata": True,
                "discovery_use_crustdata_screener": True,
                "discovery_crustdata_screener_max_keyword_calls": 3,
                "discovery_crustdata_screener_limit_per_keyword": 5,
                "crustdata_api_key": "k",
                "discovery_use_apidirect": False,
                "discovery_use_exa": False,
                "discovery_use_unipile": False,
                "discovery_crustdata_simulation_ping_on_empty_inbox": False,
            }
        ),
    )

    db, docs = _fake_db_track_candidates()
    n = discovery.discover_for_operator(
        db,
        operator=operator,
        cofounders=[cofounder],
        slate_run_id=ObjectId(),
    )
    assert n >= 1
    screener_docs = [d for d in docs if d.get("source") == "crustdata_screener"]
    assert len(screener_docs) >= 1
    assert screener_docs[0]["post_url"].startswith("https://www.linkedin.com/posts/example_post_")
    assert screener_docs[0]["source_keyword"] in ("alpha", "beta")
