"""Engine-wide constants. Kept tight so they're trivial to tune in one place."""
from __future__ import annotations

# How many keywords from each tier we draw on per cofounder per run.
# Spec defaults: 8/5/0. Higher discovery → more candidates → more survivors
# after gates → enough drafts to clear the per-cofounder floor.
DISCOVERY_TIER_1_PER_RUN = 8
DISCOVERY_TIER_2_PER_RUN = 5
DISCOVERY_TIER_3_PER_RUN = 0  # tier-3 only used as last-resort fallback

# Exa: one API call per keyword; broader tier mix than apidirect/unipile so
# each round retrieves enough raw candidates to survive gates and Rule 23.
EXA_DISCOVERY_TIER_1_PER_RUN = 12
EXA_DISCOVERY_TIER_2_PER_RUN = 8
EXA_DISCOVERY_TIER_3_PER_RUN = 4

# Pages per keyword search on apidirect.
DISCOVERY_PAGES_PER_KEYWORD = 1

# RULE 15-EXT: title-plus-industry queries through the cofounder's LinkedIn
# content search (Unipile). Per cofounder per run; 4 keeps the daily mix
# (4 topical + 4 title-industry) consistent with the audit. The pool itself
# (14 queries, 14-day no-repeat) lives in configs/<client>/keyword_pools.json.
DISCOVERY_TITLE_INDUSTRY_PER_RUN = 4

# RULE 24: title-search PEOPLE channel via Unipile. Audit-locked envelope
# is 6 queries × 10 candidates × N cofounders per day. Recent-activity walk
# fetches up to 5 posts per person.
DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN = 6
DISCOVERY_TITLE_SEARCH_PEOPLE_PER_QUERY = 10
DISCOVERY_TITLE_SEARCH_POSTS_PER_PERSON = 5

# Exhaustion ledger lookback window (days).
EXHAUSTION_LOOKBACK_DAYS = 90

# Comment-type quotas as {floor, cap} percentages of the slate.
#
# Locked values from the 2026-05-05 audit (RULE 7): Type-E was the best
# performer (31% reply), Type-A the worst (4.8%), Type-B dead (3%). Quotas
# are now {floor, cap} not (lo, hi) — caps are MAX percentages, floors are
# MIN percentages, so the allocator can mix the slate without violating
# either bound. Old (lo, hi) tuples are still accepted as legacy input;
# allocator._normalize_quota maps them to {floor=lo, cap=hi}.
COMMENT_TYPE_QUOTAS_DEFAULT: dict[str, dict[str, int]] = {
    "A": {"floor": 0,  "cap": 25},
    "B": {"floor": 0,  "cap": 10},
    "C": {"floor": 25, "cap": 100},
    "D": {"floor": 10, "cap": 15},
    "E": {"floor": 20, "cap": 100},
    "F": {"floor": 5,  "cap": 10},
}

# Comment-type descriptions, embedded into the drafter prompt.
COMMENT_TYPE_DESCRIPTIONS = {
    "A": (
        "Pure value. 3-5 sentences. Number-as-hook, graduating-the-measurement, "
        "named-actor close."
    ),
    "B": (
        "Conversation starter. 3-4 sentences. Names-the-breakdown, ends with a "
        "binary or specific question."
    ),
    "C": (
        "Soft product mention. 3-5 sentences. Reframe, specific signal, "
        "sliver-of-product reference (never a pitch)."
    ),
    "D": (
        "DM tee-up. 3-4 sentences. Pain-validation, concrete data observation "
        "we have already pulled, DM offer."
    ),
    "E": (
        "Hot take / synthesis-contrarian. 3-4 sentences. Reframe the frame, "
        "specific cohort or named lever, declarative new-moat statement."
    ),
    "F": (
        "Short + punchy on personal-narrative posts ONLY (career reflection, "
        "personal story, congratulatory). 2-3 sentences. Single sharp question "
        "OR three declaratives. Never on commercial commentary."
    ),
}

# Validator constants.
COMMENT_MIN_CHARS = 60
COMMENT_MAX_CHARS = 1200

