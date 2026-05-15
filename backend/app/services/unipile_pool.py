"""Unipile account pool with health-aware rotation and daily quotas.

Anti-automation strategy
------------------------
Single-account discovery concentrates all LinkedIn signal in one session,
which makes the account easy to flag regardless of per-call timing. The
pool spreads load across N healthy accounts using least-recently-used
selection, while each account still respects per-account humanlike
cadence (enforced separately in ``unipile.py``'s per-account throttles).

The pool also tracks per-account daily caps + cooldown state so any
account that hits a 429 from LinkedIn drops out of rotation for a
randomized window, and any account that hits status=CREDENTIALS
(LinkedIn forced re-auth) is filtered out entirely until manually
reconnected via the Unipile dashboard.

Mongo schema (collection: ``unipile_account_pool``)
---------------------------------------------------
    {
      _id: ObjectId,
      account_id: str,        # Unipile account ID
      display_name: str,
      operator_id: ObjectId|null,    # null = shared discovery pool
      role: "discovery"|"posting"|"stats"|"disabled",
      capabilities: [str],    # ["search", "messaging", ...] from Unipile
      proxy_country: str,
      status: "OK"|"COOLDOWN"|"CREDENTIALS"|"DISABLED",
      cooldown_until: datetime|null,
      daily_caps: {search, profile_view, post_fetch},
      daily_usage: {search, profile_view, post_fetch, reset_at},
      last_used_at: datetime,
      last_429_at: datetime|null,
      consecutive_errors: int,
      consecutive_429s: int,
      total_calls: int,
      total_errors: int,
      created_at, updated_at: datetime
    }

Acquisition algorithm (least-recently-used with cap & cooldown filtering)
-------------------------------------------------------------------------
1. Reset daily counters for any account whose `daily_usage.reset_at` < now
   (rolling 24h window — keeps the cap meaningful even for accounts that
   were idle yesterday).
2. Filter candidates by role + capability + status=OK + cooldown_until in
   the past + daily_usage < daily_caps for the requested capability.
3. ``find_one_and_update`` with sort by ``last_used_at`` ascending; this
   atomically claims the longest-idle account and stamps it as used.
4. Caller invokes the Unipile API, then reports success/error back so
   the pool can update health state.
"""
from __future__ import annotations

import contextvars
import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from bson import ObjectId
from pymongo import MongoClient, ReturnDocument
from pymongo.database import Database

from app.config import settings

log = logging.getLogger(__name__)


# ── Per-run allowlist filter (ContextVar + thread-local) ────────────────
#
# When a slate_run starts, ``daily_run.run_for_operator`` populates this
# with the operator's pool-account selection. From that point on, every
# ``pool.acquire(prefer_role="discovery")`` call is filtered to ONLY return
# accounts whose ``account_id`` appears in the allowlist. The filter is
# applied as a Mongo query predicate in ``find_one_and_update`` — meaning
# accounts outside the allowlist literally cannot be atomically claimed,
# even if a call site forgets to pass the filter through. **This is the
# anti-leakage guarantee.**
#
# We thread the value via ``contextvars`` so it propagates across
# ``asyncio.to_thread`` and async/await boundaries, AND via
# ``threading.local`` so raw ``threading.Thread(target=...)`` spawns (the
# pattern used by the daily-run worker threads) get it too. Worker code
# explicitly re-attaches the filter at the top of each thread function;
# see ``daily_run._discovery_thread`` etc.
#
# Allowlist value semantics:
#   None  → no filter applied; ALL discovery accounts eligible (default)
#   []    → empty allowlist; NO accounts eligible → acquire raises
#           PoolExhausted. Lets callers explicitly disable the pool for a
#           run without removing the contextvar.
#   [...] → only these account_ids are eligible
_pool_filter_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "pool_account_filter", default=None,
)
_pool_filter_threadlocal = threading.local()


