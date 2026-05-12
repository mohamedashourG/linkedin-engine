**Subject:** linkedin-engine — open issues & next-up work (post Edge-run audit)

Hi —

Just pushed the current state of the engine to `main` —
commit `a267ddb` on `github.com/mohamedashourG/linkedin-engine`. Below is
everything we noted as "to fix" or "next-up" during today's Edge-run audit,
in priority order. Full context lives in two committed markdown docs at
the repo root:

- `linkedin-engine/LESSONS_TO_FIX.md` — bugs and design issues surfaced by
  the Edge run analysis (slate `6a01fba66c4cb8852a0a9097`)
- `linkedin-engine/COMMENT_LIFECYCLE_ARCHITECTURE.md` — architecture sketch
  for sending + tracking our outbound comments and inbound engagement

---

## What just shipped in this push

1. **`backend/app/services/geo_resolver.py`** — offline geonamescache-based
   `is_us_location()`. Replaces the 87-term hand-curated substring rubric
   with a 2,752-city + 51-state US dataset. Validated 30/30 on a challenge
   suite and 87/87 against the original curated list.
2. **Geo gate wiring** in `_score_unipile_author_against_operator` and
   `_run_apidirect` — both keyword paths now use the resolver instead of
   substring matching against `target_geographies`.
3. **RULE 24 promoted to discovery source #1** —
   `_run_unipile_title_search` (server-side LOCATION + INDUSTRY + degree
   filter) now runs before the broader keyword sweep. Highest-precision
   source gets discovery wall-clock first.
4. **`.env` config corrections**:
   - `DISCOVERY_UNIPILE_POST_CONTENT_TYPE=` (was `documents` — was filtering
     to only PDF/slide-deck posts, ~50× recall hit)
   - `DISCOVERY_UNIPILE_MAX_PROFILE_FETCHES_PER_RUN=500` (was 300)
   - `DISCOVERY_UNIPILE_INLINE_PATH_A_THRESHOLD=2` (was 6 — regex T/I rubric
     was dropping CEOs of small practices like Sanjay Kumar; geo alone now
     passes Path A, LLM ICP gate handles title/industry semantically)
   - `DISCOVERY_UNIPILE_INLINE_PATH_B_THRESHOLD=999` (effectively disables
     the post-relevance regex path — Edge's tier_1/2/3 phrases never
     literal-match anyway)
5. **`backend/requirements.txt`** — added `geonamescache==3.0.1`.

---

## Bugs to fix (priority order — see `LESSONS_TO_FIX.md`)

### 🚨 BUG #1 — Discovery sources run sequentially; long ones starve later ones
- After dropping the `content_type=documents` filter, tier_1_kw alone now
  eats 15+ min of wall-clock at Edge's keyword recall.
- Partial mitigation already shipped (RULE 24 first). Full fix needs source
  parallelization via Celery `group()` or per-source wall-clock caps.
- See `LESSONS_TO_FIX.md` § BUG #1 and the streaming-architecture sketch
  in `COMMENT_LIFECYCLE_ARCHITECTURE.md`.

### 🚨 BUG #2 — Profile-fetch budget is not atomic across sources
- Each `_run_*` source function has its own local `fetch_budget_remaining`
  counter initialized from settings. Effective cap is
  `settings.discovery_unipile_max_profile_fetches_per_run × N_sources`.
- Last run had 544 fresh `unipile_author_cache` writes vs 500 cap (8.8%
  overrun).
- Fix: move to atomic Mongo `$inc` keyed by `slate_run_id`, shared across
  all sources. See `LESSONS_TO_FIX.md` § BUG #2 for the snippet.

### 🚨 BUG #3 — `slate_runs.total_discovered` stays at 0 mid-run
- Counters only update at `_set_stage(...)` boundaries. UI gate-funnel reads
  from `slate_runs.total_*` → operators see "0 discovered" on slates with
  hundreds of in-flight candidates.
- Fix: either `$inc` on every `candidates.insert_one`, or compute from
  `candidates` aggregate on the read side.

### 🚨 BUG #4 — `exhaustion_ledger` cross-run dedup is empty
- 0 entries for Edge after 4-5 runs today. Likely only populates on slate-
  seal (RULE 23), and we kept aborting before that.
- Fix: populate at candidate creation (`post_url + author_url`), not just at
  slate-seal. Avoids reprocessing in retried runs.

