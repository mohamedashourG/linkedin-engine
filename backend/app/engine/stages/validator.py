"""
Brand-voice + structure invariants for drafted comments.

Per spec:
  - 60-character minimum
  - No em-dashes (—)
  - Type-F has a 3-sentence floor (the "short and punchy" name is a trap)
  - No banned tokens / sycophantic openers
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.engine.constants import (
    COMMENT_MIN_CHARS,
    COMMENT_TYPE_F_MIN_SENTENCES,
    EM_DASH,
)

_BANNED_OPENERS = (
    "great post",
    "love this",
    "amazing post",
    "fantastic insight",
    "this is gold",
    "well said!",
    "couldn't agree more",
    "100% this",
)

_SENTENCE_SPLIT = re.compile(r"[.!?]+\s+")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None


def validate(comment: str, comment_type: str) -> ValidationResult:
    text = (comment or "").strip()
    if len(text) < COMMENT_MIN_CHARS:
        return ValidationResult(False, f"too_short ({len(text)} < {COMMENT_MIN_CHARS})")
    if EM_DASH in text:
        return ValidationResult(False, "contains_em_dash")

    lower = text.lower()
    for banned in _BANNED_OPENERS:
        if lower.startswith(banned):
            return ValidationResult(False, f"sycophantic_opener: {banned!r}")

    if comment_type == "F":
        sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
        if len(sentences) < COMMENT_TYPE_F_MIN_SENTENCES:
            return ValidationResult(
                False,
                f"type_f_floor ({len(sentences)} < {COMMENT_TYPE_F_MIN_SENTENCES})",
            )

    return ValidationResult(True, None)
