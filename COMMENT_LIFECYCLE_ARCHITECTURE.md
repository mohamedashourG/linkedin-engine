# Comment Lifecycle Tracking — Architecture

End-to-end tracking for everything the engine sends to LinkedIn (comments and
replies) and everything it receives back (reactions, replies-to-our-comments,
parent-post engagement context).

Status: **design only, no code written yet**.

---

## 1. Vendor role split

Both Unipile and APIDirect are usable. Empirically verified via raw-response
probes — Unipile's keyword-search payload already contains engagement counters,
so APIDirect's role is narrower than initially scoped.

| Data | Vendor | Cost | Notes |
|---|---|---|---|
| Post a comment / reply | **Unipile** | free at rate-limit ceiling | Only Unipile has a write endpoint |
| Total reactions / comments / reposts on a post | **Unipile** (inline in keyword search) OR **Unipile `get_post`** (refresh) | free | Already in every keyword search response — we currently discard it |
| `can_post_comments` flag | **Unipile** | free | LinkedIn lets authors disable comments; this gates whether queueing is even valid |
| Per-comment reactions / reply counts | **Unipile `/posts/comments`** | free | Comment-list payload includes per-comment counters |
| Comment text + commenter identity | **Unipile `/posts/comments`** | free | APIDirect does not return comment bodies |
| Reactions breakdown by emoji type | **APIDirect** | $0.002/call | The only place this is structured: `{ appreciation, empathy, entertainment, interest, like, praise }` |
| Author location verification | **Unipile `/users/{slug}`** (cached 14d) | free, budget-limited | Used by the existing geo_resolver gate |
| Author follower count | Neither directly | — | Out of scope for now |
| Impressions on a post | None | — | LinkedIn never exposes this to non-owners |

**Default for engagement stats: Unipile.** APIDirect is reserved for two narrow
roles: (a) emoji-breakdown sentiment, (b) failover when Unipile is rate-limited.

---

## 2. Database schema

### 2.1 `our_comments` (NEW — primary outbound ledger)

One row per comment we send (initial OR reply). Never deleted.

```
{
  _id: ObjectId,
  comment_id: str,              # Unipile/LinkedIn URN, unique
  parent_comment_id: str | null, # null = top-level, set = reply

  # Where this lives in LinkedIn
  parent_post_url: str,
  parent_post_id: str,           # LinkedIn activity URN
  parent_post_author_provider_id: str,

  # Who sent it
  operator_id: ObjectId,
  cofounder_id: ObjectId,
  unipile_account_id: str,
  candidate_id: ObjectId,        # source candidate we commented on
  slate_run_id: ObjectId,

  # What we said
  text: str,
  context: "INITIAL_COMMENT" | "PUBLIC_REPLY_BACK" | "PUBLIC_REPLY_CASCADE",
  posted_at: datetime,
  posted_via: "auto" | "human_approved",

  # Send pipeline
  send_attempts: [{ attempted_at, success, error_msg }],
  status: "queued" | "sent" | "failed" | "deleted",
  failure_reason: str | null,

  # Parent-post context captured at send time (Unipile inline payload).
  # These fields are IMMUTABLE after first write — the time-series version
  # lives in parent_post_snapshots.
  parent_post_reaction_count_at_send: int,
  parent_post_comment_count_at_send: int,
  parent_post_repost_count_at_send: int,

  # Latest engagement on OUR comment (refreshed by beat task)
  latest_reaction_count: int,
  latest_reactions_by_type: { LIKE, INSIGHT, ... },  # only set when APIDirect breakdown enabled
  latest_reply_count: int,
  latest_polled_at: datetime,
}
```

**Indices:**
- `comment_id` (unique)
- `(candidate_id, parent_comment_id)` unique (idempotency — prevents
  double-posting on the same target)
- `(operator_id, posted_at)` — for analytics queries
- `(parent_comment_id)` — reply-chain lookup
- `(parent_post_url, posted_at)` — "what did we say on this post"
- `(status, latest_polled_at)` — beat task picks stale rows

---

### 2.2 `comment_engagement_snapshots` (NEW — per-our-comment time-series)

One row per poll cycle per our_comment. Drives the "engagement over time"
chart and lets the system detect velocity inflections.

```
{
  _id: ObjectId,
  comment_id: str,                   # joins to our_comments.comment_id
  polled_at: datetime,
  reaction_count: int,
  reactions_by_type: { ... } | null, # only when APIDirect enabled
  reply_count: int,
}
```

**Index:** `(comment_id, polled_at)` compound. **TTL-archive** after 90d to a
cold collection.

---

