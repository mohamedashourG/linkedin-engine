# Pipeline lessons — Edge run analysis (2026-05-11)

Captured after analyzing Edge slate `6a01fba66c4cb8852a0a9097` (force-aborted
after 20 minutes, 635 candidates processed, 2 kept).

Issues are grouped by severity. **Bug** = clearly wrong behavior. **Design
issue** = working as configured but the configuration is suboptimal. **Open
question** = needs operator/product judgment.

---

## 🚨 BUG #1 — Discovery source ordering starves high-precision sources

**Symptom:** With `content_type=documents` filter dropped, Unipile keyword
search returns ~60-250 posts/query. tier_1_kw alone consumes 15+ minutes
of wall-clock. In a 20-minute run we processed 635 keyword candidates but
**0 from `unipile_people`** (RULE 24 server-side-filtered, the highest-
precision source).

In the prior aborted run that completed the keyword sweep, `unipile_people`
produced **359 raw kept** candidates (84% of the slate). By running tier_1_kw
first and exhausting wall-clock, we starve the source that produces the most
ICP-correct results.

**Where:** [discovery.py](backend/app/engine/stages/discovery.py) — sources
run sequentially in the order defined by the `discovery: source order = ...`
log line.

**Fix options:**
1. Reorder: run `unipile_people` (RULE 24) FIRST, then keyword sources. Keeps
   architecture simple; gets the highest-precision candidates in first.
2. Parallelize via Celery `group()` — sources fan out, results stream into
   `candidates` collection. See `COMMENT_LIFECYCLE_ARCHITECTURE.md` and the
   streaming-architecture sketch in the conversation log.
3. Per-source wall-clock caps — `_run_unipile` stops after N minutes even if
   pages remain; lets downstream sources start.

**Severity: HIGH.** Slate quality is determined by which sources actually run.

---

## 🚨 BUG #2 — Per-run profile-fetch budget is not enforced atomically

**Symptom:** Setting was `DISCOVERY_UNIPILE_MAX_PROFILE_FETCHES_PER_RUN=500`,
but actual fresh `unipile_author_cache` writes during the run were **544**
(8.8% overrun). 618 candidates had `enriched_inline=True`.

**Root cause hypothesis:** `fetch_budget_remaining` is a local Python int
initialized at the top of each source function (`_run_unipile`,
`_run_unipile_title_search`, `_run_apidirect`, `_run_exa`). Each source has
its own counter of size `settings.discovery_unipile_max_profile_fetches_per_run`,
so the effective cap is `500 × (number of sources that run)`.

In this run only tier_1_kw ran, but the 544 overrun suggests either
re-counting on cache-miss-retry paths OR a multi-pass that re-initializes the
counter mid-source.

**Where:**
[discovery.py:2237](backend/app/engine/stages/discovery.py:2237) initializes
`fetch_budget_remaining` inside `_run_unipile`. Similar initialization in
each other source function.

**Fix:** Move the counter to an atomic Mongo `$inc` keyed by `slate_run_id`,
read+decrement in a single op:
```
db.slate_runs.find_one_and_update(
    {"_id": slate_run_id, "fetch_budget_remaining": {"$gt": 0}},
    {"$inc": {"fetch_budget_remaining": -1}},
    return_document=AFTER,
)
```
If returns None → budget exhausted globally across all sources.

**Severity: MEDIUM.** At Edge's recall level this can mean 2-3× overspend on
LLM cost downstream if budget-overrun candidates burn enrichment we don't pay
for but still flow into gates.

---

## 🚨 BUG #3 — `slate_runs.total_discovered` is misleadingly 0

**Symptom:** Slate doc shows `total_discovered=0`, `total_verified=0`,
`total_gated=0` even though 635 candidates were inserted into the
`candidates` collection.

**Root cause:** Slate-level counters only update at `_set_stage(...)` calls
(stage boundaries). If a run is aborted mid-discovery (or even just running),
the counter stays at the value from the previous stage transition — which is
0 for new runs.

**User-facing impact:** Gate-funnel UI and operator-summary views read from
`slate_runs.total_*` — so users see "0 discovered" on a slate that has
hundreds of candidates in flight.

**Where:** [daily_run.py:130-242](backend/app/engine/daily_run.py:130).
Counters set during `_set_stage`.

**Fix:** Increment incrementally:
- Option A: `$inc {total_discovered: 1}` on every `db.candidates.insert_one`
  for `status` in `{raw, rejected_inline}`. Pure additive write.
