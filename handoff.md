# Session handoff — LinkedIn engagement engine

A fresh agent can pick this up cold. Project root: `/Users/ashour/Desktop/glink/linkedin-engine/`.

---

## Context

LinkedIn engagement engine that runs daily discovery → verification → 4-gate funnel → drafter → Rule 23 seal → email. Today's session focused on the active operator `aachourmohameddeveloper@gmail.com` (operator_id `69ffbf195dfdf591a3d1e6fc`) — a healthcare RCM ICP (revenue cycle, patient access, billing). Active cofounder `mohamed` (id `69ffc0225dfdf591a3d1e6fd`) is connected to Unipile via `unipile_account_id`. Operator is tagged with `client_slug=glnk` whose `configs/glnk/keyword_pools.json:title_industry` pool is unfortunately pharma-themed — see "Open items" below.

Stack: Docker Compose (`infra/docker-compose.yml`). Backend = FastAPI in `infra-backend-1`, Celery worker in `infra-worker-1`, Mongo in `infra-mongo-1`, Redis in `infra-redis-1`, Beat in `infra-beat-1`. LLM is **Azure OpenAI** (`gpt-5.4` primary, `gpt-4.1` cheap), NOT direct OpenAI.

---

## What landed today

Each item is shipped, restarted, and verified in a real run. File:lineish references are stable.

1. **Contact-import recovery** — `POST /api/contacts/bulk` and `/upload` now return the existing group when all contacts are duplicates (so the frontend can drill into it). Frontend `contacts/page.tsx` shows `"All N contacts already exist in <group>"` instead of empty `group: null`.
2. **Bright Data scaffolding** — `services/brightdata.py` exists with submit/poll helpers but is **NOT wired into discovery** (`DISCOVERY_USE_BRIGHTDATA=false`). Their keyword-discoverable LinkedIn dataset is jobs-only; not useful for post discovery.
3. **Unipile additions** — `services/unipile.py` got: `get_post(post_id_or_url)`, `extract_post_id_from_url(...)`, `search_posts_pages` cursor-pagination, `search_parameter_ids` (id resolver for LOCATION / INDUSTRY / etc.), `search_posts_by_url` (URL-paste mode). The earlier `search_posts_by_filters` was removed today — see #11 below.
4. **Crustdata key fix** — `.env` `CRUSTDATA_API_KEY` was `ccd_...` (typo), now `cd_60ace1e452e0f352b46e315d683706ad7ec1`. `list_watches` no longer raises on 404 (treats it as empty list).
5. **Crustdata-first author enrichment** — `_run_unipile` keyword path now runs a 4-pass flow per query: (1) cache lookup by `provider_id` OR `public_identifier`, (2) Crustdata Person Enrich batch for misses, (3) Unipile `/users/{slug}` fallback capped at 50/run, (4) score + qualify. Cache is `unipile_author_cache`. URN-form slugs (`ACoAA...`) bypass Crustdata to avoid wrong-person matches. Author slug is **derived from the post URL** (`/posts/<slug>_...-activity-...`) when Unipile's search payload omits the author URL.
6. **`inline_icp_qualified` trust path** — When the inline rubric's Path A passes (author title + industry score), the candidate doc gets `inline_icp_qualified=True`. Downstream LLM gates are relaxed for it: `non_buyer` drops are SPARED (logged but not enforced), `icp_scoring` is SKIPPED entirely (no LLM call). Saves ~1 LLM call per qualified candidate; also recovers clear-ICP execs whose post text reads as commentary, not buyer-speak.
7. **Keyword no-repeat ledger removed entirely** — Deleted `engine/keyword_history.py`, all 9 callsites in `discovery.py`, the `discovery_keyword_history_enabled` + `keyword_history_lookback_days` settings, the database index, and three tests. Same keyword can run every day now.
8. **Geo dropped from inline rubric score** — `_score_unipile_author_against_operator` returns `total = title + industry` (no `+ geo`). The `geo` field is still surfaced on the result dict because `_qualifies_inline_rubric` reads it for the geo-required gate. Net effect: geo is binary (US or not), not scored.
9. **Allocator over-allocation 2×** — `_run_allocator` picks `daily_volume_target × settings.allocator_target_multiplier` (default 2.0). Per-cofounder summary now records `base_target` and `effective_target`. Rule 23's "floor" check stays anchored on `base_target × 0.7` so the alert doesn't inflate.
10. **Drafter validator-feedback retry loop** — `_draft_one` retries up to `DRAFTER_VALIDATOR_FEEDBACK_RETRIES=3` times. Each retry passes the prior validator failure reason (`buzzword:'leverage'`, `no_specific_number_or_cohort`, etc.) back into the drafter's system prompt as a hard "do not do X" directive via `drafter.draft_comment(feedback_hint=...)`. Reasons mapped: buzzword, specificity, banned_opener, banned_token, sentence_count high/low, too_short, too_long. Persists `drafter_attempts` count on the candidate.
11. **Filter-only Unipile pass removed** — was added earlier in the session as `search_posts_by_filters` + `filter_only_kw` source. Produced 0 slated in one full run (BPO/staffing document content, all geo-dropped). Service helper, discovery wiring, 5 config fields, 5 env entries — all deleted.
12. **Past-runs viewer** — New backend routes `GET /api/slate/runs` (paginated list via `before` cursor) and `GET /api/slate/runs/{slate_run_id}` (full detail: source×status matrix, top 20 drop reasons, drafted candidates, cofounders). New frontend pages `(dashboard)/runs/page.tsx` (table) and `(dashboard)/runs/[slate_run_id]/page.tsx` (detail with stacked funnel + tables). Sidebar link "Past runs" added between Today's slate and Replies.

