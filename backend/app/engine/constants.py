"""Engine-wide constants. Kept tight so they're trivial to tune in one place."""
from __future__ import annotations

# How many keywords from each tier we draw on per cofounder per run.
# Tuned conservatively for first-run cost — the spec calls for 8/5/0, raise these
# once you've verified the pipeline behaves on real data.
DISCOVERY_TIER_1_PER_RUN = 2
DISCOVERY_TIER_2_PER_RUN = 1
DISCOVERY_TIER_3_PER_RUN = 0  # tier-3 only used as last-resort fallback

# Pages per keyword search on apidirect.
DISCOVERY_PAGES_PER_KEYWORD = 1

# Exhaustion ledger lookback window (days).
EXHAUSTION_LOOKBACK_DAYS = 90

# Comment-type quotas (% of slate). Match spec decision #10.
COMMENT_TYPE_QUOTAS_DEFAULT = {
    "A": (0.35, 0.40),
    "B": (0.22, 0.25),
    "C": (0.14, 0.16),
    "D": (0.09, 0.12),
    "E": (0.07, 0.10),
    "F": (0.00, 0.05),
}

# Comment-type descriptions, embedded into the drafter prompt.
COMMENT_TYPE_DESCRIPTIONS = {
    "A": (
        "Substantive analytical reply. Engage with the post's specifics; offer a "
        "framing or counter-perspective grounded in your own experience. 4-7 sentences."
    ),
    "B": (
        "War story / data point. Drop a concrete number or anecdote from your own "
        "work that pressure-tests the post's claim. 3-5 sentences."
    ),
    "C": (
        "Concise reframe. Summarize the post's core tension in one sharp sentence, "
        "then add a single clarifying observation. 2-3 sentences."
    ),
    "D": (
        "Warm encouragement, but specific. Affirm what's working in the post AND "
        "name a concrete reason it lands. 2-4 sentences."
    ),
    "E": (
        "Pointed question. Ask one specific question whose answer would deepen the "
        "post's argument. Usually one paragraph. 2-3 sentences."
    ),
    "F": (
        "Short and punchy. Single observation, one line. STILL needs 3 sentences "
        "minimum (no one-liners) and 60+ characters."
    ),
}

# Validator constants.
COMMENT_MIN_CHARS = 60
COMMENT_TYPE_F_MIN_SENTENCES = 3
EM_DASH = "—"

# RULE 23 force-abort allowlist (4 reasons exactly per spec).
RULE_23_FORCE_ABORT_REASONS = (
    "floor_breach",
    "cofounder_imbalance",
    "comment_invalid",
    "verified_tuple_missing",
)

# Per-cofounder floor relaxation: each cofounder can run up to 30% under target
# as long as the operator-level floor is met.
COFOUNDER_TARGET_FLOOR_RATIO = 0.7
