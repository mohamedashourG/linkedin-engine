"""
Drafter — produces a single LinkedIn comment per allocated candidate.

Prompt is engineered against 8 anchor conversions. Each Type A-F has verbatim
exemplars baked in. The reframe formula for sentence-1 is rotated within the
type's pool to keep the slate from collapsing into one opener.
"""
from __future__ import annotations

import logging
import random

from pydantic import BaseModel, Field

from app.engine.constants import (
    REFRAME_FORMULAS_BY_TYPE,
    SENTENCE_COUNT_BY_TYPE,
    TYPE_CLOSE_PATTERNS,
)
from app.services.llm import parse_structured_sync

log = logging.getLogger(__name__)


class _Draft(BaseModel):
    comment: str = Field(
        description=(
            "The final comment to post. No em-dashes, no en-dashes, no ellipses. "
            "No sycophantic openers. Reads like a single specific human."
        )
    )


# Fallback voice profile used when a cofounder hasn't completed onboarding.
# Generic operator-to-operator tone with three minimal example pairs so the
# drafter has enough signal to produce a coherent comment. The slate's
# resulting comments will be neutral but ship-able — once the cofounder
# completes voice onboarding, this stops being used.
DEFAULT_VOICE_PROFILE: dict = {
    "tone_description": (
        "Direct and operator-minded. Drop the obligatory hedge, lead with "
        "the specific number or constraint, and ask the question that the "
        "post implicitly raised but did not answer. No emojis, no hype "
        "words. Short sentences. Lower-case is fine when it reads natural. "
        "Always treat the author as a peer, never as a prospect."
    ),
    "examples": [
        {
            "post": (
                "We just hit 100 customers. The hardest part was not closing "
                "them, it was figuring out which of the first 10 actually "
                "told us the truth about why they bought."
            ),
            "comment": (
                "the first 10 lying problem is real. half the time the "
                "stated reason is the polite version of the actual reason, "
                "and the actual reason is what tells you whether to double "
                "down on the wedge or rip it out. how did you separate the "
                "two in your case?"
            ),
        },
        {
            "post": (
                "Hot take: most onboarding flows are too long because product "
                "teams treat them like a feature spec instead of a forcing "
                "function. You should be deleting steps every week."
            ),
            "comment": (
                "the spec vs forcing function framing is the part most teams "
                "miss. once a step is in there, deleting it feels political "
                "even when the data says it should go. did you find a way to "
                "make deletion the default rather than the exception?"
            ),
        },
        {
            "post": (
                "Sales engineers are the most leveraged hire at a Series A "
                "B2B company. Change my mind."
            ),
            "comment": (
                "fully agree on the leverage. the part i would add: the SE "
                "is also the only person on the call who can cost you a "
                "deal in the first 60 seconds by being too technical too "
                "early. so leveraged on the upside, leveraged on the "
                "downside. how do you train the second part?"
            ),
        },
    ],
    "source": "fallback_default",
}


