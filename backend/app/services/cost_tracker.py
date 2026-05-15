"""Live per-slate-run cost accounting across every paid surface.

Tracks dollars + call counts from:

  * Wiza Person Enrich   — $0.015/credit (1 credit per match at level=none)
  * Crustdata Person Enrich — ~$0.05/credit configurable (3 credits/match)
  * APIDirect            — per-endpoint flat fees:
      /v1/linkedin/company — $0.006/call
      /v1/linkedin/post    — ~$0.005/call (configurable)
      /v1/linkedin/posts   — ~$0.005/call per page (configurable)
  * Unipile              — counted ($0 / subscription) so we can see call
                            volume + LinkedIn rate-limit burn even though
                            it doesn't add to the dollar total.
  * LLM                  — OpenAI + Anthropic, priced per million tokens by
                            model substring; usage extracted from each
                            response's `.usage` object.

How it's plumbed
----------------

The `slate_run_id` flows through both an explicit kwarg (for callers that
already have it on hand) and a thread-local + ContextVar (for deep call
sites — like the LLM clients — where threading the ID through every
signature would be invasive).

Each `record_*` call does an in-process aggregation **and** a Mongo
`$inc` against ``slate_runs.cost_breakdown`` so the totals are live:
the runs UI can poll a slate_run doc mid-flight and see the dollars
climb without waiting for a flush.

Failure mode: if the Mongo write fails or there's no slate_run context
set, recording becomes a no-op (logged at DEBUG). We never let a cost-
tracking glitch abort a discovery batch.
"""
from __future__ import annotations

import contextvars
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import MongoClient
from pymongo.database import Database

from app.config import settings

log = logging.getLogger(__name__)


# ── Per-provider unit prices ─────────────────────────────────────────────
# All dollars in USD. LLM prices are per million tokens (input / output).
# Picked from each vendor's public pricing page on 2026-05-14; override
# at runtime by setting the matching env var (see `app/config.py`).

WIZA_PER_CREDIT_USD = 0.015
CRUSTDATA_PER_CREDIT_USD = 0.05   # 3 cr/match → $0.15/match at default
APIDIRECT_COMPANY_USD = 0.006
APIDIRECT_POST_USD = 0.005
APIDIRECT_SEARCH_USD = 0.005

# LLM model → (input $/M tokens, output $/M tokens). Matched by substring,
# longest-first so "claude-opus" wins over "claude" when we add new tiers.
LLM_MODEL_PRICES_PER_M: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini":  (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1":      (2.00, 8.00),
    "gpt-4o":       (2.50, 10.00),
    # Anthropic — names are substring-matched so all dated variants land
    # on the same bucket (claude-opus-4-5-20251101 matches "claude-opus").
    "claude-haiku":  (1.00, 5.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus":   (15.00, 75.00),
}


# ── Slate-run context propagation ────────────────────────────────────────
# Two parallel mechanisms — they intentionally cover different deployment
# shapes:
#   * contextvars.ContextVar — propagates across async/await boundaries and
#     across `Context.run()` thread starts (the modern path).
#   * threading.local        — covers raw `threading.Thread(target=…)` spawns
#     that don't carry the contextvar across thread boundaries.
# `set_current_slate_run` writes to both; `get_current_slate_run` reads
# whichever has a value.

_current_slate_run_var: contextvars.ContextVar[ObjectId | None] = contextvars.ContextVar(
    "current_slate_run_id", default=None,
)
_thread_local = threading.local()


def set_current_slate_run(slate_run_id: ObjectId | None) -> None:
    _current_slate_run_var.set(slate_run_id)
    _thread_local.slate_run_id = slate_run_id


def get_current_slate_run() -> ObjectId | None:
    sid = _current_slate_run_var.get()
    if sid is not None:
        return sid
    return getattr(_thread_local, "slate_run_id", None)


# ── Lazy Mongo client (side-channel for $inc writes) ─────────────────────

