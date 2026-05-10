"""
Drafter fallback voice profile.

When a cofounder hasn't completed voice onboarding (`voice_profile is None`),
the drafter must NOT silently drop the candidate with `drafter_no_voice`.
Instead it falls back to `drafter.DEFAULT_VOICE_PROFILE`, drafts a comment
in the generic operator-tone voice, and logs a one-time warning per slate.
"""
from __future__ import annotations

from typing import Any

import pytest
from bson import ObjectId

from app.engine.stages import drafter


def test_default_voice_profile_is_well_formed():
    """The fallback must satisfy what `_draft_one` reads off the dict —
    `tone_description` (str) and `examples` (list of post/comment pairs)."""
    v = drafter.DEFAULT_VOICE_PROFILE
    assert isinstance(v, dict)
    assert isinstance(v.get("tone_description"), str) and len(v["tone_description"]) > 50
    assert isinstance(v.get("examples"), list)
    assert len(v["examples"]) >= 3, "needs at least 3 examples to match VoicePayload schema"
    for ex in v["examples"]:
        assert "post" in ex and "comment" in ex
        assert ex["post"].strip()
        assert ex["comment"].strip()
    assert v.get("source") == "fallback_default", "audit tag so operators can see it's not their voice"


class _Coll:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = docs or []
        self.updates: list[tuple[dict, dict]] = []

    def find(self, query: dict, *_args, **_kwargs):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                yield d

    def update_one(self, q, u, **_kw):
        self.updates.append((q, u))
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items() if not isinstance(v, dict)):
                if "$set" in u:
                    d.update(u["$set"])
                return type("R", (), {"matched_count": 1})()
        return type("R", (), {"matched_count": 0})()


class _DB:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = _Coll(candidates)


def test_run_drafter_uses_fallback_when_voice_profile_missing(monkeypatch):
    """If a cofounder lacks voice_profile, _run_drafter mutates it in-place
    with DEFAULT_VOICE_PROFILE before calling _draft_one — so no candidate
    is dropped with drafter_no_voice."""
    from app.engine import daily_run

    cf_id = ObjectId()
    slate_id = ObjectId()
    candidate = {
        "_id": ObjectId(),
        "slate_run_id": slate_id,
        "cofounder_id": cf_id,
        "status": "allocated",
        "comment_type": "C",
        "post_text": "hello world",
        "source_classification": "A",
        "gate_results": {},
    }
    cofounder = {
        "_id": cf_id,
        "display_name": "Voiceless Founder",
        "voice_profile": None,  # ← onboarding not complete
    }

    db = _DB([candidate])

    captured_cofounder: dict[str, Any] = {}

    def fake_draft_one(_db, c, cof, **_kw):
        # Capture what _draft_one received so we can assert the fallback was injected.
        captured_cofounder["voice_profile"] = cof.get("voice_profile")
        return {"comment_text": "stub", "reframe_formula": "x"}

    monkeypatch.setattr(daily_run, "_draft_one", fake_draft_one)
    monkeypatch.setattr(daily_run, "_rebalance_reframe_overrep", lambda *a, **k: 0)

    drafted = daily_run._run_drafter(db, slate_run_id=slate_id, cofounders=[cofounder])

    assert drafted == 1, "candidate must NOT be dropped just because voice_profile is missing"
    assert captured_cofounder["voice_profile"] is not None
    assert captured_cofounder["voice_profile"]["source"] == "fallback_default"
    # The cofounder dict was mutated in-place.
    assert cofounder["voice_profile"] is drafter.DEFAULT_VOICE_PROFILE


def test_run_drafter_keeps_real_voice_profile_when_present(monkeypatch):
    """If the cofounder DOES have a voice profile, the fallback is NOT used."""
    from app.engine import daily_run

    cf_id = ObjectId()
    slate_id = ObjectId()
    real_voice = {
        "tone_description": "real custom tone",
        "examples": [{"post": "p", "comment": "c"}],
    }
    candidate = {
        "_id": ObjectId(),
        "slate_run_id": slate_id,
        "cofounder_id": cf_id,
        "status": "allocated",
        "comment_type": "A",
        "post_text": "x",
        "source_classification": "A",
        "gate_results": {},
    }
    cofounder = {"_id": cf_id, "display_name": "Real", "voice_profile": real_voice}
    db = _DB([candidate])

    captured: dict = {}

    def fake_draft_one(_db, c, cof, **_kw):
        captured["voice_profile"] = cof.get("voice_profile")
        return {"comment_text": "stub", "reframe_formula": "x"}

    monkeypatch.setattr(daily_run, "_draft_one", fake_draft_one)
    monkeypatch.setattr(daily_run, "_rebalance_reframe_overrep", lambda *a, **k: 0)

    drafted = daily_run._run_drafter(db, slate_run_id=slate_id, cofounders=[cofounder])
    assert drafted == 1
    assert captured["voice_profile"] is real_voice


def test_run_drafter_drops_when_cofounder_missing(monkeypatch):
    """Hard error: a candidate references a cofounder_id that doesn't exist
    in the cofounders list — drop with the explicit reason."""
    from app.engine import daily_run

    slate_id = ObjectId()
    candidate = {
        "_id": ObjectId(),
        "slate_run_id": slate_id,
        "cofounder_id": ObjectId(),  # not in the cofounders list
        "status": "allocated",
        "comment_type": "A",
        "post_text": "x",
        "source_classification": "A",
    }
    db = _DB([candidate])

    drops: list = []

    def fake_drop(_db, c, reason, **_):
        drops.append(reason)

    monkeypatch.setattr(daily_run, "_drop", fake_drop)
    monkeypatch.setattr(daily_run, "_draft_one", lambda *a, **k: {"x": "y"})
    monkeypatch.setattr(daily_run, "_rebalance_reframe_overrep", lambda *a, **k: 0)

    drafted = daily_run._run_drafter(db, slate_run_id=slate_id, cofounders=[])
    assert drafted == 0
    assert drops == ["drafter_no_cofounder"]