### 2.3 `parent_post_snapshots` (NEW — per-parent-post time-series)

One row per poll per unique parent_post_url. Separate from
`comment_engagement_snapshots` because a single parent post can carry many of
our comments (across cofounders / slate runs) and we want one snapshot per
poll cycle per post, not per (post × our-comment).

```
{
  _id: ObjectId,
  parent_post_url: str,
  parent_post_id: str,
  polled_at: datetime,
  source: "unipile" | "apidirect",   # which vendor produced the snapshot
  reaction_count: int,
  comment_count: int,
  repost_count: int,
  reactions_by_type: { ... } | null, # only when source=apidirect
  is_repost: bool,
}
```

**Index:** `(parent_post_url, polled_at)` compound. **TTL-archive** after 90d.

---

### 2.4 `replies` (EXISTING — extended)

The existing collection already tracks inbound replies (`reply_monitor.py`
writes here). Three additions wire it into the new lifecycle model:

```
{
  ...existing fields (candidate_id, comment_id, reply_text, reply_author_*)...

  parent_our_comment_id: ObjectId,    # NEW — joins to our_comments
  received_at: datetime,              # NEW — when we detected it
  processed: bool,                    # NEW — whether we drafted a reply-back
  our_reply_id: ObjectId | null,      # NEW — points to our_comments row holding our response
}
```

**New indices:**
- `(parent_our_comment_id)` — chain lookups
- `(operator_id, processed, received_at)` — beat task to draft reply-backs

---

### 2.5 `cofounder_send_quotas` (NEW — rate-limit tracker)

LinkedIn / Unipile enforce per-account daily POST ceilings. Per-cofounder
counter prevents the outbox from exceeding them.

```
{
  _id: { cofounder_id: ObjectId, date: "YYYY-MM-DD" },
  initial_comments_sent: int,
  replies_sent: int,
  dms_sent: int,
  last_updated: datetime,
}
```

**Index:** the compound `_id` is the unique key.

---

## 3. Service-layer additions

### 3.1 `app/services/unipile.py`

```python
@dataclass(frozen=True)
class UnipileCommentPostResult:
    comment_id: str        # URN like urn:li:comment:(activity:...,...)
    posted_at: datetime
    raw: dict[str, Any]

def post_comment(
    *, account_id: str, post_url: str, text: str,
    parent_comment_id: str | None = None,
) -> UnipileCommentPostResult:
    """Wraps POST /posts/{post_id}/comments.
    parent_comment_id set -> reply; null -> top-level."""

# Extend existing UnipileComment dataclass:
class UnipileComment:
    ...existing fields...
    reaction_count: int = 0
    reactions_by_type: dict[str, int] | None = None  # only if Unipile exposes
    reply_count: int = 0

# Extend existing UnipilePost dataclass + parser (FREE WIN — already in payload):
class UnipilePost:
    ...existing fields...
    reaction_count: int = 0
    comment_count: int = 0
    repost_count: int = 0
    is_repost: bool = False
    can_post_comments: bool = True
    has_poll: bool = False
    poll_total_votes: int = 0
```

