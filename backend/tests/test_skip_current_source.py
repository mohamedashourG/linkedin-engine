"""Unit tests for the per-source skip-discovery feature.

Covers:
  - `_should_skip_current_source` atomically consumes the flag only when
    the source-id matches what the worker is iterating.
  - The flag self-clears on consume so a second click during the next
    source works the same way.
  - The flag is a no-op for a source the worker isn't in.
  - The `_next_discovery_source` helper returns the next source-id in the
    documented order (and None when at the end).

These run in-memory with a stub Mongo collection; the only thing under
test is the contract between the worker (find_one_and_update) and the
operator-side flag write.
"""
from __future__ import annotations

from bson import ObjectId

import pytest


def _stub_db_with_skip_flag(*, current_source: str | None, skip_to: str | None):
    """Build a tiny in-memory `db` lookalike with one slate_runs doc.

    The only method `_should_skip_current_source` calls is
    `db.slate_runs.find_one_and_update`. We don't need to implement the
    rest of the Mongo surface; just enough that the matcher matches on
    the same fields the production code does."""
    doc: dict = {
        "_id": ObjectId(),
        "current_source": current_source,
    }
    if skip_to:
        doc["skip_current_source"] = skip_to

    class _Coll:
        def find_one_and_update(self, filter_, update, projection=None):  # noqa: ARG002
            for k, v in filter_.items():
                if doc.get(k) != v:
                    return None
            for k, v in update.get("$set", {}).items():
                doc[k] = v
            for k in update.get("$unset", {}).keys():
                doc.pop(k, None)
            return {"_id": doc["_id"]}

    class _DB:
        slate_runs = _Coll()

    return _DB(), doc


def test_consume_when_flag_matches_current_source():
    from app.engine.stages.discovery import _should_skip_current_source

    db, doc = _stub_db_with_skip_flag(
        current_source="unipile_title_search",
        skip_to="unipile_title_search",
    )
    # First call: flag matches → consumed, returns True
    assert _should_skip_current_source(db, doc["_id"], "unipile_title_search") is True
    # Doc no longer has the flag
    assert "skip_current_source" not in doc
    # Audit trail written
    assert doc.get("skip_current_source_consumed_for") == "unipile_title_search"
    assert doc.get("skip_current_source_consumed_at") is not None
    # Second call: flag was self-cleared → no match → returns False
    assert _should_skip_current_source(db, doc["_id"], "unipile_title_search") is False


def test_does_not_consume_when_flag_targets_different_source():
    """Operator queued a skip for unipile_keyword but the worker is still
    in RULE 24 — the per-iteration check inside RULE 24 must NOT consume
    the flag (it's not for me)."""
    from app.engine.stages.discovery import _should_skip_current_source

    db, doc = _stub_db_with_skip_flag(
        current_source="unipile_title_search",
        skip_to="unipile_keyword",
    )
    # RULE 24's check: not for me, don't consume
    assert _should_skip_current_source(db, doc["_id"], "unipile_title_search") is False
    # Flag is still there for when the worker moves on
    assert doc.get("skip_current_source") == "unipile_keyword"
    # When the worker enters unipile_keyword, its check consumes the flag
    assert _should_skip_current_source(db, doc["_id"], "unipile_keyword") is True
    assert "skip_current_source" not in doc


def test_no_flag_is_no_op():
    from app.engine.stages.discovery import _should_skip_current_source

    db, doc = _stub_db_with_skip_flag(
        current_source="unipile_title_search",
        skip_to=None,
    )
    assert _should_skip_current_source(db, doc["_id"], "unipile_title_search") is False


def test_next_discovery_source_order():
    from app.routes.slate import _next_discovery_source

    assert _next_discovery_source(None) == "unipile_title_search"
    assert _next_discovery_source("unipile_title_search") == "unipile_keyword"
    assert _next_discovery_source("unipile_keyword") == "apidirect"
    assert _next_discovery_source("apidirect") == "exa"
    assert _next_discovery_source("exa") is None
    # Unknown source-id returns None (defensive — UI just won't show a "next")
    assert _next_discovery_source("rogue_source") is None