# RULE 5 — dash family banned everywhere (comments, DMs, CR notes, anywhere
# the engine emits text). Keep this list as the SINGLE source of truth so
# validator.has_any_dash() and rule_23 stay in lockstep.
DASH_TOKENS = (
    "—",   # em-dash
    "–",   # en-dash
    "--",  # double hyphen (em-dash stand-in)
)
EM_DASH = DASH_TOKENS[0]  # back-compat alias for any external readers

# RULE 23 force-abort allowlist (4 reasons exactly per spec).
RULE_23_FORCE_ABORT_REASONS = (
    "floor_breach",
    "cofounder_imbalance",
    "comment_invalid",
    "verified_tuple_missing",
)

# Per-cofounder floor relaxation: each cofounder can run up to 60% under target
# as long as the operator-level floor is met. Lowered from 0.7 to stop
# cofounder_imbalance force-aborts when one cofounder has sparse post volume.
COFOUNDER_TARGET_FLOOR_RATIO = 0.4


# =============================================================================
# DRAFTER TONE GUARDRAILS — derived from 8 anchor conversions (2026-04-20 → 2026-05-08)
# Source: conversion case studies + anchor conversions docs
# =============================================================================

# Banned openers — validator rejects on first 80 chars matching these (case-insensitive).
BANNED_OPENERS = [
    "great point",
    "love this",
    "thanks for sharing",
    "couldn't agree more",
    "spot on",
    "i think",
    "in my opinion",
    "well said",
    "exactly",
    "agreed",
    "100%",
    "this is so true",
    "this is gold",
    "amazing post",
    "great post",
    "nice post",
    "important topic",
    "good question",
]

# Banned characters / sequences — validator rejects on any match.
# Dash family pulled from DASH_TOKENS (RULE 5) so there's only one list to keep
# in sync. Ellipses stay because they signal trailing thought, which doesn't
# fit the locked voice (separate from RULE 5).
BANNED_TOKENS = [
    *DASH_TOKENS,
    "…",       # horizontal ellipsis character
    "...",     # three-dot ellipsis
]

# Buzzwords — validator rejects on any match (case-insensitive substring).
BANNED_BUZZWORDS = [
    "leverage",
    "synergy",
    "value-add",
    "value add",
    "best-in-class",
    "best in class",
    "game-changer",
    "game changer",
    "north star",
    "unlock value",
    "10x",
    "world-class",
    "world class",
    "rockstar",
]

# Voice fingerprint phrases — drafter prompt should be told these are the signature
# patterns; not required in every comment but should appear in ~30%+ of slate.
VOICE_FINGERPRINT_PHRASES = [
    "is the right wedge",
    "is the cleanest test of",
    "the real {noun} is",
    "X is less about Y and more about Z",
    "happens fast and {obvious_source} alone misses it",
    "the dispersion is bigger than people expect",
    "is one of the quiet failure modes nobody screens for early",
    "sits with maybe {N} {role} nationally",
    "this matches what we see",
    "we have been mapping",
    "X looks like Y from the outside, but operationally it is Z",
    "the teams that {win_condition} are the ones that",
    "is becoming the real moat, not",
    "is decided in the first {N} {time_unit}, not after",
]

# Type-specific close patterns. Drafter prompt enforces ONE close pattern per type.
TYPE_CLOSE_PATTERNS = {
    "A": (
        "Prescriptive close. Tell the reader what to do or not do. Examples: "
        "'Worth building X before Y, not after.' / 'The teams that close that "
        "loop are the ones who track which insights changed a brand decision.'"
    ),
    "B": (
        "Question close. Binary or specific. Examples: 'Are you finding teams "
        "build that overlay in-house or buy it bolted on?' / 'Curious whether "
        "the medical affairs side is keeping pace with field commercial on the "
        "workflow.'"
    ),
    "C": (
        "Soft product mention. Weave in one sentence about what {product} does — "
        "never a pitch. Examples: 'We have been mapping that prescriber-level "
        "shift at G LNK and the dispersion is bigger than people expect.' / "
        "'We do this refresh work at G LNK on top of claims because the "
        "cash-pay flip happens fast and CMS data alone misses it.'"
    ),
    "D": (
        "Data offer + DM CTA. Examples: 'We pulled the rheumatology prescriber "
        "and referrer base recently across a few mid sized health systems, "
        "happy to share what we saw if useful. DM me if a quick swap of notes "
        "helps the model conversations.' / 'Happy to DM a quick note on how "
        "we are seeing launch teams tighten the HCP universe before a CSO "
        "ramps, useful either way.'"
    ),
    "E": (
        "Declarative reframe / new moat statement. Examples: 'Adherence data "
        "is becoming the real moat, not distribution.' / 'UCB will get good "
        "ROI not from broad reach but from finding the upstream referrers who "
        "currently miss the resistance signal.'"
    ),
    "F": (
        "Single sharp question OR three-sentence punchy declarative. ONLY "
        "allowed when post is personal-narrative shape (career reflection, "
        "personal story, congratulatory). Examples: 'Does it?' (Shahad) / "
        "'Strong addition for the THV team. Healthcare investing needs more "
        "operators who have actually shipped at scale.' (Laura Walk Andy "
        "Slavitt hire)"
    ),
}