---

## Current `.env` state — key knobs

```
# Enrichment
DISCOVERY_UNIPILE_ENRICHMENT_STRATEGY=crustdata_first
DISCOVERY_UNIPILE_FALLBACK_MAX_FETCHES_PER_RUN=50
DISCOVERY_UNIPILE_CRUSTDATA_BATCH_SIZE=25
DISCOVERY_UNIPILE_AUTHOR_CACHE_TTL_DAYS=14

# Inline rubric
DISCOVERY_UNIPILE_INLINE_RUBRIC_ENABLED=true
DISCOVERY_UNIPILE_INLINE_PATH_A_THRESHOLD=2
DISCOVERY_UNIPILE_INLINE_PATH_B_THRESHOLD=999   # Path B effectively off
DISCOVERY_UNIPILE_INLINE_REQUIRE_GEO=true

# Gate relaxation
GATES_NON_BUYER_SPARE_INLINE_ICP=true
GATES_ICP_SCORING_SPARE_INLINE_ICP=true

# Allocator
ALLOCATOR_TARGET_MULTIPLIER=2.0

# Drafter
DRAFTER_VALIDATOR_FEEDBACK_RETRIES=3

# Sources turned OFF
DISCOVERY_USE_CRUSTDATA_SCREENER=false     # account 403s
DISCOVERY_USE_BRIGHTDATA=false             # jobs-only, not useful
DISCOVERY_EXHAUSTION_LEDGER_ENABLED=false  # disabled during testing
```

Active LLM creds: `AZURE_OPENAI_KEY` set, `OPENAI_API_KEY` empty. Crustdata key is the `cd_...` form.

---

## Database state — what's been wiped

Earlier today we explicitly wiped, with user confirmation:
- `db.candidates.deleteMany({operator_id: ObjectId("69ffbf195dfdf591a3d1e6fc")})` — 14,915 rows
- `db.exhaustion_ledger.deleteMany({operator_id: ...})` — 4,837 rows

NOT wiped (kept warm):
- `unipile_author_cache` — ~850+ rows from prior runs. Crustdata + Unipile profile enrichment hits cache aggressively.
- `slate_runs` — historical runs still present. Their candidate references are now dangling (cosmetic only — past-runs UI may render empty rows for very old slates).
- `users`, `cofounders`, `discovery_seeds` (contacts), `voice_profile`, `icp_rubric`, `our_comments`, `audit_records` — all untouched.

---

## Open items / unfinished business

Each is concrete enough that a fresh agent could pick it up:

1. **glnk client_config / RCM operator mismatch.** `configs/glnk/keyword_pools.json` has pharma title_industry phrases (`"VP Commercial pharma"`, `"Chief Medical Officer biotech"`, etc.) but the active operator's ICP is healthcare RCM. RULE 24 title-search and the `title_industry_kw` keyword search both burn LinkedIn-account quota on the wrong audience. Fix: either rewrite `configs/glnk/keyword_pools.json:title_industry` to RCM phrasing (`"VP Revenue Cycle"`, `"CFO health system"`, `"Director Patient Access"`), or set `operator.product_extracted.title_industry` to override per-operator.