- Option B: Compute on the read side — UI route aggregates from
  `candidates` collection by slate_run_id. Slightly slower but always
  accurate.

Option B is structurally cleaner (slate_runs becomes pure metadata).

**Severity: MEDIUM.** Cosmetic to engine logic but misleading to operators.

---

## 🚨 BUG #4 — Cross-run dedup ledger never populates

**Symptom:** `exhaustion_ledger.count_documents({operator_id: edge})` = **0**
after 4-5 Edge runs today.

`_seen_urls(db, operator_id)` and `_seen_authors_shipped` at
[discovery.py:1525](backend/app/engine/stages/discovery.py:1525) read from
this ledger to prevent re-processing posts and re-shipping to authors. If
the ledger never populates, every run reprocesses everything.

**Hypothesis:** Ledger is populated at slate-seal (RULE 23), and we keep
aborting before that. Need to confirm by running a slate to completion.

**Where:** Population path needs to be traced — grep `exhaustion_ledger`
shows reads but population-time is unclear.

**Fix:** Populate the ledger on:
1. Candidate creation (write post_url + author_url to ledger immediately)
2. Slate seal (current spot, probably)

Option 1 prevents reprocessing within the same day across multiple aborted
runs — important for our current churn pattern.

**Severity: MEDIUM.** Wasted LLM gate cost on every retried run. Becomes
material at scale.

---

## ⚠️ DESIGN ISSUE #1 — Inline rubric is too strict (literal substring only)

**Symptom:** 332 of 633 drops (52%) are `T=0 I=0 G=2 post=0` — US authors
where headline doesn't contain Edge's `target_titles` AS LITERAL SUBSTRINGS.

**Confirmed false-negative — Sanjay Kumar, MD, FACC**
- Headline: "CEO, Access Heart Inc."
- Location: US (G=2 correctly credited)
- Dropped with `T=0 I=0 G=2 post=0`
- Why: "CEO" / "Chief Executive Officer" is NOT in Edge's `target_titles`
  list. "Access Heart Inc." is a small cardiology practice and doesn't match
  any `target_industries` literal ("Hospitals and Health Care", "FQHC",
  "Medical Groups", etc.).
- Verdict: This author is clearly Edge audience material — a healthcare
  CEO. The rubric configuration missed an obvious title, and the small-
  practice industry isn't covered.

Other sample dropped authors who **may** be valid Edge audience members:
- Missy Starowitz — "Disruptive Healthcare Consultant"
- Maggie Lin — "Accounting Professional"
- Healthcare-adjacent consultants without "CHRO/VP/Director" in their headline

**Root cause:** `_score_unipile_author_against_operator` at
[discovery.py:544](backend/app/engine/stages/discovery.py:544) uses
`_rubric_substring_match` — literal substring against `target_titles` /
`target_industries`. No semantic understanding, no partial credit.

**Trade-off:**
- Current: ~$0 inline cost, 0.3% pass rate. High precision, possibly low recall.
- Alternative: lower thresholds OR add partial credit OR let LLM decide
  borderline cases.

**Three fix options:**
1. **Lower thresholds** — Path A from ≥6 to ≥4 (geo + partial industry/title).
   Lets borderline cases through to LLM gates. Cost: ~$1-3/run more in LLM
   cost. Recall gain: significant.
2. **Soft signals** — partial credit when headline contains topical words
   even without exact title match. ("Healthcare Consultant" → industry=1).