DRAFTER_SYSTEM_PROMPT = """\
You draft a single LinkedIn comment in {cofounder_name}'s voice on a post written by an ICP-fit operator. The goal is to land a substantive reply, eventually a connection request, eventually a 30-minute call.

# COFOUNDER VOICE PROFILE

Tone: {cofounder_tone_description}

Examples of comments in this voice (verbatim from the cofounder, mirror their cadence):

{cofounder_voice_examples}

# THE POST

Author: {author_name}, {author_title} at {author_company}
ICP score: {icp_score}/10
Source classification: {source_classification}  (A = directly relevant to product, B = adjacent / personal-narrative)
Comment type assigned: {comment_type}

Post text:
---
{post_text}
---

# WHAT GOOD LOOKS LIKE — VERBATIM EXEMPLARS BY COMMENT TYPE

These are real comments that produced booked meetings. Match the structural shape, not the words.

## Type A — Pure Value (workhorse)

3-5 sentences. Number-as-hook, graduating-the-measurement, named-actor close.

Example (Walter Toro Jimenez, on patient access / launch friction):
The 47% number is the part that should scare every launch finance team, because forecasts built on coverage-status assumptions are sitting on a cliff. Access quality measurement has to graduate from formulary tier to time-to-first-fill at the HCP level. The real question is whether the field team knows which 500 physicians account for most of the friction, because that is where launch velocity is actually decided.

Example (Glen Froio, on biopharma launch team sizing):
Launch teams this lean live or die on targeting quality in the first 90 days. With a T-Rex-sized sales force the model breaks if even 20% of the call list is wrong, because there is no surplus capacity to correct mid-cycle. Worth building the HCP map before the first rep onboards, not after Q1 data comes in.

## Type B — Conversation Starter (highest reply rate at 17%)

3-4 sentences. Names-the-breakdown plus a binary or specific question close.

Example (Eslam Abu saraya):
Where market search usually breaks down for me is the leap from segment-level reads to prescriber-level overlay. Are you finding UAE teams build that overlay in-house or buy it bolted on.

## Type C — Soft Product Mention

3-5 sentences. Reframe, specific signal, sliver-of-product mention woven in (NEVER a pitch), optional implication.

Example (Awanish Pandey, anchor conversion):
The Amazon program is the cleanest live test of cash-pay DTC pharma at scale because it sits inside an existing primary care channel rather than a standalone telehealth flow. The signal worth watching is which PCPs in the One Medical network actually start writing GLP-1s versus referring out, since that adoption curve is what every cash-pay manufacturer needs to model. We have been mapping that prescriber-level shift at G LNK for a few cash-pay teams and the dispersion is bigger than people expect. Would be interesting to compare your forecast view against the actual writer pattern over the next two quarters.

## Type D — DM tee-up (anchor: Shibu Thomas, Blake Siewert)

3-4 sentences. Pain-validation, concrete data observation we have already pulled, DM offer.

Example (Blake Siewert, anchor conversion):
The rheum capacity gap is one of the most quietly painful access problems in adult care, ortho referrals make it worse not better. We pulled the rheumatology prescriber and referrer base recently across a few mid sized health systems, happy to share what we saw if useful. DM me if a quick swap of notes helps the model conversations.

## Type E — Hot Take (anchor: Sam Dyer, 31% reply rate)

3-4 sentences. Reframe-the-frame, specific cohort sizing or named lever, declarative moat or new-winner statement.

Example (Sam Dyer anchor):
Cell therapy launches like this look like awareness plays from the outside, but operationally they are pure targeting problems. The refractory TLE cohort sits with maybe 250 epileptologists nationally, and most of them already triage the same way. The commercial question is not who tells them about NRTX-1001, it is which neurologists upstream are actually identifying drug-resistant cases versus titrating the third AED for another year. UCB will get good ROI not from broad reach but from finding the upstream referrers who currently miss the resistance signal.

## Type F — Short + Punchy (PERSONAL-NARRATIVE POSTS ONLY)

2-3 sentences total. Single sharp question OR three declaratives.

Example (Shahad Alotaibi, anchor):
MSL strategy lives or dies on whether the scientific insight ever changes a brand decision. Does it?

# RULES (NON-NEGOTIABLE)

1. **Sentence count.** Type {comment_type} must have between {min_sentences} and {max_sentences} sentences. Period.
2. **First sentence reframes.** Never agree-and-extend. Never start with "Great point" / "Love this" / "Thanks for sharing" / "Couldn't agree more" / "Spot on" / "I think" / "In my opinion" / "100%" / "Well said" / "Exactly" / "Agreed".
3. **Specificity required.** At least one sentence must contain a specific number, percentage, dollar figure, named cohort size, named role count, or named pattern. Generic statements fail.
4. **Type-specific close.** The final sentence must follow the close pattern for Type {comment_type}:
{type_close_pattern_for_this_comment}
5. **No dashes (RULE 5).** Never use em-dashes (—), en-dashes (–), or double-hyphens (--). Never use ellipses ("..." or "…"). Use commas, periods, or "and" instead.
6. **No buzzwords.** Never use "leverage", "synergy", "value-add", "best-in-class", "game-changer", "north star", "unlock value", "10x", "world-class", "rockstar".
7. **First name only in DMs/CR notes.** Comments need no signature.
8. **Sound like {cofounder_name}, not LinkedIn.** Match the cadence of the voice examples above.
9. **Output the comment text only** in the `comment` field.

# REFRAME FORMULA TO USE

Use this reframe formula in sentence 1: {assigned_reframe_formula}

Replace X / Y / Z with the actual nouns from the post. The formula is the structural skeleton, not the literal words.

# YOUR TASK

Draft ONE comment, Type {comment_type}, on the post above, in {cofounder_name}'s voice. Follow every rule. Match the exemplars structurally. Use the assigned reframe formula in sentence 1.
"""


