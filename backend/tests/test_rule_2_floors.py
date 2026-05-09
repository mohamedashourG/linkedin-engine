"""
RULE 2 — daily target 50 / hard floor 30 / abort floor 25.

Tests the user defaults + RULE 23 Layer 1 floor handling. Uses a small
in-memory fake of pymongo Database — RULE 23 only touches three collections
and a small set of methods, so a stub is cheaper than spinning up Mongo for
a unit test.
"""
from __future__ import annotations

from typing import Any

import pytest
from bson import ObjectId

from app.engine.stages.rule_23 import Rule23ForceAbort, seal_slate
from app.models.user import UserInDB


# ---- defaults ----


def test_user_defaults_match_audit():
    """Audit-locked: 50 / 30 / 25."""
    u = UserInDB(
        _id=ObjectId(),
        email="op@example.com",
        password_hash="x",
        name="Op",
        timezone="UTC",
    )
    assert u.daily_target == 50
    assert u.hard_floor == 30
    assert u.abort_floor == 25


# ---- fake mongo for rule_23 ----


class _Cursor:
    def __init__(self, items: list[dict]):
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _Coll:
    def __init__(self, docs: list[dict] | None = None):
        self.docs = docs or []
        self.updates: list[tuple[dict, dict]] = []
        self.inserts: list[dict] = []
        self.update_many_calls: list[tuple[dict, dict]] = []

    def find(self, query: dict, *_args, **_kwargs) -> _Cursor:
        out = []
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                out.append(d)
        return _Cursor(out)

    def find_one(self, query: dict, *_args, **_kwargs) -> dict | None:
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    def update_one(self, q, u):
        self.updates.append((q, u))

    def update_many(self, q, u):
        self.update_many_calls.append((q, u))

    def insert_one(self, d):
        self.inserts.append(d)


class _DB:
    def __init__(self, slate_id: ObjectId, drafted_count: int):
        self.slate_run_id = slate_id
        self.candidates = _Coll([
            {
                "_id": ObjectId(),
                "slate_run_id": slate_id,
                "status": "drafted",
                "comment_text": "X" * 80 + " specific 47% data point.",  # passes COMMENT_MIN_CHARS + has dash-free + has %-pattern
                "post_url": f"https://www.linkedin.com/posts/some-{i}",
                "post_text": "real text " * 10,
                "cofounder_id": ObjectId(),
            }
            for i in range(drafted_count)
        ])
        self.slate_runs = _Coll([
            {"_id": slate_id, "run_date": "2026-05-09", "operator_id": ObjectId()},
        ])
        self.audit_records = _Coll()


def _operator(*, abort_floor=25, hard_floor=30) -> dict[str, Any]:
    return {
        "_id": ObjectId(),
        "email": "op@example.com",
        "name": "Op",
        "abort_floor": abort_floor,
        "hard_floor": hard_floor,
        "daily_target": 50,
    }


def _cofounder(target: int = 20) -> dict[str, Any]:
    return {"_id": ObjectId(), "daily_volume_target": target}


# ---- abort below abort_floor ----


def test_seal_aborts_below_abort_floor():
    slate_id = ObjectId()
    db = _DB(slate_id, drafted_count=24)  # < 25
    cofounders = [_cofounder() for _ in range(3)]
    # Wire each candidate to a cofounder so cofounder_floor doesn't fire first.
    cf_ids = [cf["_id"] for cf in cofounders]
    for i, c in enumerate(db.candidates.docs):
        c["cofounder_id"] = cf_ids[i % 3]

    with pytest.raises(Rule23ForceAbort) as exc:
        seal_slate(db, operator=_operator(), cofounders=cofounders, slate_run_id=slate_id)
    assert exc.value.reason == "floor_breach"
    assert exc.value.layer == "floor"
    assert exc.value.details["drafted"] == 24
    assert exc.value.details["abort_floor"] == 25


# ---- pass with warning between abort and hard ----


def test_seal_warns_between_abort_and_hard_floor():
    """27 drafted: above abort (25), below hard (30). Should NOT abort but
    should emit a floor_warn audit record."""
    slate_id = ObjectId()
    db = _DB(slate_id, drafted_count=27)
    cofounders = [_cofounder(target=10) for _ in range(3)]  # floor = max(1, 10*0.4)=4 each, fine for 27/3=9
    cf_ids = [cf["_id"] for cf in cofounders]
    for i, c in enumerate(db.candidates.docs):
        c["cofounder_id"] = cf_ids[i % 3]

    result = seal_slate(db, operator=_operator(), cofounders=cofounders, slate_run_id=slate_id)
    assert result["slated"] == 27
    # Warning audit record should be present.
    warn_records = [r for r in db.audit_records.inserts if r.get("event_type") == "floor_warn"]
    assert len(warn_records) == 1
    assert warn_records[0]["severity"] == "warn"
    assert warn_records[0]["details"]["drafted"] == 27
    assert warn_records[0]["details"]["hard_floor"] == 30


# ---- silent pass at or above hard_floor ----


def test_seal_silent_above_hard_floor():
    slate_id = ObjectId()
    db = _DB(slate_id, drafted_count=35)
    cofounders = [_cofounder(target=15) for _ in range(3)]  # 15*0.4=6 floor each, fine for 35/3≈12
    cf_ids = [cf["_id"] for cf in cofounders]
    for i, c in enumerate(db.candidates.docs):
        c["cofounder_id"] = cf_ids[i % 3]

    result = seal_slate(db, operator=_operator(), cofounders=cofounders, slate_run_id=slate_id)
    assert result["slated"] == 35
    # No floor_warn record should be emitted at or above hard_floor.
    warn_records = [r for r in db.audit_records.inserts if r.get("event_type") == "floor_warn"]
    assert len(warn_records) == 0


# ---- exactly at abort_floor passes (with warning) ----


def test_seal_exactly_at_abort_floor_does_not_abort():
    """`< abort_floor` is the abort condition; equal is allowed."""
    slate_id = ObjectId()
    db = _DB(slate_id, drafted_count=25)
    cofounders = [_cofounder(target=10) for _ in range(3)]
    cf_ids = [cf["_id"] for cf in cofounders]
    for i, c in enumerate(db.candidates.docs):
        c["cofounder_id"] = cf_ids[i % 3]

    result = seal_slate(db, operator=_operator(), cofounders=cofounders, slate_run_id=slate_id)
    assert result["slated"] == 25
    # Below hard_floor → warn record present.
    warns = [r for r in db.audit_records.inserts if r.get("event_type") == "floor_warn"]
    assert len(warns) == 1


# ---- back-compat: missing abort_floor falls to 25 ----


def test_seal_falls_back_to_default_abort_floor_for_legacy_operator():
    """Pre-RULE-2 operators don't have abort_floor in Mongo. Default to 25."""
    slate_id = ObjectId()
    db = _DB(slate_id, drafted_count=24)
    cofounders = [_cofounder() for _ in range(3)]
    cf_ids = [cf["_id"] for cf in cofounders]
    for i, c in enumerate(db.candidates.docs):
        c["cofounder_id"] = cf_ids[i % 3]

    operator_legacy = {
        "_id": ObjectId(),
        "email": "legacy@example.com",
        "name": "Legacy",
        # No abort_floor or hard_floor at all
    }
    with pytest.raises(Rule23ForceAbort) as exc:
        seal_slate(db, operator=operator_legacy, cofounders=cofounders, slate_run_id=slate_id)
    assert exc.value.reason == "floor_breach"
    assert exc.value.details["abort_floor"] == 25
    assert exc.value.details["hard_floor"] == 30