_mongo_client: MongoClient | None = None
_mongo_db: Database | None = None
_mongo_lock = threading.Lock()


def _db() -> Database | None:
    """Return a pymongo Database handle for cost writes.

    Opens its own pymongo client rather than reusing the FastAPI/Motor
    handle so the side-channel works from sync Celery workers, sync engine
    threads, and async route handlers without coupling to whatever shape
    the caller is using.
    """
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        return _mongo_db
    with _mongo_lock:
        if _mongo_db is not None:
            return _mongo_db
        uri = (settings.mongodb_uri or "").strip()
        db_name = (settings.mongodb_db or "").strip()
        if not uri or not db_name:
            log.debug("cost_tracker._db: mongo not configured; recording disabled")
            return None
        try:
            _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            _mongo_db = _mongo_client[db_name]
        except Exception as err:  # noqa: BLE001
            log.warning("cost_tracker._db: connect failed: %s", err)
            return None
        return _mongo_db


# ── Generic recorder ─────────────────────────────────────────────────────


def _record(
    *,
    provider: str,
    line_item: str,
    count: int = 1,
    dollars: float = 0.0,
    slate_run_id: ObjectId | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Internal: increment Mongo counters for one paid event.

    The Mongo update is fire-and-forget — exceptions are logged at DEBUG.
    The caller never needs to handle the result; cost tracking must NEVER
    block or fail a discovery / drafter call.
    """
    sid = slate_run_id or get_current_slate_run()
    if sid is None:
        return
    db = _db()
    if db is None:
        return
    now = datetime.now(timezone.utc)
    safe_li = line_item.replace(".", "_")  # Mongo doesn't allow dots in keys
    update = {
        "$inc": {
            f"cost_breakdown.{provider}.line_items.{safe_li}.count": int(count),
            f"cost_breakdown.{provider}.line_items.{safe_li}.dollars": float(dollars),
            f"cost_breakdown.{provider}.totals.count": int(count),
            f"cost_breakdown.{provider}.totals.dollars": float(dollars),
            "cost_breakdown.totals.calls": int(count),
            "cost_breakdown.totals.dollars": float(dollars),
        },
        "$set": {
            "cost_breakdown.updated_at": now,
            f"cost_breakdown.{provider}.last_at": now,
        },
    }
    try:
        db.slate_runs.update_one({"_id": sid}, update)
    except Exception as err:  # noqa: BLE001
        log.debug("cost_tracker: mongo write failed slate=%s err=%s", sid, err)


# ── Per-provider convenience wrappers ─────────────────────────────────────


def record_wiza_match(*, slate_run_id: ObjectId | None = None) -> None:
    """One Wiza individual_reveal at enrichment_level=none that matched
    (1 scrape credit). Free misses do not call this — only matches."""
    _record(
        provider="wiza", line_item="enrich_none_match",
        count=1, dollars=WIZA_PER_CREDIT_USD,
        slate_run_id=slate_run_id,
    )


def record_crustdata_match(*, slate_run_id: ObjectId | None = None) -> None:
    """One Crustdata Person Enrich match (3 credits/match)."""
    dollars = 3 * CRUSTDATA_PER_CREDIT_USD
    _record(
        provider="crustdata", line_item="person_enrich_match",
        count=1, dollars=dollars,
        slate_run_id=slate_run_id,
    )


def record_apidirect_call(endpoint: str, *, slate_run_id: ObjectId | None = None) -> None:
    """One paid APIDirect HTTP call. ``endpoint`` ∈ {company, post, search}.

    Pricing per endpoint is set above; unknown endpoints record at $0
    so we still count the call without inflating the dollar bucket."""
    dollars = {
        "company": APIDIRECT_COMPANY_USD,
        "post": APIDIRECT_POST_USD,
        "search": APIDIRECT_SEARCH_USD,
    }.get(endpoint, 0.0)
    _record(
        provider="apidirect", line_item=endpoint,
        count=1, dollars=dollars,
        slate_run_id=slate_run_id,
    )


def record_unipile_call(endpoint: str, *, slate_run_id: ObjectId | None = None) -> None:
    """One Unipile HTTP call — subscription billing, so $0/call. Still
    tracked because Unipile call volume is the *real* cost surface
    (LinkedIn rate-limit budget on the connected account)."""
    _record(
        provider="unipile", line_item=endpoint,
        count=1, dollars=0.0,
        slate_run_id=slate_run_id,
    )


def record_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    slate_run_id: ObjectId | None = None,
    tier: str | None = None,
) -> None:
    """One LLM round-trip. Uses the per-model price table; matches by
    longest substring so ``claude-opus-4-5-20251101`` lands in the
    ``claude-opus`` bucket. Models not in the table record as $0 (and
    log a warning once) so a new model deployment doesn't silently
    inflate the bill — only show up as a count delta we can investigate.
    """
    if prompt_tokens < 0:
        prompt_tokens = 0
    if completion_tokens < 0:
        completion_tokens = 0
    in_per_m: float | None = None
    out_per_m: float | None = None
    matched_key: str | None = None
    # Longest-substring-first so "gpt-4o-mini" wins over "gpt-4o".
    for key in sorted(LLM_MODEL_PRICES_PER_M.keys(), key=len, reverse=True):
        if key in model:
            in_per_m, out_per_m = LLM_MODEL_PRICES_PER_M[key]
            matched_key = key
            break
    if in_per_m is None or out_per_m is None:
        log.warning("cost_tracker: unknown LLM model %r — counted at $0", model)
        line_item = f"unknown:{model[:40]}"
        dollars = 0.0
    else:
        line_item = matched_key or model
        if tier:
            line_item = f"{matched_key}:{tier}"
        dollars = (
            prompt_tokens * in_per_m + completion_tokens * out_per_m
        ) / 1_000_000.0
    # Tokens are tracked alongside dollars under the same line_item so the
    # UI can show "X calls, Y tokens, $Z" without a second query.
    sid = slate_run_id or get_current_slate_run()
    if sid is None:
        return
    db = _db()
    if db is None:
        return
    now = datetime.now(timezone.utc)
    safe_li = line_item.replace(".", "_")
    try:
        db.slate_runs.update_one(
            {"_id": sid},
            {
                "$inc": {
                    f"cost_breakdown.llm.line_items.{safe_li}.count": 1,
                    f"cost_breakdown.llm.line_items.{safe_li}.prompt_tokens": int(prompt_tokens),
                    f"cost_breakdown.llm.line_items.{safe_li}.completion_tokens": int(completion_tokens),
                    f"cost_breakdown.llm.line_items.{safe_li}.dollars": float(dollars),
                    "cost_breakdown.llm.totals.count": 1,
                    "cost_breakdown.llm.totals.prompt_tokens": int(prompt_tokens),
                    "cost_breakdown.llm.totals.completion_tokens": int(completion_tokens),
                    "cost_breakdown.llm.totals.dollars": float(dollars),
                    "cost_breakdown.totals.calls": 1,
                    "cost_breakdown.totals.dollars": float(dollars),
                },
                "$set": {
                    "cost_breakdown.updated_at": now,
                    "cost_breakdown.llm.last_at": now,
                },
            },
        )
    except Exception as err:  # noqa: BLE001
        log.debug("cost_tracker.record_llm_call: mongo write failed: %s", err)


def get_breakdown(slate_run_id: ObjectId) -> dict[str, Any]:
    """Read the current cost_breakdown for a slate_run. Returns an empty
    dict when nothing's been recorded yet or the slate doesn't exist."""
    db = _db()
    if db is None:
        return {}
    doc = db.slate_runs.find_one(
        {"_id": slate_run_id},
        {"cost_breakdown": 1, "_id": 0},
    )
    if not doc:
        return {}
    return doc.get("cost_breakdown") or {}