3. **Best**: use inline rubric only as a buyer-vs-seller filter. Drop only on
   `G=0` (high-confidence geo) and on clear-non-buyer signals (e.g. "Account
   Executive" / "Sales Manager"). Let LLM ICP scoring handle title/industry
   fit on US healthcare-adjacent candidates.

**Severity: MEDIUM** for narrow-ICP operators like Edge (small unique-buyer
count; aggressive precision filter may zero out valid candidates).

---

## ⚠️ DESIGN ISSUE #2 — Path B (post relevance) is dead for Edge

**Symptom:** All 635 candidates this run had `post=0`. The post-relevance
path never fires.

**Root cause:** Edge's tier_1/2/3 keyword phrases are very specific:
- "hospital workforce shortage"
- "front-end RCM denials"
- "nurse turnover rate hospital"
- "RCM efficiency hospital"

These multi-word phrases rarely appear as literal substrings in post bodies.
LinkedIn posts use natural language ("we're seeing severe staffing pressure"
not "we have a hospital workforce shortage").

**Effect:** Path B is theoretical — in practice every passing candidate goes
through Path A (author rubric). This eliminates one of the two qualification
paths and pushes all weight onto the title/headline match.

**Fix options:**
1. Break tier keywords into single-word components for the post-relevance
   scorer (e.g. "hospital workforce shortage" → scoring on individual words).
2. Add stem-aware matching (workforce, staffing → workforce_synonyms).
3. Accept Path B as dead for narrow-ICP operators and rely on Path A only.

**Severity: MEDIUM.** Cuts one of two intended qualification paths.

---

## ⚠️ DESIGN ISSUE #3 — Dead `inline_rubric` field on candidate docs

**Symptom:** Persisted `candidates` docs have two parallel fields:
- `inline_rubric` — always `None`
- `unipile_rubric` — has the actual rubric data

The drafter and gates read from `unipile_rubric`. `inline_rubric` is dead.

**Where:** Two assignments around
[discovery.py:2326](backend/app/engine/stages/discovery.py:2326).

**Fix:** Remove the dead `inline_rubric` initialization and pass; keep only
`unipile_rubric`.

**Severity: LOW.** Cosmetic, confusing for future debuggers.

---

## ⚠️ DESIGN ISSUE #4 — Discovery stage counters mix concepts

`slate_runs` has `total_discovered`, `total_verified`, `total_gated`,
`total_drafted`, `total_slated` — but discovery itself fans out into 5
sub-sources, each with its own success/drop pattern, and we don't track
per-source counts at the slate level.

**Effect:** Operators looking at the UI can't see "Unipile found 595, Exa
found 50, RULE 24 found 359" — they just see one aggregate number.

**Fix:** Add `slate_runs.per_source_counts: { tier_1_kw, tier_2_kw,
unipile_people, exa_kw, apidirect_kw, ... }` — populated via `$inc` per
candidate insert.

**Severity: LOW.** Observability, not correctness.

---

## ❓ OPEN QUESTION — Rubric tuning for Edge

For both kept candidates (Denise Corbisiero, Adelajda Tego):
- Headlines contain "Revenue Cycle Management Expert" / "Healthcare
  Operations & Strategy"
- They scored `industry=0` because Edge's `target_industries` is
  company-type-specific (`"Hospitals and Health Care"`, `"FQHC"`, etc.)
  — these consultants don't have "Hospital X" in their headline.

**Question for the operator:** Should solo consultants and fractional execs
in the healthcare ops space be in scope, or do we only want people who work
AT health systems / hospitals / medical groups?

If in scope: add `"Healthcare Operations"`, `"Revenue Cycle Management"`,
`"Healthcare Consulting"`, `"Health System Advisor"` etc. to
`target_industries`. Industry rubric will then credit them.

If out of scope: current behavior is correct, and the rubric is doing its
job. No code change needed.

---

## ❓ OPEN QUESTION — Profile-fetch budget right-sizing

Current 500 cap is enforced loosely (BUG #2 above). Even if enforced
correctly, is 500 the right number?

With content_type filter dropped:
- ~50 unique queries × ~60 posts/query = ~3000 posts/run
- ~30-50% unique new authors = ~1000-1500 unique enrichments needed/run

500 cap means we miss enrichment on ~500-1000 authors per run. They flow
through without geo verification (back to LLM-from-post-text).

**Trade-offs:**
- Higher cap: more enrichment, fewer "no_profile" drops, more $0 cost (Unipile
  profile fetches are free). Risk: rate-limit 429s, slower runs.
- Lower cap: faster runs, may miss geo verification on tail candidates.

Right answer depends on Unipile's actual rate limit (we haven't probed it).
**Recommend setting to 1500 and observing 429 rate over a week.**

---

## Build order (after current run priorities)

1. **Fix BUG #1 (source ordering)** — reorder OR parallelize. Without this,
   slate quality is throttled by discovery wall-clock.
2. **Fix BUG #2 (atomic budget counter)** — small refactor, prevents
   surprise overspend.
3. **Fix BUG #4 (exhaustion_ledger population)** — populate on candidate
   creation, not just slate-seal. Avoids reprocessing on every aborted run.
4. **Fix BUG #3 (live counter on slate_runs)** — switch to per-insert `$inc`
   or compute on read.
5. **DESIGN ISSUE #1 (rubric strictness)** — discuss with operator first;
   may be acceptable as-is for very narrow ICPs.
6. **DESIGN ISSUE #2 (Path B dead)** — keyword tuning conversation.
7. **DESIGN ISSUE #3+#4 (cosmetic)** — bundle into a cleanup pass.

---

## What's NOT a problem (working as designed)

- ✅ Geo resolver: 299/299 sample drops were genuine non-US.
- ✅ Inline rubric math: scores match the displayed drop_reason strings exactly.
- ✅ Path A correctly catches ICP fits (the 2 kept candidates).
- ✅ Content-type-filter removal: per-query recall jumped ~50×.
- ✅ Inline drop savings: 633 candidates that would otherwise hit LLM gates
  ≈ $1.50-3 of LLM credits saved.

---

## UI categorization gap: `rejected_inline` is invisible (NEW, 2026-05-13)

### What the user sees

On the `/runs/[id]` pipeline view, the funnel collapses in a confusing way:

- **Verification → pass:** large number (e.g. 287 of 287)
- **Verification → fail:** tiny
- **Gates → pass:** 1
- **Gates → fail:** 31
- **"Missing" between Verification and Gates:** ~255 candidates with no visible bucket

### What's actually happening

There are TWO filters between Verification and the 4 LLM gates that the UI
doesn't render:

1. **Inline rubric** runs immediately after Crustdata/Unipile-fallback author
   enrichment, BEFORE any LLM call. Drops authors whose title/industry/geography
   rubric score has no qualifying path.
2. Candidates that fail are stored as `status='rejected_inline'` with
   `drop_reason='inline_geo: ...'` or `'inline_rubric: T=x I=y G=z post=w → no path'`.

`backend/app/routes/slate_pipeline.py::compute_pipeline_breakdown` then
categorizes:

- `verification.pass` = everything NOT in `rejected_url_mismatch`
- `gates.pass/fail/pending` = only statuses in
  `{gate_passed, allocated, drafted, slated, shipped, dropped_by_user,
  gate_dropped, verified, cheap_gate_passed}`

`rejected_inline` matches NEITHER — so the inline-rubric drops:

- Inflate `verification.pass` (they SHOULD be a separate stage)
- Vanish entirely from the gates column

### Concrete example (Cardiowell keyword-only run `e5db6e23`, 2026-05-13)

Of 151 candidates from the first 4 tier_1 keywords:

| Bucket | Count | Status / drop_reason |
|---|---|---|
| Inline geo (non-US) | 107 | `rejected_inline` / `inline_geo: geo_not_in_author_location` |
| Inline rubric T=0 I=0 | 24 | `rejected_inline` / `inline_rubric: T=0 I=0 G=2 post=*` |
| LLM gates rejected (vendor/competitor) | 17 | `gate_dropped` / `non_buyer: vendor pitch ...` |
| Reshare | 1 | `rejected_url_mismatch` / `is_repost: ...` |
| icp_low | 1 | `gate_dropped` / `icp_low: score=4 < 6` |
| Drafted ✓ | 1 | `drafted` |

The UI shows ver_pass=150, gates_fail=17, gates_pass=1 — leaving the operator
wondering where 131 candidates went.

### Fix options (deferred per operator decision on 2026-05-13)

Three viable patches, listed by preference:

1. **Add an "Inline rubric" pipeline stage** between Verification and Gates in
   `compute_pipeline_breakdown` and the frontend pipeline view.
   `rejected_inline` candidates land in this new bucket.
2. **Fold `rejected_inline` into verification.fail** with a sub-label. Simpler
   but loses the geo-vs-rubric split.
3. **Fold `rejected_inline` into gates.fail** with drop_reason prefix
   `inline_*`. Mixes pre-gate and post-gate rejections.

Decision: leave as-is for now, document instead. The underlying Mongo state is
correct; only the UI is misleading.

### Operator quick-check until the UI fix lands

```
docker exec infra-backend-1 python -c "
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings
from bson import ObjectId
async def m():
    c = AsyncIOMotorClient(settings.mongodb_uri); db = c[settings.mongodb_db]
    sid = ObjectId('<your_slate_id>')
    n   = await db.candidates.count_documents({'slate_run_id': sid, 'status': 'rejected_inline'})
    geo = await db.candidates.count_documents({'slate_run_id': sid, 'drop_reason': {'\$regex':'^inline_geo'}})
    rub = await db.candidates.count_documents({'slate_run_id': sid, 'drop_reason': {'\$regex':'^inline_rubric'}})
    print(f'rejected_inline total: {n}  (geo={geo}  rubric={rub})')
    c.close()
asyncio.run(m())
"
```