def _format_examples(examples: list[dict]) -> str:
    if not examples:
        return "(no examples provided — fall back to the verbatim exemplars by type below)"
    out: list[str] = []
    for i, ex in enumerate(examples, start=1):
        post = (ex.get("post") or "").strip()
        comment = (ex.get("comment") or "").strip()
        out.append(f"Example {i}:\nPOST: {post}\nTHEIR COMMENT: {comment}")
    return "\n\n".join(out)


def pick_reframe_formula(
    comment_type: str, *, exclude: list[str] | None = None
) -> str:
    """Pick a reframe formula for the given Type, optionally excluding ones the
    slate-level rebalancer flagged as over-represented."""
    pool = REFRAME_FORMULAS_BY_TYPE.get(comment_type) or REFRAME_FORMULAS_BY_TYPE["A"]
    if exclude:
        candidates = [f for f in pool if f not in exclude] or pool
    else:
        candidates = pool
    return random.choice(candidates)


def draft_comment(
    *,
    cofounder_name: str,
    cofounder_tone: str,
    cofounder_examples: list[dict],
    post_text: str,
    author_name: str | None,
    author_title: str | None,
    author_company: str | None,
    icp_score: int,
    comment_type: str,
    source_classification: str,
    reframe_formula: str | None = None,
    feedback_hint: str | None = None,
) -> tuple[str, str]:
    """
    Returns (comment_text, reframe_formula_used). The caller persists
    `reframe_formula_used` so the slate-level rebalancer can detect over-
    representation later.

    ``feedback_hint`` is an optional hard instruction appended to the
    system prompt when retrying after a validator failure. It tells the
    LLM exactly what to avoid this time (e.g., ``"buzzword:'leverage'"``
    or ``"no_specific_number_or_cohort"``) so the next draft passes
    validation. See ``_draft_one`` in daily_run.py for the retry loop.
    """
    min_s, max_s = SENTENCE_COUNT_BY_TYPE.get(comment_type, (3, 5))
    close_pattern = TYPE_CLOSE_PATTERNS.get(comment_type, "")
    formula = reframe_formula or pick_reframe_formula(comment_type)

    system = DRAFTER_SYSTEM_PROMPT.format(
        cofounder_name=cofounder_name or "the cofounder",
        cofounder_tone_description=cofounder_tone or "(not provided)",
        cofounder_voice_examples=_format_examples(cofounder_examples or []),
        author_name=author_name or "(unknown)",
        author_title=author_title or "(unknown title)",
        author_company=author_company or "(unknown company)",
        icp_score=icp_score,
        source_classification=source_classification or "A",
        comment_type=comment_type,
        post_text=post_text,
        min_sentences=min_s,
        max_sentences=max_s,
        type_close_pattern_for_this_comment=close_pattern,
        assigned_reframe_formula=formula,
    )

    if feedback_hint:
        system = system + "\n\n" + _format_feedback_block(feedback_hint)

    # NOTE: model_tier="drafter" is callsite-scoped. The LLM router treats
    # this tier as a fall-through to the primary tier unless the operator
    # explicitly sets LLM_PROVIDER_DRAFTER. That lets us route comment
    # drafting to Claude (LLM_PROVIDER_DRAFTER=anthropic) while keeping
    # the ICP scoring gate, analyst gate, reply drafter, voice template
    # builder, and ICP extractor on whatever LLM_PROVIDER_PRIMARY says.
    result = parse_structured_sync(
        model_tier="drafter",
        system=system,
        user="Draft the comment now. Output the comment body only in the `comment` field.",
        schema=_Draft,
    )
    return result.comment.strip(), formula