def set_pool_account_filter(account_ids: list[str] | None) -> None:
    """Restrict every subsequent ``acquire(prefer_role='discovery')`` in
    this context (and any thread that re-calls this function) to the
    given list of account_ids.

    Call with ``None`` to clear the filter (back to "all accounts").
    Call with ``[]`` to block all discovery acquires (pool effectively
    disabled for this context).
    """
    normalized: list[str] | None
    if account_ids is None:
        normalized = None
    else:
        # Defensive copy + strip; dedupe while preserving order.
        seen: set[str] = set()
        normalized = []
        for a in account_ids:
            sa = (a or "").strip()
            if sa and sa not in seen:
                seen.add(sa)
                normalized.append(sa)
    _pool_filter_var.set(normalized)
    _pool_filter_threadlocal.value = normalized


def get_pool_account_filter() -> list[str] | None:
    """Read the current allowlist. ContextVar wins (covers async paths);
    threading.local is the fallback for raw thread spawns."""
    try:
        v = _pool_filter_var.get()
    except LookupError:
        v = None
    if v is not None:
        return v
    return getattr(_pool_filter_threadlocal, "value", None)


def clear_pool_account_filter() -> None:
    """Convenience for cleanup paths / tests."""
    set_pool_account_filter(None)


# Capabilities the pool understands. These map to operation categories
# rather than Unipile's raw `sources` field, so we can change the
# underlying account-source check without breaking callers.
Capability = Literal[
    "search", "profile_view", "post_fetch", "post_comment", "invite",
]

# Default daily caps per account. Conservative — tuned for 5-6 account pool
# serving 1-3 operators. Bump these in env if you have larger pools.
DEFAULT_DAILY_CAPS: dict[str, int] = {
    "search": 80,
    "profile_view": 50,
    "post_fetch": 200,
    # post_comment is intentionally NOT in the discovery pool — comment
    # writes go through the operator's own account, not the pool, so we
    # don't enforce a pool-level cap. The per-account throttle in
    # ``unipile.py`` enforces a ~90s+ gap between writes per account.
    #
    # invite (connection requests with a note): LinkedIn quietly rate-
    # limits at ~100/week free-tier and flags accounts on bursts. 10/day
    # per account is the safe operating point — 70/week across 6
    # accounts is well below the per-account ceiling while still
    # producing meaningful weekly invite volume. Bump cautiously via
    # operator settings rather than this default.
    "invite": 10,
}

# Cooldown window after a LinkedIn-provider 429. Randomized to avoid the
# "all accounts come back at the same time" pattern. Spec: 20-60 min,
# with longer cooldowns after repeated 429s.
_COOLDOWN_MIN_S = 20 * 60
_COOLDOWN_MAX_S = 60 * 60
_COOLDOWN_BACKOFF_PER_429 = 1.5  # cumulative multiplier per recent 429

# ── Layer 3: aggregate pool gate ────────────────────────────────────────
#
# Per-account throttles keep each LinkedIn session at humanlike cadence
# on its own timeline, but a pool of N idle accounts could have N workers
# all atomically claim different accounts within microseconds of each
# other → N Unipile calls hit the wire in a sub-second burst. From an
# external observer's view (Unipile gateway, LinkedIn edge, proxy
# correlation) that's the coordinated-automation pattern, regardless of
# what each individual account does next.
#
# The aggregate gate is a process-wide minimum inter-acquire delay that
# runs AFTER the atomic claim succeeds and BEFORE acquire() returns. It
# ensures "no two pool.acquire() calls return within X seconds of each
# other" — capping the aggregate Unipile-call rate across the entire
# pool to a humanlike rate.
#
# Numbers (2.5s baseline + 0-2.5s jitter + 10% chance of 7.5-20s pause):
#   typical inter-acquire window  : 2.5-5.0s
#   90th-percentile burst          : ~3 calls in 10 seconds
#   long-pause samples (10%)       : 7.5-20s wait
#   minute-average aggregate rate  : ~12 calls/min across the whole pool
#
# Override via UNIPILE_POOL_AGGREGATE_MIN_INTERVAL_S / _JITTER_MAX_S in
# the env if you want it more or less aggressive.
import os as _os
_POOL_AGGREGATE_MIN_INTERVAL_S = float(
    _os.environ.get("UNIPILE_POOL_AGGREGATE_MIN_INTERVAL_S", "2.5")
)
_POOL_AGGREGATE_JITTER_MAX_S = float(
    _os.environ.get("UNIPILE_POOL_AGGREGATE_JITTER_MAX_S", "2.5")
)
_aggregate_gate_lock = threading.Lock()
_aggregate_gate_last_ts = 0.0


