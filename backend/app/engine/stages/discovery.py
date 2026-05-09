"""
Discovery stage: for each cofounder, fetch posts via Unipile (using their
connected LinkedIn account) for the operator's tier-1/2 keywords plus pending
manual contacts / harvester seeds. Dedupe via the exhaustion ledger and insert
raw candidates.

Each cofounder needs `unipile_account_id` set. Without one, that cofounder is
skipped with an audit warning.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.engine.constants import (
    DISCOVERY_TIER_1_PER_RUN,
    DISCOVERY_TIER_2_PER_RUN,
    DISCOVERY_TIER_3_PER_RUN,
    EXHAUSTION_LOOKBACK_DAYS,
)
from app.models.common import utcnow
from app.services.unipile import (
    UnipileError,
    UnipileNotConfigured,
    UnipilePost,
    get_user_posts,
    search_posts,
)

log = logging.getLogger(__name__)


def _rotate(values: list[str], n: int) -> list[str]:
    """Pick up to N keywords with mild shuffling so successive runs vary."""
    if not values:
        return []
    pool = list(values)
    random.shuffle(pool)
    return pool[: max(0, n)]


def _is_exhausted(db: Database, operator_id: ObjectId, author_url: str) -> bool:
    if not author_url:
        return False
    cutoff = utcnow() - timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    doc = db.exhaustion_ledger.find_one(
        {"operator_id": operator_id, "linkedin_url": author_url}
    )
    return bool(doc and doc.get("last_engaged_at") and doc["last_engaged_at"] >= cutoff)


def _candidate_doc(
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    post: UnipilePost,
    source: str,
    source_keyword: str,
    source_classification: str,
) -> dict[str, Any]:
    now = utcnow()
    return {
        "operator_id": operator_id,
        "cofounder_id": cofounder_id,
        "slate_run_id": slate_run_id,
        "post_url": post.url,
        "post_id": post.id,
        "author_name": post.author_name,
        "author_title": post.author_title,
        "author_company": post.author_company,
        "author_linkedin_url": post.author_profile_url,
        "post_text": post.text,
        "post_published_at": post.published_at,
        "source": source,
        "source_keyword": source_keyword,
        "source_classification": source_classification,
        "status": "raw",
        "gate_results": {},
        "drop_reason": None,
        "comment_text": None,
        "comment_type": None,
        "shipped_at": None,
        "user_action": "pending",
        "last_reply_check_at": None,
        "reply_count": 0,
        "created_at": now,
        "updated_at": now,
    }


def discover_for_operator(
    db: Database,
    *,
    operator: dict[str, Any],
    cofounders: list[dict[str, Any]],
    slate_run_id: ObjectId,
) -> int:
    """Insert raw candidates per cofounder via Unipile."""
    operator_id: ObjectId = operator["_id"]
    extracted = operator.get("product_extracted") or {}
    keywords = (extracted.get("suggested_keywords") or {}) if extracted else {}
    tier_1 = keywords.get("tier_1") or []
    tier_2 = keywords.get("tier_2") or []
    tier_3 = keywords.get("tier_3") or []

    seeds = list(
        db.discovery_seeds.find(
            {
                "operator_id": operator_id,
                "status": "pending",
                "expires_at": {"$gt": utcnow()},
            }
        )
    )

    inserted = 0
    seen_urls: set[str] = set()

    for cofounder in cofounders:
        cofounder_id: ObjectId = cofounder["_id"]
        account_id = cofounder.get("unipile_account_id")
        if not account_id:
            log.warning(
                "discovery: skipping cofounder %s — no unipile_account_id",
                cofounder_id,
            )
            db.audit_records.insert_one(
                {
                    "operator_id": operator_id,
                    "event_type": "stage_error",
                    "stage": "discovery",
                    "details": {
                        "cofounder_id": str(cofounder_id),
                        "reason": "no_unipile_account",
                    },
                    "severity": "warn",
                    "created_at": utcnow(),
                }
            )
            continue

        # Build the per-cofounder discovery plan: keyword searches + seed
        # fetches. Each entry is (kind, payload, source, classification).
        plan: list[tuple[str, str, str, str]] = []
        for kw in _rotate(tier_1, DISCOVERY_TIER_1_PER_RUN):
            plan.append(("kw", kw, "tier_1_kw", "A"))
        for kw in _rotate(tier_2, DISCOVERY_TIER_2_PER_RUN):
            plan.append(("kw", kw, "tier_2_kw", "B"))
        for kw in _rotate(tier_3, DISCOVERY_TIER_3_PER_RUN):
            plan.append(("kw", kw, "tier_3_kw", "B"))
        for seed in seeds:
            seed_source = (
                "manual_seed"
                if seed.get("source") == "manual"
                else "embedded_harvest"
            )
            url = seed.get("linkedin_url")
            if url:
                plan.append(("user", url, seed_source, "B"))
            else:
                # Name-only seeds fall back to keyword search.
                name = seed.get("extracted_name") or ""
                title = seed.get("extracted_title") or ""
                company = seed.get("extracted_company") or ""
                if title and company:
                    query = f"{name} {title} {company}".strip()
                elif company:
                    query = f"{name} {company}".strip()
                else:
                    query = name
                if query:
                    plan.append(("kw", query, seed_source, "B"))

        for kind, payload, source, classification in plan:
            try:
                if kind == "kw":
                    posts = search_posts(account_id=account_id, query=payload, limit=20)
                else:  # kind == "user"
                    posts = get_user_posts(
                        account_id=account_id,
                        public_identifier_or_url=payload,
                        limit=10,
                    )
            except UnipileNotConfigured as err:
                log.warning("discovery: unipile unconfigured, halting: %s", err)
                return inserted
            except UnipileError as err:
                log.warning(
                    "discovery: unipile %s failed for %r: %s", kind, payload, err
                )
                continue

            for post in posts:
                if not post.url or post.url in seen_urls:
                    continue
                if _is_exhausted(db, operator_id, post.author_profile_url or post.url):
                    continue
                seen_urls.add(post.url)

                db.candidates.insert_one(
                    _candidate_doc(
                        operator_id=operator_id,
                        cofounder_id=cofounder_id,
                        slate_run_id=slate_run_id,
                        post=post,
                        source=source,
                        source_keyword=payload if kind == "kw" else "",
                        source_classification=classification,
                    )
                )
                inserted += 1

    # Only mark *harvester* seeds as used — manual contacts are recurring.
    harvested_ids = [s["_id"] for s in seeds if s.get("source") != "manual"]
    _mark_seeds_used(db, harvested_ids)
    log.info(
        "discovery: inserted %d candidates for operator=%s cofounders=%d",
        inserted,
        operator_id,
        len(cofounders),
    )
    return inserted


def _mark_seeds_used(db: Database, seed_ids: list[ObjectId]) -> None:
    if not seed_ids:
        return
    db.discovery_seeds.update_many(
        {"_id": {"$in": seed_ids}},
        {"$set": {"status": "used", "updated_at": utcnow()}},
    )
