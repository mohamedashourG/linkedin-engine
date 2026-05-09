"""
RULE 19 — Post quality bar (Gate D). Binary pass/fail. AVOID list checked
first; then ≥1 qualifying signal must apply, else drop.

The LLM call itself is hard to test without a mock, but we can verify:
  - the schema enumerates the five audit-listed signals + "none"
  - the system prompt mentions every signal (regression guard against a
    careless edit that drops one)
"""
from __future__ import annotations

import inspect

import pytest

from app.engine.stages.gates import post_quality


REQUIRED_SIGNALS = (
    "direct_expertise",
    "adjacent_buyer",
    "career_update",
    "icp_author_personal",
    "conference_mention",
)


def test_qualifying_signal_field_includes_all_five_audit_signals_plus_none():
    """Pydantic Literal must list exactly the audit's five positive signals
    plus "none"."""
    schema = post_quality._Verdict.model_json_schema()
    qs = schema["properties"]["qualifying_signal"]
    enum_values = set(qs["enum"])
    expected = set(REQUIRED_SIGNALS) | {"none"}
    assert enum_values == expected, (
        f"qualifying_signal enum drift: got {enum_values}, want {expected}"
    )


@pytest.mark.parametrize("signal", REQUIRED_SIGNALS)
def test_system_prompt_mentions_each_signal(signal: str):
    """If a refactor drops a signal from the prompt, the LLM stops emitting
    it. This test catches that before it ships."""
    assert signal in post_quality._SYSTEM, (
        f"RULE 19 audit signal {signal!r} missing from post_quality prompt"
    )


def test_system_prompt_lists_avoid_categories():
    """STEP 1 of RULE 19 enumerates AVOID categories. Spot-check that all
    five (engagement bait, platitudes, self-promo, lyrics, sub-200-char) are
    present in the prompt."""
    s = post_quality._SYSTEM.lower()
    for kw in ("engagement bait", "platitudes", "self-promotion", "lyrics", "200 char"):
        assert kw in s, f"AVOID keyword {kw!r} missing from prompt"


def test_verdict_schema_has_drop_reason_and_signal():
    fields = set(post_quality._Verdict.model_fields.keys())
    assert fields == {"drop", "reason", "qualifying_signal"}


def test_verdict_drop_false_with_signal_parses():
    """The schema must accept a 'kept' verdict where drop=False and a real
    signal is named."""
    v = post_quality._Verdict.model_validate(
        {
            "drop": False,
            "reason": "direct expertise overlap",
            "qualifying_signal": "direct_expertise",
        }
    )
    assert v.drop is False
    assert v.qualifying_signal == "direct_expertise"


def test_verdict_drop_true_with_none_signal_parses():
    v = post_quality._Verdict.model_validate(
        {"drop": True, "reason": "engagement bait", "qualifying_signal": "none"}
    )
    assert v.drop is True
    assert v.qualifying_signal == "none"


def test_verdict_invalid_signal_rejected():
    """Pydantic's Literal must reject signals outside the audit list."""
    with pytest.raises(Exception):
        post_quality._Verdict.model_validate(
            {"drop": False, "reason": "x", "qualifying_signal": "made_up_signal"}
        )
