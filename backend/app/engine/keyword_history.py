"""
Per-query no-repeat ledger (RULE 15 / RULE 15-EXT / RULE 24).

When ``settings.discovery_keyword_history_enabled`` is true, each
(operator_id, source_channel, query) tuple is recorded the first time the
engine uses it. Subsequent runs filter out queries used in the lookback
window so the engine does not burn API credits or LinkedIn-account quota on
the same query day after day.

When that setting is false (default), ``filter_unused`` returns every query
and ``mark_used`` does nothing — keywords may repeat every run.

Schema:
    keyword_history:
      operator_id      ObjectId
      source_channel   str        # "keyword_topical" | "keyword_title_industry" | "title_search"
      query            str
      last_used_at     datetime
      expires_at       datetime   # last_used_at + DEFAULT_LOOKBACK_DAYS, used by TTL index

Indexes (added in app.database._ensure_indexes):
  - { operator_id, source_channel, query } unique
  - { expires_at } TTL (Mongo expires docs when expires_at is in the past)
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from bson import ObjectId
from pymongo.database import Database

from app.models.common import utcnow

from app.config import get_settings, settings

# Fallback when no override comes via settings (kept for callers that still
# pass through to the constant). Settings takes precedence at call time.
DEFAULT_LOOKBACK_DAYS = settings.keyword_history_lookback_days or 14


def filter_unused(
    db: Database,
    *,
    operator_id: ObjectId,
    source_channel: str,
    queries: Iterable[str],
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[str]:
    """Return the subset of `queries` that have NOT been used by this
    operator on this channel within the last `days`. Order preserved."""
    queries = [q for q in queries if q]
    if not get_settings().discovery_keyword_history_enabled:
        return queries
    if not queries:
        return []
    cutoff = utcnow() - timedelta(days=days)
    cursor = db.keyword_history.find(
        {
            "operator_id": operator_id,
            "source_channel": source_channel,
            "query": {"$in": queries},
            "last_used_at": {"$gte": cutoff},
        },
        {"query": 1},
    )
    used = {row["query"] for row in cursor if row.get("query")}
    return [q for q in queries if q not in used]


def mark_used(
    db: Database,
    *,
    operator_id: ObjectId,
    source_channel: str,
    query: str,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> None:
    """Record that `query` was used. Resets last_used_at and bumps the
    expires_at on each call so the ledger always reflects the most recent
    use of a given query."""
    if not get_settings().discovery_keyword_history_enabled:
        return
    if not query:
        return
    now = utcnow()
    db.keyword_history.update_one(
        {
            "operator_id": operator_id,
            "source_channel": source_channel,
            "query": query,
        },
        {
            "$set": {
                "last_used_at": now,
                "expires_at": now + timedelta(days=days),
                "updated_at": now,
            },
            "$setOnInsert": {
                "operator_id": operator_id,
                "source_channel": source_channel,
                "query": query,
                "created_at": now,
            },
        },
        upsert=True,
    )
