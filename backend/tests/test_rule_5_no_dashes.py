"""
RULE 5 — NO DASHES policy. Em-dash (—), en-dash (–), double-hyphen (--)
are banned everywhere: comments, DMs, CR notes, RULE 23 layer 1.

These are pure-function tests; no Mongo or network required.
"""
from __future__ import annotations

import pytest

from app.engine.constants import DASH_TOKENS
from app.engine.stages.validator import (
    has_any_dash,
    validate_comment,
    validate_cr_note,
    validate_dm,
    validate_public_reply_back,
)


# ---- has_any_dash: single source of truth ----

def test_has_any_dash_em():
    flag, tok = has_any_dash("3 to 5 sentences — a clean rule")
    assert flag and tok == "—"


def test_has_any_dash_en():
    flag, tok = has_any_dash("ranges 3–5 sentences")
    assert flag and tok == "–"


def test_has_any_dash_double_hyphen():
    flag, tok = has_any_dash("a clean rule -- and a strong one")
    assert flag and tok == "--"


def test_has_any_dash_clean():
    flag, tok = has_any_dash("3 to 5 sentences, a clean rule")
    assert flag is False
    assert tok is None


def test_has_any_dash_single_hyphen_is_fine():
    """Single hyphens (compound words, ranges like 3-5) are NOT banned."""
    flag, _ = has_any_dash("3-5 sentences, end-of-quarter recap, value-add not banned here")
    assert flag is False


def test_dash_tokens_constant_is_canonical():
    """The constant is the single source of truth that validator and rule_23
    both consume. If this list changes, both should pick it up automatically."""
    assert "—" in DASH_TOKENS
    assert "–" in DASH_TOKENS
    assert "--" in DASH_TOKENS


# ---- validate_comment: rejects dashes ----

_GOOD_COMMENT = (
    "The 47% number is the part that should scare every launch finance team. "
    "Forecasts built on coverage status assumptions sit on a cliff. "
    "Worth building the HCP map before the first rep onboards."
)


@pytest.mark.parametrize("dash", DASH_TOKENS)
def test_validate_comment_rejects_each_dash(dash: str):
    text = f"The 47% number {dash} is the part that should scare every launch finance team. Worth building before, not after. Specific cohort sizing matters."
    result = validate_comment(text, "A")
    assert result.ok is False
    assert "banned_token" in (result.reason or "")


def test_validate_comment_clean_passes():
    result = validate_comment(_GOOD_COMMENT, "A")
    assert result.ok, f"unexpected reject: {result.reason}"


# ---- DM validators: dashes now banned (was: em-dashes tolerated) ----

# A reasonably-shaped HOT DM — to be mutated with each dash.
_GOOD_HOT_DM = (
    "Sure Sam, lets do that, feel free to put some time on my calendar.\n\n"
    "https://calendly.com/alexander-glnkco/meeting-with-alex"
)


@pytest.mark.parametrize("dash", DASH_TOKENS)
def test_validate_dm_hot_rejects_each_dash(dash: str):
    text = f"Sure Sam {dash} lets do that, feel free to put some time on my calendar.\n\nhttps://calendly.com/alexander-glnkco/meeting-with-alex"
    result = validate_dm(text, "POST_CR_DM_HOT")
    assert result.ok is False
    assert result.reason and "banned_token" in result.reason, f"got reason {result.reason!r}"


def test_validate_dm_hot_clean_passes():
    result = validate_dm(_GOOD_HOT_DM, "POST_CR_DM_HOT")
    assert result.ok, f"unexpected reject: {result.reason}"


@pytest.mark.parametrize("dash", DASH_TOKENS)
def test_validate_dm_warm_rejects_each_dash(dash: str):
    text = (
        f"Hi Awanish, thanks for the accept. The Amazon GLP-1 thread {dash} "
        "you posted is one we keep coming back to. Happy to share what we are seeing. Alex"
    )
    result = validate_dm(text, "POST_CR_DM_WARM")
    assert result.ok is False
    assert "banned_token" in (result.reason or "")


def test_validate_dm_warm_clean_passes():
    text = (
        "Hi Awanish, thanks for the accept. The Amazon GLP-1 thread you posted "
        "is one we keep coming back to. Happy to share what we are seeing. Alex"
    )
    result = validate_dm(text, "POST_CR_DM_WARM")
    assert result.ok, f"unexpected reject: {result.reason}"


@pytest.mark.parametrize("dash", DASH_TOKENS)
def test_validate_dm_stage6_rejects_each_dash(dash: str):
    text = (
        f"Hey Sam, thanks for connecting. Really enjoyed our exchange {dash} on launch sequencing.\n\n"
        "If a 20 minute call works, here's my calendar: https://calendly.com/alexander-glnkco"
    )
    result = validate_dm(text, "STAGE_6_DM")
    assert result.ok is False
    assert "banned_token" in (result.reason or "")


# ---- CR-note validator: dashes already covered via BANNED_TOKENS, double-hyphen new ----

@pytest.mark.parametrize("dash", DASH_TOKENS)
def test_validate_cr_note_rejects_each_dash(dash: str):
    text = (
        f"Hi Awanish, enjoyed your GLP-1 post {dash} we have been mapping cash-pay "
        "prescriber behaviour. Would love to stay in touch. Alex"
    )
    result = validate_cr_note(text)
    assert result.ok is False
    assert "banned_token" in (result.reason or "")


def test_validate_cr_note_clean_passes():
    text = (
        "Hi Awanish, enjoyed your GLP-1 post on the Amazon One Medical program. "
        "We have been mapping cash-pay prescriber behaviour. Would love to stay in touch. Alex"
    )
    result = validate_cr_note(text)
    assert result.ok, f"unexpected reject: {result.reason}"


# ---- public reply-back ----


@pytest.mark.parametrize("dash", DASH_TOKENS)
def test_validate_public_reply_back_rejects_each_dash(dash: str):
    text = (
        f"The teams that close that loop {dash} in our experience are the ones who track which insights changed a brand decision. "
        "Curious how you handle the loop on your side."
    )
    result = validate_public_reply_back(text)
    assert result.ok is False
    assert "banned_token" in (result.reason or "")