# Sentence count rules. Type-F now allows 2 sentences (anchor: Shahad's "Does it?")
# but only on personal-narrative posts.
SENTENCE_COUNT_BY_TYPE = {
    "A": (3, 5),
    "B": (3, 5),
    "C": (3, 5),
    "D": (3, 4),
    "E": (3, 4),
    "F": (2, 3),
}

# Required structural elements — at least one of these must be present in the body.
# Validator regex set.
REQUIRED_SPECIFICITY_PATTERNS = [
    r"\b\d{1,3}%",                           # percentage: "47%", "20%"
    r"\$\d+(\.\d+)?[BMK]\b",                 # dollar figure: "$650M", "$1.2B"
    r"\b\d+\s*(to|–|-)\s*\d+\b",             # range: "8 to 10", "5-10", "5–10"
    r"\b\d{2,4}\b",                          # raw number: "250", "500", "20"
]


# =============================================================================
# Reframe formula rotation — sentence-1 templates by Type.
# Allocator-level: drafter picks one of these per Type, and the slate-level
# rebalancer redrafts if any single formula appears in >40% of the slate.
# =============================================================================

REFRAME_FORMULAS_BY_TYPE: dict[str, list[str]] = {
    "A": [
        "X is the cleanest test of Z because",
        "Where X usually breaks down is",
        "X is one of the quiet failure modes nobody screens for early",
    ],
    "B": [
        "Where X usually breaks down for me is",
        "X is less about Y and more about Z",
    ],
    "C": [
        "X is the cleanest test of Z because",
        "X looks like Y from the outside, but operationally it is Z",
        "This matches what we see",
    ],
    "D": [
        "X is one of the quiet failure modes nobody screens for early",
        "X is the cleanest test of Z because",
    ],
    "E": [
        "X looks like Y from the outside, but operationally it is Z",
        "X is less about Y and more about Z",
        "X is one of the quiet failure modes nobody screens for early",
    ],
    "F": [
        "X is less about Y and more about Z",
    ],
}

# If any single reframe formula represents > this fraction of a slate, the
# rebalancer redrafts the over-represented candidates with a different formula.
REFRAME_OVER_REPRESENTATION_THRESHOLD = 0.40


# =============================================================================
# CR note guardrails — phrases that signal the LLM didn't get a real signal.
# =============================================================================

GENERIC_CR_PHRASES = [
    "the product thread",
    "the post we engaged on",
    "the thread we engaged on",
    "your recent post",
    "the recent thread",
    "the recent post",
    "the post you wrote",
]


# =============================================================================
# DM-specific validation rules (HOT / WARM / STAGE_6).
# =============================================================================

# Calendly-direct DM (POST_CR_DM_HOT) lockstep rules.
DM_HOT_REQUIRED_LOWERCASE_LETS = "lets"  # not "let's" — locked voice
DM_HOT_BANNED_LETS = "let's"

# WARM DM must NOT include a calendly URL — calendly drops in DM #2 if engaged.
DM_WARM_BANNED_DOMAINS = ("calendly.com",)

# STAGE_6 must include both a "loved/enjoyed our exchange" phrase and a calendly URL.
DM_STAGE_6_REQUIRED_PHRASES = [
    "loved our exchange",
    "really enjoyed our exchange",
    "enjoyed our exchange",
]