The `UnipilePost` extension is the biggest free win — every keyword-search
response already contains these fields ([verified via probe](#vendor-role-split)).
Parser currently discards them at [unipile.py:244-308](backend/app/services/unipile.py:244).

### 3.2 `app/services/apidirect.py`

```python
# Extend LinkedInPostDetails (optional, only when emoji breakdown wanted):
class LinkedInPostDetails:
    ...existing fields...
    likes: int = 0
    comments: int = 0
    shares: int = 0
    reactions_by_type: dict[str, int] = {}  # the unique value-add
```

---

### 3.3 `app/engine/outbox.py` (NEW)

```python
def enqueue_comment(
    db, *, operator_id, cofounder_id, candidate_id, slate_run_id,
    text, parent_comment_id=None, posted_via="auto",
) -> ObjectId:
    """Idempotent insert into our_comments with status=queued.
    Enforced via unique (candidate_id, parent_comment_id) index."""

def process_outbox(db, *, batch_size=20) -> dict:
    """Drain queued our_comments.
    1. Check cofounder quota for today.
    2. Call unipile.post_comment.
    3. On success: status=sent, persist comment_id.
    4. On failure: append send_attempt, retry up to 3x, then status=failed.
    Returns { processed, sent, failed, quota_blocked }."""
```

---

### 3.4 `app/engine/engagement_poller.py` (NEW)

```python
def poll_our_comments_and_replies(db, *, lookback_days=30, hot_only=False) -> dict:
    """Single-pass Unipile poll covering both stats and replies.
    For each unique parent_post_url where we have sent our_comments:
      1. unipile.get_post_comments(post_url)
      2. PASS A — find our comments by comment_id; update latest_* fields;
         write comment_engagement_snapshots row.
      3. PASS B — find inbound replies (new comments where parent_id == our
         comment_id); insert into replies collection.
      4. PASS C — also extract the post's own top-level counters from the
         response and write parent_post_snapshots (source=unipile).
    Merges what reply_monitor.py used to do — single Unipile call, two
    concerns serviced."""

def poll_apidirect_breakdown(db, *, comment_ids: list[str]) -> dict:
    """OPTIONAL — emoji-breakdown enrichment for selected comments.
    For each comment_id, call apidirect.get_linkedin_post_details on the
    parent_post_url and persist reactions_by_type to the snapshot.
    Config-gated, off by default."""
```

---

## 4. Beat tasks (Celery scheduled)

| Task | Schedule | Vendor | Purpose |
|---|---|---|---|
| `engine.process_outbox` | every 1 min, business hours | Unipile (write) | Drain queued sends; respects cofounder quota |
| `engine.poll_engagement_hot` | every 30 min, first 48 h after send | Unipile | High-cadence polling for fresh comments (engagement curve is steepest early) |
| `engine.poll_engagement_daily` | daily 03:00 UTC | Unipile | Long-tail polling, 30 d window |
| `engine.poll_apidirect_breakdown` | daily 03:00 UTC (optional, off by default) | APIDirect | Emoji-breakdown for top-N performers |
| `engine.poll_replies` (EXISTING) | every 15 min | Unipile | **Merge into `poll_engagement_*`** — both call `get_post_comments`. Single call services both concerns. |

The hot/daily/reply merge is the key performance optimization: one Unipile call
per unique parent_post_url per cycle.

---

## 5. HTTP API routes (UI surface)

```
GET  /api/analytics/comments?operator_id=X&since=Y&status=...
       → paginated list of our_comments + latest snapshot + at-send context

GET  /api/analytics/comments/{comment_id}/timeseries
       → comment_engagement_snapshots time-series for one comment

GET  /api/analytics/operator-summary?operator_id=X&since=Y
       → aggregates: comments sent, reactions received, reply rate,
         avg reactions per comment, top-performing comments, send-failure rate

GET  /api/analytics/post-thread/{candidate_id}
       → chronological merged view of our_comments + replies + reply-backs
         on one parent post
```

---

## 6. Data flow

```
┌─ allocator picks candidate
│
├─ drafter writes initial comment text
│
├─ outbox.enqueue_comment(parent_comment_id=None)
│     → our_comments insert, status=queued
│     → snapshots parent_post_*_at_send from the Unipile candidate doc
│       (data already captured at discovery — free)
│
├─ process_outbox beat (every 1 min):
│     → unipile.post_comment(...)
│     → our_comments update: status=sent, comment_id=X, posted_at=…
│
├─ poll_engagement beats (hot every 30m / daily 03:00 UTC):
│     For each unique parent_post_url with our_comments in window:
│       unipile.get_post_comments(post_url)  ← single call, multi-purpose
│         (1) update our_comments.latest_* + insert comment_engagement_snapshots
│         (2) detect new replies → insert replies + (optionally) draft reply-back
│         (3) update parent_post_snapshots (counters from response top-level)
│
├─ reply-back loop (opt-in per operator):
│     reply_drafter.draft_reply(PUBLIC_REPLY_BACK)
│       → outbox.enqueue_comment(parent_comment_id=our.comment_id)
│       → loops back to process_outbox above
│
└─ UI reads:
      our_comments + comment_engagement_snapshots + parent_post_snapshots
        via /api/analytics/* routes
```

---

## 7. Safety / race conditions

| Concern | Mitigation |
|---|---|
| Double-send to same target | Unique compound index `(candidate_id, parent_comment_id)` on `our_comments` — `enqueue_comment` is upsert-safe |
| Comments disabled on target post | Check `UnipilePost.can_post_comments` at verifier stage; drop with `drop_reason=comments_disabled` |
| Reply to ourselves | `replies.reply_author_provider_id` filtered against the operator's cofounder roster before triggering drafter |
| Double-reply to same inbound reply | `replies.processed` flag; `replies.our_reply_id` set when reply-back is queued |
| Send-during-rate-limit | `cofounder_send_quotas` daily counter checked in `process_outbox` before each `unipile.post_comment` |
| Backoff on transient send failure | `send_attempts` array; 3 retries with exponential delay; then status=failed for human re-trigger |
| Auto-reply going sideways publicly | Per-operator config flag `auto_send_replies_enabled: bool = false` (default OFF). Replies always drafted; sending is explicit opt-in |
| Stale parent post (deleted by author) | `unipile.get_post_comments` returns 404 → mark `our_comments.status="deleted"`, stop polling |
| Operator swaps cofounder mid-thread | `our_comments.cofounder_id` is immutable on the row; only new outbound comments use the current cofounder |
| Operator pauses engine | `process_outbox` reads `users.paused` before draining; skips paused operators |

---

## 8. Migration / build order

Each step independently shippable. Earlier steps land regardless of whether
later steps are pursued.

| Step | Effort | Vendor surface | Risk |
|---|---|---|---|
| 1. Schema migrations + indices | ½ day | — | None (data-only) |
| 2. Extend `UnipilePost` parser (engagement counters + can_post_comments) | 2 h | Unipile (read) | None — additive |
| 3. Wire engagement counters into `candidates.engagement_*` + `allocator._candidate_rank` | 2 h | — | Low — allocator already had `engagement` field defaulting to 0 |
| 4. Use `can_post_comments=false` as hard verifier drop | 1 h | — | Low — prevents wasted gate spend |
| 5. `unipile.post_comment` write endpoint + `UnipileComment` extension | 3 h | Unipile (write) | Medium — first write path |
| 6. `outbox.enqueue_comment` + `process_outbox` beat task | ½ day | Unipile (write) | Medium — needs quota tracking |
| 7. Allocator-side wiring: drafter enqueues instead of writing to Mongo-for-review | 2 h | — | High — flips behavior from "draft for human review" to "auto-send" |
| 8. `engagement_poller.poll_our_comments_and_replies` (merge with existing `poll_replies`) | ½ day | Unipile (read) | Low — read-only |
| 9. Hot poll beat (every 30m, first 48h) | 2 h | Unipile (read) | Low |
| 10. Reply-back auto-loop (gated behind `auto_send_replies_enabled`) | ½ day | Unipile (write) | High — public conversation surface |
| 11. APIDirect emoji-breakdown enrichment (optional) | 2 h | APIDirect | Low — additive |
| 12. UI tiles (comment performance table + time-series chart) | 1 day | — | Low |

**Total to MVP** (steps 1-9, full lifecycle without auto-reply-back): ~2 days.
**Total to full loop** (steps 1-12): ~3-4 days.

**Lowest-risk first slice** (steps 1-4 only, ~1 day): unlocks engagement-aware
allocation and pre-comment validity check with zero behavior change to send
flow. Ship this even if the rest is deferred.

---

## 9. What you can see once shipped

| Metric | Source | Latency |
|---|---|---|
| Reactions on each of our comments | Unipile `get_post_comments` (per-comment counter) | Near-real-time |
| Replies to each of our comments | Unipile `get_post_comments` (reply count + replies list) | Near-real-time |
| Engagement velocity (reactions/hour) | `comment_engagement_snapshots` time-series | 30-min granularity for first 48h |
| Per-emoji reaction breakdown | APIDirect (optional) | Daily |
| Parent post engagement context at send | Unipile inline payload at discovery | Captured at send |
| Parent post engagement over time | `parent_post_snapshots` time-series | Daily |
| Engagement delta during our comment's lifetime | `latest - at_send` join | Daily |
| Reply rate per operator / per cofounder / per ICP segment | aggregated from `our_comments` ↔ `replies` join | Live |
| **Impressions on our comment** | ❌ Never available — LinkedIn does not expose | — |

---

## 10. Open design questions

1. **Quota source of truth.** Is the per-cofounder daily ceiling fixed
   (50 comments/day?) or should it adapt based on Unipile's observed 429 rate?
2. **Auto-reply-back default.** Off seems right at first, but the engine's
   value drops sharply if every reply needs human approval. Worth a
   per-operator setting with a sensible default of `false` initially and
   `true` after a 2-week comfort period.
3. **Conversation cascade depth.** LinkedIn flattens replies-to-replies, so
   chain depth is effectively unbounded. Should we cap our participation at
   2-3 turns to avoid looking like a bot?
4. **Engagement-aware allocator weighting.** Right now allocator ranks
   `(icp_score, engagement, recency)` — should we instead weight by
   *predicted* engagement (using historical comment performance for similar
   ICP segments) rather than just the parent post's current counts?
5. **Cold-storage policy.** 90d TTL on snapshots is arbitrary. For long-term
   ICP analysis we might want indefinite retention but at lower resolution
   (one row per week instead of one per poll).