### ⚠️ DESIGN ISSUE — Inline rubric regex is too brittle
- Already partially worked around in this push (Path A threshold lowered to
  2, lets LLM decide title/industry fit on US authors).
- Long-term: keep this as the default, drop the dead `inline_rubric` field
  from candidate docs (two parallel fields persisted — `inline_rubric` is
  always `None`, `unipile_rubric` has real data).

### ⚠️ DESIGN ISSUE — Path B (post relevance) is effectively dead
- All 635 candidates last run scored `post=0`. Edge's tier_1/2/3 phrases
  ("hospital workforce shortage", "front-end RCM denials") are too specific
  to literal-match LinkedIn post bodies.
- Options: break tier phrases into stem components, add synonym tables, or
  accept Path B as dead for narrow-ICP operators.

---

## New build: comment lifecycle tracking

Full spec in `COMMENT_LIFECYCLE_ARCHITECTURE.md`. Summary of what's needed
to close the loop on sending comments and tracking what comes back:

### Phase 1 — minimum viable lifecycle (~2 days)
1. Schema migrations: 4 new Mongo collections (`our_comments`,
   `comment_engagement_snapshots`, `parent_post_snapshots`,
   `cofounder_send_quotas`) + extension on existing `replies`.
2. Unipile parser extension: `UnipilePost` dataclass + `_parse_unipile_post`
   already get `reaction_counter`, `comment_counter`, `repost_counter`,
   `is_repost`, `permissions.can_post_comments` inline in keyword search
   payload — we currently discard all of it. ~20 lines to extract.
3. `unipile.post_comment(account_id, post_url, text, parent_comment_id)` —
   wraps `POST /posts/{post_id}/comments`. Returns the new comment URN.
4. `outbox.enqueue_comment` + `process_outbox` Celery beat task — drain
   queued sends, respect per-cofounder quota, idempotent on
   `(candidate_id, parent_comment_id)`.
5. `engagement_poller.poll_our_comments_and_replies` (merge with existing
   `reply_monitor` — both call `get_post_comments`, single pass for both
   concerns). Daily beat + 30-min "hot" beat for first 48h after send.

### Phase 2 — auto-reply-back + UI (~1-2 days)
6. Reply-back loop: `reply_drafter` (already exists, produces
   `PUBLIC_REPLY_BACK` text) wired to enqueue via `outbox` instead of
   writing to Mongo for manual review. Gated behind per-operator config
   flag `auto_send_replies_enabled: bool = false` by default — opt-in.
7. HTTP routes:
   `/api/analytics/comments`,
   `/api/analytics/comments/{comment_id}/timeseries`,
   `/api/analytics/operator-summary`,
   `/api/analytics/post-thread/{candidate_id}`.
8. UI tiles: comment performance table + comment time-series chart.

### Phase 3 — optional APIDirect emoji breakdown (~2h)
9. Extend `apidirect.LinkedInPostDetails` with `likes/comments/shares/
   reactions_by_type` fields. APIDirect's `/v1/linkedin/post` returns the
   per-emoji breakdown (`appreciation/empathy/like/insight/praise/...`) —
   the only place this is structured. Config-gated, off by default.

### Important constraint
**Impressions on our comments are NOT available via any API.** LinkedIn
only surfaces them to the post owner via Creator Analytics. Best we can
track is reactions + replies + parent-post-engagement context.

---

## Open product/operator questions

1. **Rubric tuning for Edge**: should solo healthcare consultants /
   fractional execs be in scope, or only people who work AT health systems?
   Currently `target_industries` is company-type-specific (`"Hospitals and
   Health Care"`, `"FQHC"`, `"Medical Groups"`). Consultants score
   `industry=0` even when "Healthcare Operations" / "RCM Expert" is in
   their headline. See `LESSONS_TO_FIX.md` open questions.
2. **Auto-reply-back default**: off seems right initially. Engine's value
   drops sharply if every reply needs human approval. Sensible defaults?
3. **Conversation cascade depth**: LinkedIn flattens replies-to-replies.
   Should we cap our participation at 2-3 turns to avoid looking like a
   bot?
4. **Profile-fetch budget right-sizing**: current cap (now 500) is
   probably still too low at the new content_type-free recall level
   (~3000 posts/run, ~1000-1500 unique new authors). Recommend bumping to
   1500 and watching the 429 rate.

---

Happy to walk through any of this. Both markdown docs are the source of
truth — the email is a digest.

— Nicolas
