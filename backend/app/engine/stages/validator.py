"""
Brand-voice + structural invariants for drafted comments, DMs, and CR notes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.engine.constants import (
    BANNED_BUZZWORDS,
    BANNED_OPENERS,
    BANNED_TOKENS,
    COMMENT_MAX_CHARS,
    COMMENT_MIN_CHARS,
    DM_HOT_BANNED_LETS,
    DM_HOT_REQUIRED_LOWERCASE_LETS,
    DM_STAGE_6_REQUIRED_PHRASES,
    DM_WARM_BANNED_DOMAINS,
    GENERIC_CR_PHRASES,
    REQUIRED_SPECIFICITY_PATTERNS,
    SENTENCE_COUNT_BY_TYPE,
)

_OPENER_HEAD_CHARS = 80
_CR_MIN_CHARS = 40
_CR_MAX_CHARS = 200
_DM_MIN_CHARS = 20
_DM_MAX_CHARS = 1500
_REPLY_BACK_SENTENCE_RANGE = (2, 5)
_SENTENCE_SPLIT = re.compile(r"[.!?]+(?=\s|$)")
_COMPILED_SPECIFICITY = [re.compile(p) for p in REQUIRED_SPECIFICITY_PATTERNS]
# Ellipsis only banned in DMs (em-dashes are tolerated per spec).
_DM_BANNED_TOKENS = ("…", "...")
_CALENDLY_RE = re.compile(r"https?://(?:www\.)?calendly\.com/[\w\-/.?=&]+", re.I)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None


# ---------------------------------------------------------------- helpers


def count_sentences(text: str) -> int:
    parts = _SENTENCE_SPLIT.split(text.strip())
    return sum(1 for p in parts if p.strip())


def has_banned_opener(text: str) -> tuple[bool, str | None]:
    head = text.strip().lower()[:_OPENER_HEAD_CHARS]
    for banned in BANNED_OPENERS:
        if head.startswith(banned.lower()):
            return True, banned
    return False, None


def has_banned_token(text: str) -> tuple[bool, str | None]:
    for token in BANNED_TOKENS:
        if token in text:
            return True, token
    return False, None


def has_buzzword(text: str) -> tuple[bool, str | None]:
    lower = text.lower()
    for buzzword in BANNED_BUZZWORDS:
        if buzzword in lower:
            return True, buzzword
    return False, None


def has_required_specificity(text: str) -> bool:
    return any(pat.search(text) for pat in _COMPILED_SPECIFICITY)


def has_calendly_url(text: str) -> bool:
    return bool(_CALENDLY_RE.search(text))


def has_dm_banned_token(text: str) -> tuple[bool, str | None]:
    """DMs allow em-dashes but not ellipsis."""
    for token in _DM_BANNED_TOKENS:
        if token in text:
            return True, token
    return False, None


def has_generic_cr_phrase(text: str) -> tuple[bool, str | None]:
    """CR-note rejection: phrases that prove the LLM never received a real signal."""
    lower = text.lower()
    for phrase in GENERIC_CR_PHRASES:
        if phrase in lower:
            return True, phrase
    return False, None


# ---------------------------------------------------------------- comment


def validate_comment(
    text: str,
    comment_type: str,
    *,
    sentence_range: tuple[int, int] | None = None,
    require_specificity: bool = True,
) -> ValidationResult:
    """
    Returns ValidationResult. Order chosen so the most informative failure
    surfaces first.

    `sentence_range` overrides the per-type default — used by the public
    reply-back validator which wants 2-5 instead of the strict per-type range.
    `require_specificity` can be relaxed to False for short reply-backs.
    """
    if not text or not text.strip():
        return ValidationResult(False, "comment_empty")
    text = text.strip()

    n_chars = len(text)
    if n_chars < COMMENT_MIN_CHARS:
        return ValidationResult(False, f"too_short:{n_chars}<{COMMENT_MIN_CHARS}")
    if n_chars > COMMENT_MAX_CHARS:
        return ValidationResult(False, f"too_long:{n_chars}>{COMMENT_MAX_CHARS}")

    has_bt, token = has_banned_token(text)
    if has_bt:
        return ValidationResult(False, f"banned_token:{token!r}")

    has_bo, opener = has_banned_opener(text)
    if has_bo:
        return ValidationResult(False, f"banned_opener:{opener!r}")

    has_buzz, buzz = has_buzzword(text)
    if has_buzz:
        return ValidationResult(False, f"buzzword:{buzz!r}")

    sentences = count_sentences(text)
    min_s, max_s = sentence_range or SENTENCE_COUNT_BY_TYPE.get(comment_type, (3, 5))
    if sentences < min_s:
        return ValidationResult(
            False, f"sentence_count_below_floor:{sentences}<{min_s}"
        )
    if sentences > max_s:
        return ValidationResult(
            False, f"sentence_count_above_ceiling:{sentences}>{max_s}"
        )

    if require_specificity and not has_required_specificity(text):
        return ValidationResult(False, "no_specific_number_or_cohort")

    return ValidationResult(True, None)


def validate_public_reply_back(text: str) -> ValidationResult:
    """Public reply-backs are shorter (2-5 sentences) and don't enforce
    specificity (a curiosity-question close can stand alone)."""
    return validate_comment(
        text,
        comment_type="A",
        sentence_range=_REPLY_BACK_SENTENCE_RANGE,
        require_specificity=False,
    )


# ---------------------------------------------------------------- DM


def validate_dm(text: str, context: str) -> ValidationResult:
    """
    Context-aware DM validator. Em-dashes are tolerated in DMs (spec). Each
    context has its own structural requirements:
      - POST_CR_DM_HOT     : lowercase 'lets', calendly URL on its own line
      - POST_CR_DM_WARM    : NO calendly URL (drops in DM #2)
      - STAGE_6_DM         : 'enjoyed our exchange' phrase + calendly URL
    """
    if not text or not text.strip():
        return ValidationResult(False, "dm_empty")
    text = text.strip()

    n_chars = len(text)
    if n_chars < _DM_MIN_CHARS:
        return ValidationResult(False, f"dm_too_short:{n_chars}<{_DM_MIN_CHARS}")
    if n_chars > _DM_MAX_CHARS:
        return ValidationResult(False, f"dm_too_long:{n_chars}>{_DM_MAX_CHARS}")

    has_ellipsis, token = has_dm_banned_token(text)
    if has_ellipsis:
        return ValidationResult(False, f"banned_token:{token!r}")

    has_bo, opener = has_banned_opener(text)
    if has_bo:
        return ValidationResult(False, f"banned_opener:{opener!r}")

    has_buzz, buzz = has_buzzword(text)
    if has_buzz:
        return ValidationResult(False, f"buzzword:{buzz!r}")

    if context == "POST_CR_DM_HOT":
        # Locked voice: lowercase "lets", calendly URL on its own line, no agenda.
        if DM_HOT_BANNED_LETS in text.lower():
            return ValidationResult(False, "dm_hot_uppercase_lets")
        if DM_HOT_REQUIRED_LOWERCASE_LETS not in text.lower():
            return ValidationResult(False, "dm_hot_missing_lowercase_lets")
        if not has_calendly_url(text):
            return ValidationResult(False, "dm_hot_missing_calendly")
        # Calendly should be on its own line.
        for line in text.splitlines():
            if _CALENDLY_RE.search(line):
                stripped = _CALENDLY_RE.sub("", line).strip()
                if stripped:
                    return ValidationResult(
                        False, "dm_hot_calendly_not_on_own_line"
                    )
                break

    elif context == "POST_CR_DM_WARM":
        if has_calendly_url(text):
            return ValidationResult(False, "dm_warm_has_calendly_url")

    elif context == "STAGE_6_DM":
        lower = text.lower()
        if not any(phrase in lower for phrase in DM_STAGE_6_REQUIRED_PHRASES):
            return ValidationResult(False, "dm_stage6_missing_exchange_phrase")
        if not has_calendly_url(text):
            return ValidationResult(False, "dm_stage6_missing_calendly")

    return ValidationResult(True, None)


# ---------------------------------------------------------------- CR note


def validate_cr_note(text: str) -> ValidationResult:
    """
    CR-note rules: 40-200 chars, no em-dashes/ellipses, no buzzwords, no
    generic placeholder phrases ("the product thread", "your recent post").
    """
    if not text or not text.strip():
        return ValidationResult(False, "cr_empty")
    text = text.strip()

    n_chars = len(text)
    if n_chars > _CR_MAX_CHARS:
        return ValidationResult(False, f"cr_too_long:{n_chars}>{_CR_MAX_CHARS}")
    if n_chars < _CR_MIN_CHARS:
        return ValidationResult(False, f"cr_too_short:{n_chars}<{_CR_MIN_CHARS}")

    has_bt, token = has_banned_token(text)
    if has_bt:
        return ValidationResult(False, f"banned_token:{token!r}")

    has_buzz, buzz = has_buzzword(text)
    if has_buzz:
        return ValidationResult(False, f"buzzword:{buzz!r}")

    has_generic, phrase = has_generic_cr_phrase(text)
    if has_generic:
        return ValidationResult(False, f"generic_phrase:{phrase!r}")

    return ValidationResult(True, None)


# ---------------------------------------------------------------- back-compat


validate = validate_comment
