"""
Build per-cofounder voice templates from tone description + 3-5 example
post/comment pairs.

Per spec (decision #16, source-aware voice):
- source_a_template — for substantive, tier-1 keyword matches
- source_b_template — for warm-light, tier-2/3 or embedded harvester sources

Both templates are system-prompts the drafter (Phase 3) will inject into the
comment-generation call.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.llm import parse_structured


class VoiceExample(BaseModel):
    post: str
    comment: str


class _VoiceTemplates(BaseModel):
    source_a_template: str = Field(
        description=(
            "System prompt for substantive Source A comments (tier-1 keyword "
            "matches). Should reflect the cofounder's tone, encode their typical "
            "structural moves (length, opener style, how they cite specifics), "
            "and exclude em-dashes. 200-450 words."
        )
    )
    source_b_template: str = Field(
        description=(
            "System prompt for warm-light Source B comments (tier-2/3 or embedded "
            "harvester sources). Friendlier, shorter, less analytical than Source A. "
            "Same voice, lower intensity. 150-350 words."
        )
    )


_SYSTEM = """You are designing voice prompts for a LinkedIn engagement engine.

The operator manages a 'cofounder' (a real person whose LinkedIn account will post the comments). You will write two system prompts the engine uses when drafting comments in their voice.

Source A vs Source B (distinct prompts):
- Source A is for substantive engagement on tier-1 keyword matches. Comments are high-effort, may reference specifics in the post, and should land like a peer responding from experience.
- Source B is for warm-light engagement on tier-2/3 keyword matches or embedded-harvester names. Friendlier, shorter, lower stakes. Same voice, just less intense.

Hard rules — embed these in BOTH templates:
- Never use em-dashes (—). Use commas, periods, or parentheses.
- Never sycophantic openers ("Great post!", "Love this!").
- Comments must read like a single specific human, not a brand.
- Type-F comments (short and punchy) still need a 3-sentence floor.
- 60-character minimum on every comment.

Distill the operator's tone description and examples into a voice that captures their typical moves: opener style, how long their comments are, when they share their own data point, how they end (question? statement? nothing?).

Output two system prompts. Both should be self-contained — they'll be passed to a separate model call without other context."""


def _format_examples(examples: list[VoiceExample]) -> str:
    lines: list[str] = []
    for i, ex in enumerate(examples, start=1):
        lines.append(f"--- Example {i} ---")
        lines.append(f"POST:\n{ex.post.strip()}")
        lines.append(f"\nTHEIR COMMENT:\n{ex.comment.strip()}")
        lines.append("")
    return "\n".join(lines)


async def build_templates(
    *, tone_description: str, examples: list[VoiceExample]
) -> dict:
    """
    Returns: {"source_a_template": str, "source_b_template": str}
    """
    if len(examples) < 3:
        raise ValueError("Provide at least 3 example post/comment pairs.")

    user_msg = (
        f"Tone description:\n{tone_description.strip()}\n\n"
        f"Examples ({len(examples)}):\n\n{_format_examples(examples)}"
    )

    templates = await parse_structured(
        model_tier="primary",
        system=_SYSTEM,
        user=user_msg,
        schema=_VoiceTemplates,
    )
    return {
        "source_a_template": templates.source_a_template,
        "source_b_template": templates.source_b_template,
    }