def _format_feedback_block(reason: str) -> str:
    """Translate a validator failure reason into a hard instruction the
    drafter LLM can act on. The reason strings come from
    ``stages/validator.py`` and look like ``"buzzword:'leverage'"`` or
    ``"no_specific_number_or_cohort"`` — we expand each one into a clear
    do-not-do directive for the system prompt."""
    r = reason or ""
    parts: list[str] = [
        "CRITICAL: your previous draft FAILED validation. You MUST fix the",
        f"specific issue below in your next draft. Failure reason: {r!r}.",
        "",
    ]
    rlow = r.lower()
    if rlow.startswith("buzzword:"):
        # Extract the offending token from the rest of the string
        # (everything after the colon, often quoted).
        bad = r.split(":", 1)[1].strip().strip("'\"").strip()
        parts.append(
            f"DO NOT use the word \"{bad}\" anywhere in your draft. Pick "
            "concrete language instead. Other banned buzzwords to avoid: "
            "\"leverage\", \"synergy\", \"value-add\", \"best-in-class\", "
            "\"thought-leader\", \"game-changer\", \"paradigm shift\"."
        )
    elif rlow.startswith("no_specific_number_or_cohort"):
        parts.append(
            "Your reply MUST include ONE concrete anchor: a specific number "
            "(percentage, time, count) OR a named cohort (e.g., \"VPs of "
            "Engineering at Series B SaaS\", \"hospitals over 200 beds\", "
            "\"RCM directors with 5+ EHR migrations\"). Vague references "
            "like \"many teams\" or \"a lot of folks\" do NOT count."
        )
    elif rlow.startswith("banned_opener:"):
        bad = r.split(":", 1)[1].strip().strip("'\"").strip()
        parts.append(
            f"DO NOT start your comment with \"{bad}\". Open differently — "
            "ideally jump straight into the substantive observation."
        )
    elif rlow.startswith("banned_token:"):
        bad = r.split(":", 1)[1].strip().strip("'\"").strip()
        parts.append(
            f"DO NOT use the token \"{bad}\" anywhere in your draft. Often "
            "this is an em-dash (—), en-dash (–), ellipsis (…), or "
            "AI-tell phrase. Use plain punctuation instead."
        )
    elif rlow.startswith("sentence_count_below_floor"):
        parts.append(
            "Your previous draft was too SHORT (sentence count below the "
            "required floor). Add at least one more substantive sentence "
            "with a concrete observation or follow-up — without padding."
        )
    elif rlow.startswith("sentence_count_above_ceiling"):
        parts.append(
            "Your previous draft was too LONG (sentence count above the "
            "ceiling). Tighten — keep only the strongest sentences."
        )
    elif rlow.startswith("too_short"):
        parts.append(
            "Your previous draft was too short in characters. Add a "
            "concrete observation, but stay within the sentence range."
        )
    elif rlow.startswith("too_long"):
        parts.append(
            "Your previous draft was too long in characters. Tighten "
            "ruthlessly while keeping the strongest specific anchor."
        )
    else:
        parts.append(
            "Re-read the rules above carefully and produce a draft that "
            "complies with every constraint."
        )
    return "\n".join(parts)
