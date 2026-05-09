"""
Discovery stage: 5-source priority pipeline.

  1. apidirect synchronous keyword search — primary source. Fast + cheap
     keyword-based LinkedIn post search. Skipped when not configured, in
     mock mode, or circuit-broken (402 quota).
  2. Exa semantic LinkedIn search — high-volume neural search, ~50 posts/call.
  3. Crustdata inbox drain — pre-filtered posts pushed asynchronously by
     Crustdata's `linkedin-post-with-keyword` watch (keyword + author_title
     + industry + post_intent + headcount, all evaluated upstream).
  4. Unipile keyword + seed-author search — runs against the cofounder's
     connected LinkedIn account. Acts as a fallback / supplemental source.
  5. Contact seeds — imported contacts (CSV/Excel/manual) searched via
     apidirect + Exa by name/title/company. Tagged source="contact_seed".

Each source is gated by a `discovery_use_*` config flag so operators can
disable any layer without code changes. All sources merge into the same
`candidates` collection, tagged with `source`. Dedupe is global per operator:
a post URL seen anywhere in the last EXHAUSTION_LOOKBACK_DAYS window is
dropped before insert. Authors already shipped to are also dropped
(belt-and-suspenders alongside the exhaustion ledger).
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.engine.constants import (
    DISCOVERY_TIER_1_PER_RUN,
    DISCOVERY_TIER_2_PER_RUN,
    DISCOVERY_TIER_3_PER_RUN,
    EXA_DISCOVERY_TIER_1_PER_RUN,
    EXA_DISCOVERY_TIER_2_PER_RUN,
    EXA_DISCOVERY_TIER_3_PER_RUN,
    EXHAUSTION_LOOKBACK_DAYS,
)
from app.models.common import utcnow
from app.services.apidirect import (
    ApiDirectError,
    ApiDirectNotConfigured,
    ApiDirectQuotaExhausted,
    LinkedInPost as ApiDirectPost,
    search_linkedin_posts,
)
from app.services.exa import (
    ExaError,
    ExaNotConfigured,
    ExaPost,
    ExaQuotaExhausted,
    search_linkedin_posts as exa_search_linkedin_posts,
)
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


def _seen_post_urls(db: Database, operator_id: ObjectId) -> set[str]:
    """All post URLs we've already inserted as candidates within the lookback
    window. Used to prevent re-discovery of the same post across runs (the
    exhaustion ledger only tracks authors, not posts)."""
    cutoff = utcnow() - timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    cursor = db.candidates.find(
        {
            "operator_id": operator_id,
            "created_at": {"$gte": cutoff},
            "post_url": {"$exists": True, "$nin": [None, ""]},
        },
        {"post_url": 1},
    )
    return {doc["post_url"] for doc in cursor if doc.get("post_url")}


def _seen_author_urls(db: Database, operator_id: ObjectId) -> set[str]:
    """All author URLs we've engaged with (shipped) within the lookback window.
    Belt-and-suspenders alongside the exhaustion ledger."""
    cutoff = utcnow() - timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    cursor = db.candidates.find(
        {
            "operator_id": operator_id,
            "status": "shipped",
            "shipped_at": {"$gte": cutoff},
            "author_linkedin_url": {"$exists": True, "$nin": [None, ""]},
        },
        {"author_linkedin_url": 1},
    )
    return {
        doc["author_linkedin_url"]
        for doc in cursor
        if doc.get("author_linkedin_url")
    }


def _candidate_doc(
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    post_url: str,
    post_id: str | None,
    author_name: str | None,
    author_title: str | None,
    author_company: str | None,
    author_linkedin_url: str | None,
    post_text: str,
    post_published_at: Any,
    source: str,
    source_keyword: str,
    source_classification: str,
) -> dict[str, Any]:
    now = utcnow()
    return {
        "operator_id": operator_id,
        "cofounder_id": cofounder_id,
        "slate_run_id": slate_run_id,
        "post_url": post_url,
        "post_id": post_id,
        "author_name": author_name,
        "author_title": author_title,
        "author_company": author_company,
        "author_linkedin_url": author_linkedin_url,
        "post_text": post_text,
        "post_published_at": post_published_at,
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


def _doc_from_unipile(
    post: UnipilePost,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source: str,
    source_keyword: str,
    source_classification: str,
) -> dict[str, Any]:
    return _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=post.id,
        author_name=post.author_name,
        author_title=post.author_title,
        author_company=post.author_company,
        author_linkedin_url=post.author_profile_url,
        post_text=post.text,
        post_published_at=post.published_at,
        source=source,
        source_keyword=source_keyword,
        source_classification=source_classification,
    )


def _doc_from_apidirect(
    post: ApiDirectPost,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source_keyword: str,
    source_classification: str,
) -> dict[str, Any]:
    return _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=None,
        author_name=post.author,
        author_title=None,
        author_company=None,
        author_linkedin_url=None,
        post_text=post.snippet or post.title or "",
        post_published_at=post.published_at,
        source="apidirect_kw",
        source_keyword=source_keyword,
        source_classification=source_classification,
    )


def _doc_from_exa(
    post: ExaPost,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source_keyword: str,
    source_classification: str,
) -> dict[str, Any]:
    # Pre-derive author URL from the LinkedIn post slug so profile_resolve
    # can enrich it. /pulse/ and /feed/update/ URLs return None and stay
    # unenrichable (Crustdata has nothing to look up).
    from app.engine.stages.verification import _author_url_from_post_url
    return _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=None,
        author_name=post.author,
        author_title=None,
        author_company=None,
        author_linkedin_url=_author_url_from_post_url(post.url),
        post_text=post.snippet or post.title or "",
        post_published_at=post.published_at,
        source="exa_kw",
        source_keyword=source_keyword,
        source_classification=source_classification,
    )


def _doc_from_inbox(
    inbox_row: dict[str, Any],
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
) -> dict[str, Any]:
    return _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=inbox_row.get("post_url") or "",
        post_id=inbox_row.get("post_uid"),
        author_name=inbox_row.get("author_name"),
        author_title=inbox_row.get("author_title"),
        author_company=inbox_row.get("author_company"),
        author_linkedin_url=inbox_row.get("author_linkedin_url"),
        post_text=inbox_row.get("post_text") or "",
        post_published_at=inbox_row.get("date_posted"),
        source="crustdata",
        source_keyword="",
        source_classification="A",  # Crustdata's filters mirror tier-1 ICP
    )


def _drain_crustdata_inbox(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    seen_urls: set[str],
    seen_authors_shipped: set[str],
) -> int:
    """Pull unconsumed Crustdata posts for this cofounder, insert as
    candidates, mark consumed. Returns count inserted."""
    cutoff = utcnow() - timedelta(hours=settings.crustdata_inbox_lookback_hours)
    cursor = db.crustdata_inbox.find(
        {
            "cofounder_id": str(cofounder_id),
            "consumed": False,
            "received_at": {"$gte": cutoff},
        }
    ).sort("received_at", -1)

    inserted = 0
    for row in cursor:
        post_url = row.get("post_url") or ""
        if not post_url or post_url in seen_urls:
            db.crustdata_inbox.update_one(
                {"_id": row["_id"]},
                {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "duplicate_url"}},
            )
            continue
        author_url = row.get("author_linkedin_url") or ""
        if author_url and author_url in seen_authors_shipped:
            db.crustdata_inbox.update_one(
                {"_id": row["_id"]},
                {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "author_shipped"}},
            )
            continue
        if _is_exhausted(db, operator_id, author_url or post_url):
            db.crustdata_inbox.update_one(
                {"_id": row["_id"]},
                {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "exhausted"}},
            )
            continue
        seen_urls.add(post_url)
        db.candidates.insert_one(
            _doc_from_inbox(
                row,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
            )
        )
        db.crustdata_inbox.update_one(
            {"_id": row["_id"]},
            {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "inserted"}},
        )
        inserted += 1
    return inserted


def _apidirect_enabled() -> bool:
    """Apidirect runs only when explicitly configured AND not in mock mode AND
    the operator hasn't disabled it via DISCOVERY_USE_APIDIRECT. Mock mode
    would pollute the slate with canned posts."""
    return (
        settings.discovery_use_apidirect
        and bool(settings.apidirect_api_key)
        and not settings.apidirect_mock
    )


def _exa_enabled() -> bool:
    return settings.discovery_use_exa and bool(settings.exa_api_key)


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
    # Pre-seed the in-run dedupe set with every post_url we've already
    # inserted in the last 90 days, so re-running the engine never surfaces
    # the same post twice.
    seen_urls: set[str] = _seen_post_urls(db, operator_id)
    seen_authors_shipped: set[str] = _seen_author_urls(db, operator_id)
    log.info(
        "discovery: pre-seeded dedupe sets — %d seen post URLs, %d shipped authors",
        len(seen_urls),
        len(seen_authors_shipped),
    )

    apidirect_on = _apidirect_enabled()
    exa_on = _exa_enabled()
    crustdata_on = settings.discovery_use_crustdata
    unipile_on = settings.discovery_use_unipile
    log.info(
        "discovery: source order = %s%s%s%s",
        "apidirect → " if apidirect_on else "(apidirect off) → ",
        "exa → " if exa_on else "(exa off) → ",
        "crustdata_inbox → " if crustdata_on else "(crustdata off) → ",
        "unipile" if unipile_on else "(unipile off)",
    )

    # Recency window for Exa's startPublishedDate — passes our recency filter
    # downstream and saves Exa from returning years-old posts.
    from datetime import date as _date
    exa_after = (
        (utcnow().date() - timedelta(days=settings.discovery_max_age_days)).isoformat()
        if settings.discovery_max_age_days > 0
        else None
    )

    # Manual contact seeds — searched via apidirect + exa (separate from keyword pool)
    contact_seeds = [s for s in seeds if s.get("source") == "manual"]
    contacts_on = bool(contact_seeds)
    log.info(
        "discovery: %d contact seeds loaded for operator=%s",
        len(contact_seeds),
        operator_id,
    )

    for cofounder in cofounders:
        cofounder_id: ObjectId = cofounder["_id"]
        apidirect_inserted = 0
        exa_inserted = 0
        crustdata_inserted = 0
        unipile_inserted = 0
        contacts_inserted = 0

        # ── SOURCE 1: apidirect synchronous keyword search ─────────────────
        if apidirect_on:
            apidirect_inserted = _run_apidirect(
                db,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                tier_1=tier_1,
                tier_2=tier_2,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
            )
            inserted += apidirect_inserted

        # ── SOURCE 2: Exa LinkedIn-scoped search (high numResults per call) ──
        if exa_on:
            exa_inserted = _run_exa(
                db,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                tier_1=tier_1,
                tier_2=tier_2,
                tier_3=tier_3,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
                start_published_date=exa_after,
            )
            inserted += exa_inserted

        # ── SOURCE 3: Crustdata inbox (zero-cost; pre-filtered upstream) ──
        if crustdata_on:
            crustdata_inserted = _drain_crustdata_inbox(
                db,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
            )
            inserted += crustdata_inserted

        # ── SOURCE 3: Unipile keyword + seed-author search ─────────────────
        if unipile_on:
            account_id = cofounder.get("unipile_account_id")
            if not account_id:
                log.warning(
                    "discovery: cofounder %s has no unipile_account_id — "
                    "skipping Unipile (apidirect=%d, crustdata=%d already in)",
                    cofounder_id,
                    apidirect_inserted,
                    crustdata_inserted,
                )
                db.audit_records.insert_one(
                    {
                        "operator_id": operator_id,
                        "event_type": "stage_error",
                        "stage": "discovery",
                        "details": {
                            "cofounder_id": str(cofounder_id),
                            "reason": "no_unipile_account",
                            "apidirect_inserted": apidirect_inserted,
                            "crustdata_inserted": crustdata_inserted,
                        },
                        "severity": "warn",
                        "created_at": utcnow(),
                    }
                )
            else:
                unipile_inserted = _run_unipile(
                    db,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    account_id=account_id,
                    tier_1=tier_1,
                    tier_2=tier_2,
                    tier_3=tier_3,
                    seeds=seeds,
                    seen_urls=seen_urls,
                    seen_authors_shipped=seen_authors_shipped,
                )
                inserted += unipile_inserted

        # ── SOURCE 5: Contact seeds (apidirect + Exa search by name) ─────
        if contacts_on:
            contacts_inserted = _run_contact_seeds(
                db,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                seeds=contact_seeds,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
                start_published_date=exa_after,
            )
            inserted += contacts_inserted

        log.info(
            "discovery: cofounder=%s total=%d (apidirect=%d, exa=%d, crustdata=%d, unipile=%d, contacts=%d)",
            cofounder_id,
            apidirect_inserted + exa_inserted + crustdata_inserted + unipile_inserted + contacts_inserted,
            apidirect_inserted,
            exa_inserted,
            crustdata_inserted,
            unipile_inserted,
            contacts_inserted,
        )

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


def _run_apidirect(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    tier_1: list[str],
    tier_2: list[str],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
) -> int:
    """Synchronous keyword search via apidirect. Trips its own circuit on 402;
    we just stop calling it for the rest of the run."""
    plan: list[tuple[str, str]] = []
    for kw in _rotate(tier_1, DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(tier_2, DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))

    inserted = 0
    for query, classification in plan:
        try:
            posts = search_linkedin_posts(query)
        except (ApiDirectQuotaExhausted, ApiDirectNotConfigured) as err:
            log.warning("discovery: apidirect halted mid-run: %s", err)
            return inserted
        except ApiDirectError as err:
            log.warning("discovery: apidirect %r failed: %s", query, err)
            continue

        for post in posts:
            if not post.url or post.url in seen_urls:
                continue
            if _is_exhausted(db, operator_id, post.url):
                continue
            seen_urls.add(post.url)
            db.candidates.insert_one(
                _doc_from_apidirect(
                    post,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source_keyword=query,
                    source_classification=classification,
                )
            )
            inserted += 1
    return inserted


def _run_exa(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    tier_1: list[str],
    tier_2: list[str],
    tier_3: list[str],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
    start_published_date: str | None,
) -> int:
    """Exa LinkedIn-scoped semantic search. One API call per keyword, up to
    ~100 results per call (configurable via EXA_RESULTS_PER_QUERY, capped at 100).
    Uses a wider keyword plan than apidirect/unipile so each run pulls enough
    raw posts to survive downstream gates. Trips the circuit on 401/402/429."""
    plan: list[tuple[str, str]] = []
    for kw in _rotate(tier_1, EXA_DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(tier_2, EXA_DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))
    for kw in _rotate(tier_3, EXA_DISCOVERY_TIER_3_PER_RUN):
        plan.append((kw, "B"))

    inserted = 0
    for query, classification in plan:
        try:
            posts = exa_search_linkedin_posts(
                query, start_published_date=start_published_date
            )
        except (ExaQuotaExhausted, ExaNotConfigured) as err:
            log.warning("discovery: exa halted mid-run: %s", err)
            return inserted
        except ExaError as err:
            log.warning("discovery: exa %r failed: %s", query, err)
            continue

        for post in posts:
            if not post.url or post.url in seen_urls:
                continue
            if _is_exhausted(db, operator_id, post.url):
                continue
            seen_urls.add(post.url)
            db.candidates.insert_one(
                _doc_from_exa(
                    post,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source_keyword=query,
                    source_classification=classification,
                )
            )
            inserted += 1
    return inserted


def _run_unipile(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    account_id: str,
    tier_1: list[str],
    tier_2: list[str],
    tier_3: list[str],
    seeds: list[dict[str, Any]],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
) -> int:
    plan: list[tuple[str, str, str, str]] = []
    for kw in _rotate(tier_1, DISCOVERY_TIER_1_PER_RUN):
        plan.append(("kw", kw, "tier_1_kw", "A"))
    for kw in _rotate(tier_2, DISCOVERY_TIER_2_PER_RUN):
        plan.append(("kw", kw, "tier_2_kw", "B"))
    for kw in _rotate(tier_3, DISCOVERY_TIER_3_PER_RUN):
        plan.append(("kw", kw, "tier_3_kw", "B"))
    for seed in seeds:
        seed_source = (
            "manual_seed" if seed.get("source") == "manual" else "embedded_harvest"
        )
        url = seed.get("linkedin_url")
        if url:
            plan.append(("user", url, seed_source, "B"))
        else:
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

    inserted = 0
    for kind, payload, source, classification in plan:
        try:
            if kind == "kw":
                posts = search_posts(account_id=account_id, query=payload, limit=20)
            else:
                posts = get_user_posts(
                    account_id=account_id,
                    public_identifier_or_url=payload,
                    limit=10,
                )
        except UnipileNotConfigured as err:
            log.warning("discovery: unipile unconfigured, halting: %s", err)
            return inserted
        except UnipileError as err:
            log.warning("discovery: unipile %s failed for %r: %s", kind, payload, err)
            continue

        for post in posts:
            if not post.url or post.url in seen_urls:
                continue
            author_url = post.author_profile_url or ""
            if author_url and author_url in seen_authors_shipped:
                continue
            if _is_exhausted(db, operator_id, author_url or post.url):
                continue
            seen_urls.add(post.url)
            db.candidates.insert_one(
                _doc_from_unipile(
                    post,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source=source,
                    source_keyword=payload if kind == "kw" else "",
                    source_classification=classification,
                )
            )
            inserted += 1
    return inserted


def _run_contact_seeds(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    seeds: list[dict[str, Any]],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
    start_published_date: str | None,
) -> int:
    """Search for posts by imported contacts via apidirect + Exa.

    For each seed with a LinkedIn URL we search for their name / slug as a
    keyword query. For seeds without a URL we build a name+title+company
    query. Results are tagged source="contact_seed" so they sort ahead of
    anonymous keyword discoveries in the gate phase.
    """
    if not seeds:
        return 0

    apidirect_on = _apidirect_enabled()
    exa_on = _exa_enabled()
    if not apidirect_on and not exa_on:
        log.info("discovery: contact_seeds skipped — no search source enabled")
        return 0

    inserted = 0
    for seed in seeds:
        # Build a search query from the seed's metadata
        name = seed.get("extracted_name") or ""
        title = seed.get("extracted_title") or ""
        company = seed.get("extracted_company") or ""
        linkedin_url = seed.get("linkedin_url") or ""

        # Derive search query: prefer "name title company" for best recall
        parts = [name]
        if title:
            parts.append(title)
        if company:
            parts.append(company)
        query = " ".join(parts).strip()

        # If only a URL, derive the slug as the search query
        if not query and linkedin_url:
            slug = linkedin_url.rstrip("/").rsplit("/", 1)[-1] if "/in/" in linkedin_url else ""
            query = slug.replace("-", " ").strip()
        if not query:
            continue

        # Track the seed's author URL for dedupe
        seed_author_url = linkedin_url or None
        if seed_author_url and seed_author_url in seen_authors_shipped:
            continue
        if seed_author_url and _is_exhausted(db, operator_id, seed_author_url):
            continue

        # ── apidirect search ──────────────────────────────────────────────
        if apidirect_on:
            try:
                posts = search_linkedin_posts(query)
            except (ApiDirectQuotaExhausted, ApiDirectNotConfigured) as err:
                log.warning("discovery: contact_seeds apidirect halted: %s", err)
            except ApiDirectError as err:
                log.warning("discovery: contact_seeds apidirect %r failed: %s", query, err)
            else:
                for post in posts:
                    if not post.url or post.url in seen_urls:
                        continue
                    if _is_exhausted(db, operator_id, post.url):
                        continue
                    seen_urls.add(post.url)
                    db.candidates.insert_one(
                        _candidate_doc(
                            operator_id=operator_id,
                            cofounder_id=cofounder_id,
                            slate_run_id=slate_run_id,
                            post_url=post.url,
                            post_id=None,
                            author_name=post.author or name,
                            author_title=title or None,
                            author_company=company or None,
                            author_linkedin_url=seed_author_url,
                            post_text=post.snippet or post.title or "",
                            post_published_at=post.published_at,
                            source="contact_seed",
                            source_keyword=query,
                            source_classification="A",
                        )
                    )
                    inserted += 1

        # ── Exa search ────────────────────────────────────────────────────
        if exa_on:
            try:
                exa_posts = exa_search_linkedin_posts(
                    query,
                    num_results=30,
                    start_published_date=start_published_date,
                )
            except (ExaQuotaExhausted, ExaNotConfigured) as err:
                log.warning("discovery: contact_seeds exa halted: %s", err)
            except ExaError as err:
                log.warning("discovery: contact_seeds exa %r failed: %s", query, err)
            else:
                from app.engine.stages.verification import _author_url_from_post_url
                for post in exa_posts:
                    if not post.url or post.url in seen_urls:
                        continue
                    if _is_exhausted(db, operator_id, post.url):
                        continue
                    seen_urls.add(post.url)
                    # Prefer seed's author URL; fall back to URL extraction
                    author_url = seed_author_url or _author_url_from_post_url(post.url)
                    db.candidates.insert_one(
                        _candidate_doc(
                            operator_id=operator_id,
                            cofounder_id=cofounder_id,
                            slate_run_id=slate_run_id,
                            post_url=post.url,
                            post_id=None,
                            author_name=post.author or name,
                            author_title=title or None,
                            author_company=company or None,
                            author_linkedin_url=author_url,
                            post_text=post.snippet or post.title or "",
                            post_published_at=post.published_at,
                            source="contact_seed",
                            source_keyword=query,
                            source_classification="A",
                        )
                    )
                    inserted += 1

    log.info(
        "discovery: contact_seeds searched %d contacts, inserted %d candidates",
        len(seeds),
        inserted,
    )
    return inserted


def _mark_seeds_used(db: Database, seed_ids: list[ObjectId]) -> None:
    if not seed_ids:
        return
    db.discovery_seeds.update_many(
        {"_id": {"$in": seed_ids}},
        {"$set": {"status": "used", "updated_at": utcnow()}},
    )