2. **Path A threshold may now under-qualify.** With geo dropped from the score (item #8 above) and `DISCOVERY_UNIPILE_INLINE_PATH_A_THRESHOLD=2`, an author needs at least `industry=3` to clear Path A (`title=5` also works, geo-only no longer). If next run shows too many `[DROP/unipile] ← rubric T=0 I=0 G=2 post=0 → no path` lines, drop the threshold to 0 or 1.

3. **Vendor/recruiter exclusion in inline rubric.** Many `[non_buyer/spared]` log lines show "vendor pitch (selling RCM services into operators)" or "recruiter posting jobs". The rubric flags them as ICP because their title matches (`VP RCM at staffing vendor`), but they're competitors, not buyers. Fix: add a vendor-company exclusion check in `_score_unipile_author_against_operator` that flips `inline_icp_qualified` to False when `author.company` matches a known vendor/staffing/recruiter pattern.

4. **Exa API key 401.** `EXA_API_KEY` is invalid. Either rotate the key or accept Exa as a dead source.

5. **Crustdata screener 403.** `POST /screener/linkedin_posts/keyword_search/` returns 403 "You do not have access to this API". The account doesn't have screener access (same gating issue as the Watcher API was, before that got enabled). Either contact Crustdata support or leave `DISCOVERY_USE_CRUSTDATA_SCREENER=false`.

6. **Streaming verification (Nicolas's proposal).** Idea: discover posts in batches, verify each batch immediately, stop discovery once N posts verify. Saves LLM cost on subsequent gates. Discussed during this session but not implemented — the user dismissed the AskUserQuestion follow-up. If a future session picks this up, the implementation paths are: (a) interleave verify per discovery wave, (b) cap discovery candidates and verify batch, (c) tune the existing 3-round top-up loop.

7. **engagement_poller 500/429** — separate periodic task hits Unipile `/posts/comments` and gets 500/429 every minute. Pre-existing; not in the daily_run path. Cosmetic noise in worker logs.

8. **per_cofounder_counts UI surface.** Allocator now writes `base_target` and `effective_target` to the per-cofounder summary, but the today's-slate UI doesn't display this yet. Quick win to show "allocated 18 of 20 effective (target 10, overage 2×)" so the operator knows what's happening.

---

## How to run / verify

```bash
# Bring stack up
cd /Users/ashour/Desktop/glink/linkedin-engine/infra
docker compose up -d

# Restart only backend + worker after code changes (don't need to bounce mongo/redis)
docker compose restart backend worker

# Trigger a daily_run for the active operator
docker exec infra-worker-1 python -c "
from app.celery_app import celery_app
celery_app.send_task('engine.daily_run', kwargs={'operator_id': '69ffbf195dfdf591a3d1e6fc'})
"

# Watch worker logs (tight filter, excludes per-minute cron + engagement-poller noise)
docker logs -f infra-worker-1 2>&1 | grep -v -E "dispatch_daily_runs|dispatch_nightly_runs|HTTP Request: GET|HTTP Request: POST|engagement_poller|/posts/comments" | grep -E --line-buffered "Traceback|ERROR|CRITICAL|daily_run sealed|crustdata-enrich\] requested|unipile-enrich\] strategy|icp/spared|non_buyer/spared|\[verify\]|\[cheap_gates\]|\[exp_gates|\[drafter\]"

# Inspect a slate run's funnel
docker exec infra-mongo-1 mongosh --quiet linkedin_engine --eval '
const opId = ObjectId("69ffbf195dfdf591a3d1e6fc");
const slate = db.slate_runs.find({operator_id: opId}).sort({created_at: -1}).limit(1).toArray()[0];
print("slate:", slate._id, "sealed:", slate.sealed_at, "slated:", slate.total_slated);
db.candidates.aggregate([
  {$match: {slate_run_id: slate._id}},
  {$group: {_id: {source: "$source", status: "$status"}, count: {$sum: 1}}},
  {$sort: {count: -1}}
]).forEach(r => print("  " + r._id.source + " / " + r._id.status + " ×" + r.count));
'

# Past-runs API smoke test (will 401 without cookie — use the frontend at http://localhost:3000/runs)
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/slate/runs
```

---

## Test the recent fixes — what to look for in the next run

Trigger a fresh daily_run and grep the worker logs for these signals:

| Change | Log line to confirm |
|---|---|
| Crustdata-first | `[crustdata-enrich] requested=N matched=M` (multiple per run); `[unipile-enrich] strategy=crustdata_first sources: cache_hit=… crustdata_matched=… unipile_fallback=…` (one per cofounder) |
| Inline-ICP relaxation | `[non_buyer/spared] icp-qualified author kept despite verdict: …`; `[icp/spared] inline-icp-qualified author passes without LLM ICP scoring` |
| Allocator overage | `allocator: cofounder=… base_target=10 × 2.00 = effective=20`; `slate_runs.per_cofounder_counts.<cf_id>.{base_target, effective_target}` populated |
| Drafter retry loop | `[drafter]     candidate=… attempt=N/3 failed: <reason> — retrying with feedback`; `candidate=… recovered on attempt N` |
| Geo gate intact | Still see `[DROP/unipile] … ← geo_not_in_author_location` for non-US authors |

If anything looks off, the `/runs/{slate_run_id}` UI page now has the full funnel breakdown — start there.

---

## Plan file

Previous plan for the most recent batch of changes (over-allocate, drop geo from ICP, remove filter-only, past-runs viewer, drafter retry) is saved at:

```
/Users/ashour/.claude/plans/toasty-bouncing-noodle.md
```

It documents the design decisions and verification steps in detail. Earlier plans in that file are overwritten — the current one covers the four landing items.