def _wait_aggregate_gate() -> None:
    """Block until the humanlike inter-acquire window has elapsed since
    the previous acquire returned. Uses the same heavy-tailed delay
    distribution as the per-account throttles (``_human_delay_seconds``
    from unipile.py) — most of the time a short gap, occasional longer
    "thinking" pause to break the uniform-jitter detection pattern.

    Process-wide lock — safe under single-worker uvicorn / single-thread
    Celery. Multi-process deployments would need a Redis token bucket
    here to coordinate.
    """
    import time as _t
    # Import lazily to avoid a circular import at module load. unipile.py
    # imports from app.config only; unipile_pool.py imports from unipile.
    from app.services.unipile import _human_delay_seconds
    global _aggregate_gate_last_ts
    with _aggregate_gate_lock:
        elapsed = _t.monotonic() - _aggregate_gate_last_ts
        target_gap = _human_delay_seconds(
            _POOL_AGGREGATE_MIN_INTERVAL_S,
            _POOL_AGGREGATE_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            _t.sleep(wait)
        _aggregate_gate_last_ts = _t.monotonic()

# Process-wide pool singleton — initialized lazily on first access.
_pool_instance: "UnipileAccountPool | None" = None
_pool_lock = threading.Lock()


class PoolExhausted(RuntimeError):
    """All eligible accounts are in cooldown or over their daily cap."""


class UnipileAccountPool:
    """Mongo-backed pool of Unipile-connected LinkedIn accounts.

    All state lives in Mongo so it persists across worker restarts and
    is consistent across the FastAPI process, Celery workers, and the
    Celery beat scheduler. Each pool method makes atomic Mongo updates
    via ``find_one_and_update`` so concurrent acquires from different
    workers can't double-claim the same account.
    """

    COLLECTION = "unipile_account_pool"

    def __init__(self, db: Database) -> None:
        self.db = db
        self.coll = db[self.COLLECTION]
        # Indexes for fast acquire + dashboard reads. Idempotent.
        self.coll.create_index("account_id", unique=True)
        self.coll.create_index([("role", 1), ("status", 1), ("last_used_at", 1)])
        self.coll.create_index("status")
        # One-time backfill for accounts created before a capability /
        # daily-cap key was added to DEFAULT_DAILY_CAPS. Without this,
        # acquire("invite") would $expr-compare null < null on every old
        # account and exclude them all from the pool. Idempotent: $set
        # only fires when the field is absent. 2026-05-15: added 'invite'.
        self._backfill_capability_defaults()

    def _backfill_capability_defaults(self) -> None:
        """Make sure every existing account doc has the full set of
        DEFAULT_DAILY_CAPS keys (caps + usage). Runs every init; the
        Mongo filter (`field: {$exists: False}`) guarantees idempotency.
        """
        for cap_name, cap_default in DEFAULT_DAILY_CAPS.items():
            # Add cap value if missing
            self.coll.update_many(
                {f"daily_caps.{cap_name}": {"$exists": False}},
                {"$set": {f"daily_caps.{cap_name}": cap_default}},
            )
            # Add usage=0 if missing
            self.coll.update_many(
                {f"daily_usage.{cap_name}": {"$exists": False}},
                {"$set": {f"daily_usage.{cap_name}": 0}},
            )
        # The "invite" capability also needs to appear in each LinkedIn
        # account's `capabilities` array (the acquire() filter checks
        # `capabilities: capability`). Only patch accounts that have a
        # MESSAGING-tier capability (post_comment) — those are the
        # session-authenticated accounts where invites can fire.
        # `$addToSet` is idempotent so this is safe to run on every init.
        self.coll.update_many(
            {
                "$and": [
                    {"capabilities": "post_comment"},
                    {"capabilities": {"$ne": "invite"}},
                ],
            },
            {"$addToSet": {"capabilities": "invite"}},
        )

    # ── Bootstrap / sync ────────────────────────────────────────────────

    def sync_from_unipile(self) -> dict[str, int]:
        """Pull the current Unipile account list, upsert each into the
        pool collection. Newly-discovered accounts get role=discovery by
        default; existing accounts have their status + capabilities +
        proxy_country refreshed from Unipile (so accounts that hit
        CREDENTIALS on LinkedIn's end get marked correctly).

        Returns ``{"created": N, "updated": M}``."""
        from app.services.unipile import _client, _check_resp, _request_with_429_retry

        # Route through `_request_with_429_retry` so the call goes through
        # the same throttle wrapper as every other Unipile request.
        # `/accounts` is a tenant-level admin endpoint (no per-session
        # context), so account_id=None here intentionally falls into the
        # global "__global__" bucket of the default throttle — that bucket
        # is fine for admin/management calls that don't belong to any one
        # LinkedIn session. The wrapper also handles transport errors +
        # 429s uniformly, which raw client.get does not.
        with _client() as client:
            resp = _request_with_429_retry(
                client, "GET", "/accounts",
                account_id=None,
                params={"limit": 100},
            )
        payload = _check_resp(resp, "pool.sync_from_unipile")
        raw_accounts = payload.get("items") or []

        now = datetime.now(timezone.utc)
        created = 0
        updated = 0
        for raw in raw_accounts:
            aid = raw.get("id")
            if not aid:
                continue
            sources = raw.get("sources") or []
            source_statuses = [
                (s.get("status") or "").upper() for s in sources
            ]
            connection = (raw.get("connection_params") or {}).get("im") or {}
            proxy = connection.get("proxy") or {}
            # Capabilities derived from sources[]. A MESSAGING source means
            # the account can post/read; SEARCH means it can hit
            # /linkedin/search. We assume both for any healthy LINKEDIN
            # account — Unipile sometimes only labels one source even when
            # both work in practice.
            caps = ["post_fetch", "profile_view"]
            for s in sources:
                sid = (s.get("id") or "").upper()
                if "SEARCH" in sid:
                    caps.append("search")
                if "MESSAGING" in sid:
                    caps.append("post_comment")
                    # Invitations ride the same authenticated LinkedIn
                    # session as comment-posting, so a MESSAGING source
                    # is sufficient for both. We don't gate invites on a
                    # separate Unipile source label because Unipile uses
                    # the same /api/v1/users/invite endpoint for any
                    # LINKEDIN-typed account with an active session.
                    caps.append("invite")
            # If nothing was tagged SEARCH but status=OK, allow search
            # optimistically — Unipile's labeling is inconsistent.
            if "search" not in caps and any(s == "OK" for s in source_statuses):
                caps.append("search")
            caps = sorted(set(caps))

            status = "OK"
            if any(s in ("CREDENTIALS", "CREDENTIAL") for s in source_statuses):
                status = "CREDENTIALS"
            elif not source_statuses or all(s != "OK" for s in source_statuses):
                status = "CREDENTIALS"

            display_name = raw.get("name") or aid

            # Two-part update: $set refreshes volatile fields on every sync,
            # $setOnInsert seeds defaults that should never be overwritten
            # by a re-sync (role assignment, daily caps/usage, error
            # counters). Mongo rejects overlapping keys between $set and
            # $setOnInsert, so the two dicts MUST be disjoint.
            set_doc = {
                "display_name": display_name,
                "capabilities": caps,
                "proxy_country": proxy.get("country") or "?",
                "status": status,
                "updated_at": now,
            }
            set_on_insert_doc = {
                "account_id": aid,
                "operator_id": None,
                "role": "discovery",  # default; user can promote later
                "cooldown_until": None,
                "daily_caps": dict(DEFAULT_DAILY_CAPS),
                "daily_usage": {
                    "search": 0,
                    "profile_view": 0,
                    "post_fetch": 0,
                    "invite": 0,
                    "reset_at": _next_reset_at(now),
                },
                "last_used_at": now,
                "last_429_at": None,
                "consecutive_errors": 0,
                "consecutive_429s": 0,
                "total_calls": 0,
                "total_errors": 0,
                "created_at": now,
            }
            update_doc = {
                "$set": set_doc,
                "$setOnInsert": set_on_insert_doc,
            }
            res = self.coll.update_one({"account_id": aid}, update_doc, upsert=True)
            if res.upserted_id is not None:
                created += 1
            else:
                updated += 1

        # Tag the operator's own posting account + the stats account
        # automatically from settings, so the dashboard reflects intent.
        ops_db = (
            self.db.client["linkedin_engine"] if hasattr(self.db, "client") else self.db
        )
        try:
            operators = list(ops_db.users.find({}, {"_id": 1, "unipile_account_id": 1}))
            for op in operators:
                op_aid = (op.get("unipile_account_id") or "").strip()
                if not op_aid:
                    continue
                self.coll.update_one(
                    {"account_id": op_aid},
                    {"$set": {"role": "posting", "operator_id": op["_id"]}},
                )
        except Exception as err:  # noqa: BLE001
            log.warning("pool.sync: operator role tagging skipped: %s", err)

        stats_aid = (getattr(settings, "unipile_stats_account_id", "") or "").strip()
        if stats_aid:
            self.coll.update_one(
                {"account_id": stats_aid},
                {"$set": {"role": "stats"}},
            )

        return {"created": created, "updated": updated}

    # ── Acquisition ─────────────────────────────────────────────────────

    def acquire(
        self,
        *,
        capability: Capability,
        operator_id: ObjectId | None = None,
        prefer_role: Literal["discovery", "stats"] = "discovery",
    ) -> str:
        """Atomically claim the least-recently-used account that:
          - has role=prefer_role (default 'discovery')
          - includes ``capability`` in its capabilities array
          - has status=OK
          - has no active cooldown
          - has daily_usage[capability] < daily_caps[capability]

        Increments the relevant daily_usage counter and updates
        last_used_at as part of the same Mongo update — concurrent
        callers from different workers can't double-claim.

        Raises ``PoolExhausted`` when no account meets the criteria.
        Caller decides whether to wait, skip, or fall through to a
        legacy single-account path.
        """
        now = datetime.now(timezone.utc)
        # Roll daily counters first — accounts that were idle yesterday
        # should not be penalized today.
        self._reset_due_daily_counters(now)

        usage_field = f"daily_usage.{capability}"
        cap_field = f"daily_caps.{capability}"

        candidate_filter: dict[str, Any] = {
            "role": prefer_role,
            "status": "OK",
            "capabilities": capability,
            "$or": [
                {"cooldown_until": None},
                {"cooldown_until": {"$lt": now}},
            ],
            "$expr": {
                "$lt": [f"${usage_field}", f"${cap_field}"],
            },
        }
        # operator_id filter: include accounts explicitly assigned to this
        # operator AND shared accounts (operator_id=null). For "stats"
        # role we don't filter by operator since the stats account is
        # global.
        if operator_id is not None and prefer_role == "discovery":
            candidate_filter["$and"] = [
                {
                    "$or": [
                        {"operator_id": operator_id},
                        {"operator_id": None},
                    ],
                }
            ]

        # **Anti-leakage allowlist** — when a slate run has restricted the
        # pool to specific accounts, the Mongo filter must reject every
        # other account at the atomic-claim level. We apply this ONLY to
        # discovery acquires (stats/posting acquires are special-purpose
        # single-account paths that the caller picks explicitly via
        # role+operator filters above).
        if prefer_role == "discovery":
            allowlist = get_pool_account_filter()
            if allowlist is not None:
                if not allowlist:
                    raise PoolExhausted(
                        "pool allowlist is empty for this run — no accounts eligible"
                    )
                candidate_filter["account_id"] = {"$in": allowlist}

        result = self.coll.find_one_and_update(
            candidate_filter,
            {
                "$inc": {
                    usage_field: 1,
                    "total_calls": 1,
                },
                "$set": {
                    "last_used_at": now,
                    "updated_at": now,
                },
            },
            sort=[("last_used_at", 1)],  # least-recently-used first
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            raise PoolExhausted(
                f"No available account for capability={capability!r} role={prefer_role!r}; "
                "all accounts are over daily cap, in cooldown, or status != OK"
            )

        # Layer 3: aggregate-pool gate. Even though per-account throttles
        # space individual sessions humanlike, N idle accounts in the pool
        # can otherwise be acquired in parallel within microseconds — N
        # Unipile calls hit the wire in a sub-second burst. This gate
        # smooths the aggregate request rate from the entire pool to a
        # humanlike cadence (~12 calls/min across the pool by default).
        _wait_aggregate_gate()
        return result["account_id"]

    # ── Health reporting ────────────────────────────────────────────────

    def report_success(self, account_id: str) -> None:
        """Reset consecutive-error counters on a clean response."""
        self.coll.update_one(
            {"account_id": account_id},
            {
                "$set": {
                    "consecutive_errors": 0,
                    "consecutive_429s": 0,
                    "updated_at": datetime.now(timezone.utc),
                },
            },
        )

    def report_error(self, account_id: str, error: Exception) -> None:
        """Update per-account health state based on the error type.

          - 429 / provider rate-limit → set cooldown_until = now +
            random(20m, 60m) × (1.5 ^ consecutive_429s). Status stays OK
            so the account auto-recovers when cooldown expires.
          - 401 / auth / credentials → set status=CREDENTIALS. Account
            won't be acquired again until manually reconnected via the
            Unipile dashboard.
          - Other errors → increment consecutive_errors. After 5 in a
            row, set status=COOLDOWN with a 30m timeout (transient
            upstream issues).
        """
        now = datetime.now(timezone.utc)
        msg = str(error)
        lower = msg.lower()

        update: dict[str, Any] = {
            "$inc": {
                "total_errors": 1,
                "consecutive_errors": 1,
            },
            "$set": {"updated_at": now},
        }

        is_429 = "429" in msg or "too many" in lower or "rate" in lower
        is_auth = (
            "401" in msg or "credentials" in lower or "unauthor" in lower
            or "auth" in lower
        )

        if is_auth:
            # Hard fail — needs human re-auth.
            update["$set"]["status"] = "CREDENTIALS"
            update["$set"]["cooldown_until"] = None
            log.warning(
                "pool: account=%s flagged CREDENTIALS — reconnect in Unipile",
                account_id,
            )
        elif is_429:
            # Soft fail — back off with random jitter, then auto-recover.
            current = self.coll.find_one(
                {"account_id": account_id}, {"consecutive_429s": 1}
            )
            n_recent = int((current or {}).get("consecutive_429s", 0))
            multiplier = _COOLDOWN_BACKOFF_PER_429 ** min(n_recent, 5)
            secs = random.uniform(_COOLDOWN_MIN_S, _COOLDOWN_MAX_S) * multiplier
            cooldown_until = now + timedelta(seconds=secs)
            update["$inc"]["consecutive_429s"] = 1
            update["$set"]["cooldown_until"] = cooldown_until
            update["$set"]["last_429_at"] = now
            log.warning(
                "pool: account=%s cooling for %.0fs after 429 (#%d)",
                account_id, secs, n_recent + 1,
            )
        else:
            # Transient — track but don't disable unless we accumulate 5.
            current = self.coll.find_one(
                {"account_id": account_id}, {"consecutive_errors": 1}
            )
            n_errs = int((current or {}).get("consecutive_errors", 0)) + 1
            if n_errs >= 5:
                update["$set"]["cooldown_until"] = now + timedelta(minutes=30)
                log.warning(
                    "pool: account=%s cooling 30m after %d consecutive errors",
                    account_id, n_errs,
                )

        self.coll.update_one({"account_id": account_id}, update)

    # ── Admin / dashboard ───────────────────────────────────────────────

    def list_all(self) -> list[dict[str, Any]]:
        """All pool accounts in display order — used by the dashboard UI."""
        return list(self.coll.find().sort("role", 1).sort("display_name", 1))

    def reset_cooldown(self, account_id: str) -> bool:
        """Manually clear an account's cooldown. Used by the UI's
        "Reset cooldown" button when an operator knows a 429 was
        transient and wants to put the account back in rotation."""
        res = self.coll.update_one(
            {"account_id": account_id},
            {
                "$set": {
                    "cooldown_until": None,
                    "consecutive_429s": 0,
                    "consecutive_errors": 0,
                    "updated_at": datetime.now(timezone.utc),
                },
            },
        )
        return res.modified_count > 0

    def set_role(self, account_id: str, role: str) -> bool:
        """Manually change an account's role assignment. UI exposes this
        so operators can move accounts between discovery/posting/stats
        pools without DB surgery."""
        if role not in ("discovery", "posting", "stats", "disabled"):
            raise ValueError(f"invalid role: {role}")
        status_update: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        status_update["role"] = role
        if role == "disabled":
            status_update["status"] = "DISABLED"
        else:
            # When promoting out of disabled, restore OK status if
            # credentials aren't flagged.
            current = self.coll.find_one({"account_id": account_id}, {"status": 1})
            if (current or {}).get("status") == "DISABLED":
                status_update["status"] = "OK"
        res = self.coll.update_one(
            {"account_id": account_id}, {"$set": status_update}
        )
        return res.modified_count > 0

    # ── Internal helpers ────────────────────────────────────────────────

    def _reset_due_daily_counters(self, now: datetime) -> None:
        """Atomically roll daily_usage counters for any account whose
        ``reset_at`` has passed. Called on every acquire so we don't need
        a background scheduler."""
        next_reset = _next_reset_at(now)
        self.coll.update_many(
            {"daily_usage.reset_at": {"$lt": now}},
            {
                "$set": {
                    "daily_usage.search": 0,
                    "daily_usage.profile_view": 0,
                    "daily_usage.post_fetch": 0,
                    "daily_usage.invite": 0,
                    "daily_usage.reset_at": next_reset,
                    "updated_at": now,
                },
            },
        )


def _next_reset_at(now: datetime) -> datetime:
    """Next midnight UTC. We use a calendar-day window rather than a
    rolling 24h so the cap is intuitive to operators ("80 searches today")
    and admins can predict when the pool refills."""
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return tomorrow


# ── Module-level accessors ──────────────────────────────────────────────


def _open_db() -> Database:
    """Sync pymongo handle for the pool's own use. Independent of the
    request-scoped Motor handle so the pool works from threads, Celery
    workers, and sync engine code without coupling."""
    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
    return client[settings.mongodb_db]


def get_pool() -> UnipileAccountPool:
    """Process-wide pool singleton. Lazy-initialized on first access so
    importing this module doesn't open a Mongo connection at startup."""
    global _pool_instance
    if _pool_instance is not None:
        return _pool_instance
    with _pool_lock:
        if _pool_instance is None:
            _pool_instance = UnipileAccountPool(_open_db())
        return _pool_instance


def reset_pool_singleton_for_tests() -> None:
    """Drop the cached singleton — used by tests that want a fresh
    pool with a different Mongo handle."""
    global _pool_instance
    with _pool_lock:
        _pool_instance = None
