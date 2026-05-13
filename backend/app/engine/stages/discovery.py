"""
Discovery stage: 5-source priority pipeline.

  1. **Unipile** keyword post search (PRIMARY) — `search_posts_pages` against
     the classic LinkedIn search API, then per-author enrichment via the free
     ``GET /users/{slug}`` endpoint (cached cross-run in
     ``unipile_author_cache``), then dual-path rubric qualification against
     the operator's own ICP fields. Path A passes on the enriched author
     rubric (title 5 + industry 3 + geo 2); Path B passes on post text
     keyword-tier hits (tier_1=3, tier_2=2, tier_3=1, capped at 12). Geo is
     required on both paths when the operator has ``target_geographies``
     set. Inline-enriched candidates carry ``enriched_inline`` for downstream
     bookkeeping. Reference:
     ``unipile_hybrid_sweep.py``.
  2. Crustdata — (a) inbox drain of webhook-written Mongo rows; (b) optional
     realtime ``POST /screener/linkedin_posts/keyword_search/``; (c) optional
     simulation ping on empty yield.
  3. apidirect synchronous keyword search — fast + cheap keyword LinkedIn
     post search. Skipped when not configured, in mock mode, or
     circuit-broken (402 quota).
  4. Exa semantic LinkedIn search — high-volume neural search, ~50 posts/call.
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
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.engine.stages.gates import post_quality
from app.engine.constants import (
    DISCOVERY_TIER_1_PER_RUN,
    DISCOVERY_TIER_2_PER_RUN,
    DISCOVERY_TIER_3_PER_RUN,
    DISCOVERY_TITLE_INDUSTRY_PER_RUN,
    DISCOVERY_TITLE_SEARCH_PEOPLE_PER_QUERY,
    DISCOVERY_TITLE_SEARCH_POSTS_PER_PERSON,
    DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN,
    EXA_DISCOVERY_TIER_1_PER_RUN,
    EXA_DISCOVERY_TIER_2_PER_RUN,
    EXA_DISCOVERY_TIER_3_PER_RUN,
    EXHAUSTION_LOOKBACK_DAYS,
)
from app.models.common import utcnow
from app.services import client_config
from app.services.geo_resolver import is_us_location
from app.services.apidirect import (
    ApiDirectError,
    ApiDirectNotConfigured,
    ApiDirectQuotaExhausted,
    LinkedInPost as ApiDirectPost,
    LinkedInPostDetails,
    get_linkedin_post_details,
    search_linkedin_posts_pages,
)
from app.services.exa import (
    ExaError,
    ExaNotConfigured,
    ExaPost,
    ExaQuotaExhausted,
    reset_exa_circuit,
    search_linkedin_posts as exa_search_linkedin_posts,
)
from app.services.unipile import (
    UnipileError,
    UnipileNotConfigured,
    UnipilePerson,
    UnipilePost,
    get_user_posts,
    resolve_location_ids_from_text,
    search_people,
    search_posts_pages,
)
from app.services.crustdata import (
    CrustdataError,
    CrustdataNotConfigured,
    CrustdataQuotaExhausted,
    author_profile_url_from_screener_post,
    screener_keyword_search_posts,
)

log = logging.getLogger(__name__)

_SENIORITY_HINT_RE = re.compile(
    r"\b(?:c[\s]?suite|chief|evp|svp|vp|vice\s+president|director|head|partner|"
    r"president|cfo|coo|ceo|cto|cmo|managing\s+director)\b",
    re.I,
)


def _resolve_industry_ids_for_operator(
    db: Database,
    *,
    operator_id: ObjectId,
    operator: dict[str, Any],
    account_id: str,
    max_terms: int = 25,
    max_ids_per_term: int = 2,
) -> list[str]:
    """Resolve the operator's target industries → Unipile/LinkedIn INDUSTRY
    parameter IDs, cached cross-run.

    The Unipile people-search ``industry`` body field accepts an array of
    digit-string IDs (e.g. ``["14"]`` for Hospital & Health Care). We look
    them up once per run via GET /linkedin/search/parameters?type=INDUSTRY
    and cache them in ``unipile_industry_id_cache`` keyed by the operator.

    Returns at most ``max_terms × max_ids_per_term`` IDs to keep the
    request lean (LinkedIn's filter is OR-style, more IDs = wider net).
    """
    from app.services.unipile import (
        search_parameter_ids,
        UnipileError,
        UnipileNotConfigured,
    )

    ext = operator.get("product_extracted") or {}
    target_inds = [str(s).strip() for s in (ext.get("target_industries") or []) if str(s).strip()]
    if not target_inds:
        return []

    # Cross-run cache. Industry IDs don't change at LinkedIn's level, so a
    # 30-day TTL is fine; we just want to avoid the PARAMETERS lookup on
    # every run for the same operator.
    cache_key = {"operator_id": operator_id, "kind": "industry_ids"}
    cached = db.unipile_industry_id_cache.find_one(cache_key) if hasattr(db, "unipile_industry_id_cache") else None
    try:
        cached = db["unipile_industry_id_cache"].find_one(cache_key)
    except Exception:
        cached = None
    if cached:
        ttl_cutoff = utcnow() - timedelta(days=30)
        fetched_at = cached.get("fetched_at")
        # Mongo stores datetimes tz-naive; coerce to UTC-aware before
        # comparing against utcnow() (tz-aware) to avoid TypeError.
        if fetched_at is not None and getattr(fetched_at, "tzinfo", None) is None:
            from datetime import timezone as _tz
            fetched_at = fetched_at.replace(tzinfo=_tz.utc)
        if fetched_at and fetched_at >= ttl_cutoff:
            return list(cached.get("ids") or [])

    resolved: list[str] = []
    seen: set[str] = set()
    for term in target_inds[:max_terms]:
        try:
            hits = search_parameter_ids(
                account_id=account_id, type="INDUSTRY", keywords=term, limit=max_ids_per_term
            )
        except (UnipileError, UnipileNotConfigured) as err:
            log.warning("discovery: industry lookup failed for term=%r: %s", term, err)
            continue
        for h in hits[:max_ids_per_term]:
            sid = str(h.id).strip() if hasattr(h, "id") else ""
            if sid and sid not in seen:
                seen.add(sid)
                resolved.append(sid)

    if resolved:
        db["unipile_industry_id_cache"].update_one(
            cache_key,
            {"$set": {**cache_key, "ids": resolved, "fetched_at": utcnow()}},
            upsert=True,
        )
        log.info(
            "discovery: resolved %d industry IDs for operator (cached 30d): %s",
            len(resolved), resolved,
        )
    return resolved


def _operator_primary_geography(operator: dict[str, Any]) -> str | None:
    """First entry of ``operator.product_extracted.target_geographies`` (or
    the first ``geography.tiers[0].matches`` entry on the rubric if extracted
    is empty). Used as the default Location for Exa candidates that have no
    enriched location — Exa's API doesn't expose a location field, so we
    treat its results as operator-primary-geo unless the LLM finds explicit
    counter-evidence in the post text."""
    ext = operator.get("product_extracted") or {}
    raw = ext.get("target_geographies") or []
    for x in raw:
        s = str(x).strip()
        if len(s) >= 2:
            return s
    rub = operator.get("icp_rubric") or {}
    tiers = (rub.get("geography") or {}).get("tiers") or []
    if tiers:
        for m in (tiers[0].get("matches") or []):
            s = str(m).strip()
            if len(s) >= 2:
                return s
    return None


def _operator_targets_us(operator: dict[str, Any]) -> bool:
    """True iff any of the operator's geography terms (target_geographies or
    rubric.geography.tiers[*].matches) resolves to a US signal.

    Used to switch the inline-geo gate from a brittle substring rubric to
    the `geo_resolver.is_us_location` resolver. For US-targeting operators
    we trust the offline geonamescache resolver across keyword Unipile and
    APIDirect paths (direct-author RULE 24 and Exa already enforce geo at
    the source so they're untouched)."""
    ext = operator.get("product_extracted") or {}
    raw_geos: list[str] = []
    for x in ext.get("target_geographies") or []:
        if isinstance(x, str) and x.strip():
            raw_geos.append(x.strip())
    rub = operator.get("icp_rubric") or {}
    for tier in (rub.get("geography") or {}).get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        for m in tier.get("matches") or []:
            if isinstance(m, str) and m.strip():
                raw_geos.append(m.strip())
    for g in raw_geos:
        if is_us_location(g) is True:
            return True
    return False


def _operator_geo_terms(operator: dict[str, Any]) -> list[str]:
    """Geography phrases from onboarding / ICP for query bias + post filter.

    Preserves human casing (e.g. ``San Francisco``) for Unipile PARAMETERS
    lookup; dedupes case-insensitively."""
    ext = operator.get("product_extracted") or {}
    raw = ext.get("target_geographies") or []
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if len(s) >= 2:
            out.append(s)
    rub = operator.get("icp_rubric") or {}
    geo = rub.get("geography") or {}
    for tier in geo.get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        for m in tier.get("matches") or []:
            s = str(m).strip()
            if len(s) >= 2:
                out.append(s)
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        key = s.casefold()
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq[:15]


# Generic company-type tokens used to expand `target_titles` × company-type
# into title_industry queries when the operator's curated pool doesn't cover
# the long tail (small practices, founder-led orgs, specialty clinics).
# Kept short and ICP-agnostic — operator's actual industry preferences are
# enforced server-side via the LinkedIn INDUSTRY filter on people-search.
_GENERIC_COMPANY_TYPE_TOKENS: tuple[str, ...] = (
    "hospital",
    "health system",
    "medical group",
    "medical practice",
    "physician group",
    "specialty clinic",
    "FQHC",
    "ambulatory",
)


def _expand_title_industry_queries(
    operator: dict[str, Any], *, max_queries: int = 320
) -> list[str]:
    """Cross-product expander: ``target_titles × _GENERIC_COMPANY_TYPE_TOKENS``.

    Used to supplement the curated ``title_industry`` pool so RULE 24 catches
    the long tail of small-practice / founder-CEO / specialty-clinic variants
    that the hand-curated list misses (e.g., ``"CEO medical practice"``,
    ``"CEO physician group"`` — both essential for catching independent-
    physician CEOs like cardiology practice owners).

    Outer loop is COMPANY TYPE so every title gets at least one query per
    company-type token before any title gets a second one; this guarantees
    even short-pool coverage when ``max_queries`` truncates. The discovery
    loop's ``_rotate`` then samples queries randomly per run."""
    ext = operator.get("product_extracted") or {}
    titles = ext.get("target_titles") or []
    if not isinstance(titles, list):
        return []
    titles_clean = [t.strip() for t in titles if isinstance(t, str) and t.strip()]
    if not titles_clean:
        return []
    out: list[str] = []
    for ind in _GENERIC_COMPANY_TYPE_TOKENS:
        for title in titles_clean:
            out.append(f"{title} {ind}")
            if len(out) >= max_queries:
                return out
    return out


def _mark_geo_verified_by_resolver(
    doc: dict[str, Any],
    operator: dict[str, Any],
    enriched_profile: dict[str, Any] | None,
) -> None:
    """If the operator targets US AND the candidate has a REAL enriched
    location string that resolves to US via the geonamescache resolver,
    mark ``geo_verified_at_source=True`` on the candidate doc. The LLM ICP
    scoring gate then auto-credits the geography axis at the rubric's top
    tier instead of re-scoring from rubric (`icp_scoring.evaluate` honors
    this flag).

    Skip when ``enriched_profile`` has no real location, when the location
    is a synthesized fallback (e.g. Exa's ``_location_source='exa_default'``),
    or when the resolver returns anything other than True. Conservative —
    we only set the flag when we have positive evidence."""
    if not _operator_targets_us(operator):
        return
    if not enriched_profile:
        return
    loc_source = (enriched_profile.get("_location_source") or "").strip().lower()
    if loc_source == "exa_default":
        return  # synthesized — not real verification
    loc = (enriched_profile.get("location") or "").strip()
    if not loc:
        return
    if is_us_location(loc) is True:
        doc["geo_verified_at_source"] = True


def _seniority_hints_from_titles(operator: dict[str, Any]) -> str:
    """Short phrase of seniority tokens mined from target job titles."""
    ext = operator.get("product_extracted") or {}
    titles = ext.get("target_titles") or []
    hints: set[str] = set()
    for t in titles:
        if not isinstance(t, str):
            continue
        for m in _SENIORITY_HINT_RE.finditer(t):
            hints.add(m.group(0).strip())
    if not hints:
        return ""
    return " ".join(sorted(hints, key=len, reverse=True))[:160]


def _unipile_post_location_ids_for_operator(
    account_id: str,
    operator: dict[str, Any],
) -> list[str]:
    """LinkedIn geo id strings for classic post search ``body.location``.

    Order: ``DISCOVERY_UNIPILE_POST_LOCATION_IDS`` (comma-separated) if set;
    else when ``DISCOVERY_UNIPILE_POST_RESOLVE_LOCATION`` is true, resolve the
    first matching ICP geography via ``GET /linkedin/search/parameters``."""
    raw = (settings.discovery_unipile_post_location_ids or "").strip()
    if raw:
        ids = [x.strip() for x in raw.split(",") if x.strip()][:10]
        if ids:
            log.info(
                "discovery: unipile post search using configured location_ids=%s",
                ids,
            )
        return ids
    if not settings.discovery_unipile_post_resolve_location:
        return []
    if settings.unipile_mock or not (account_id or "").strip():
        return []
    for term in _operator_geo_terms(operator)[:8]:
        if not term.strip():
            continue
        try:
            ids = resolve_location_ids_from_text(
                account_id=account_id,
                location_text=term,
            )
        except (UnipileError, UnipileNotConfigured) as err:
            log.info(
                "discovery: unipile location PARAMETERS failed term=%r: %s",
                term[:60],
                err,
            )
            continue
        if ids:
            log.info(
                "discovery: unipile post search location_ids from PARAMETERS "
                "term=%r -> %s",
                term[:80],
                ids,
            )
            return ids
    log.info(
        "discovery: unipile post search no location_ids "
        "(no DISCOVERY_UNIPILE_POST_LOCATION_IDS and no PARAMETERS match)"
    )
    return []


def _compose_discovery_query(
    base_kw: str,
    operator: dict[str, Any],
    *,
    with_geo: bool = True,
    with_seniority: bool = True,
) -> str:
    """Append geography + seniority hints to keyword so LinkedIn search stays
    ICP-local. Vendors that enforce geo server-side (Unipile content search via
    ``location_ids``; Unipile people search via ``geoUrn`` default) should pass
    ``with_geo=False`` to avoid baking geo terms into the literal text-match
    keyword, which silently kills recall."""
    parts = [base_kw.strip()]
    if with_geo:
        geos = _operator_geo_terms(operator)
        if geos:
            # Cap geo terms at ONE — LinkedIn / APIDirect / Exa keyword
            # indices are strict text-match, so a 5-term geo list ("United
            # States Florida Texas Georgia US") tacked onto every keyword
            # collapses recall to zero. One country-level term is enough
            # text-bias; finer geo is enforced via location_ids/geoUrn for
            # the vendors that support it.
            parts.append(geos[0])
    if with_seniority:
        sen = _seniority_hints_from_titles(operator)
        if sen:
            parts.append(sen)
    q = " ".join(p for p in parts if p).strip()
    return q[:480]


def _rotate(values: list[str], n: int) -> list[str]:
    """Pick up to N keywords with mild shuffling so successive runs vary.

    ``n <= 0`` means **no limit** — return every keyword in the pool
    (still shuffled, so the order varies across runs). Used by tiers we
    want to sweep exhaustively (e.g. tier_1 on Unipile + all Exa tiers)."""
    if not values:
        return []
    pool = list(values)
    random.shuffle(pool)
    if n <= 0:
        return pool
    return pool[:n]


def _is_exhausted(db: Database, operator_id: ObjectId, url: str) -> bool:
    """True when this URL (author profile or canonical post URL) was touched
    recently in the exhaustion ledger (discovery insert or nightly shipped)."""
    if not settings.discovery_exhaustion_ledger_enabled:
        return False
    if not url:
        return False
    coll = getattr(db, "exhaustion_ledger", None)
    if coll is None:
        return False
    cutoff = utcnow() - timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    doc = coll.find_one({"operator_id": operator_id, "linkedin_url": url})
    if not doc:
        return False
    for key in ("last_seen_discovery_at", "last_engaged_at"):
        ts = doc.get(key)
        if ts is None:
            continue
        # Older Mongo rows stored datetimes as tz-naive (BSON drops tzinfo).
        # `cutoff` is tz-aware (from utcnow()). Normalize ts to UTC if naive
        # so the comparison doesn't raise.
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        if ts >= cutoff:
            return True
    return False


def _sanitize_per_source_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", (s or "unknown").strip())[:64] or "unknown"


def _discovery_record_insert(db: Database, doc: dict[str, Any]) -> None:
    """Increment slate run discovery counters + touch exhaustion ledger."""
    slate_run_id = doc.get("slate_run_id")
    operator_id = doc.get("operator_id")
    if slate_run_id is None or operator_id is None:
        return
    slate_runs = getattr(db, "slate_runs", None)
    if slate_runs is not None:
        inc: dict[str, Any] = {"total_discovered": 1}
        src = doc.get("source")
        if src:
            inc[f"per_source_counts.src_{_sanitize_per_source_key(str(src))}"] = 1
        ch = doc.get("source_channel")
        if ch:
            inc[f"per_source_counts.ch_{_sanitize_per_source_key(str(ch))}"] = 1
        slate_runs.update_one({"_id": slate_run_id}, {"$inc": inc})
    if not settings.discovery_exhaustion_ledger_enabled:
        return
    now = utcnow()
    expires = now + timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    urls: list[str] = []
    au = doc.get("author_linkedin_url")
    if isinstance(au, str) and au.strip():
        urls.append(au.strip())
    pu = doc.get("post_url")
    if isinstance(pu, str) and pu.strip():
        urls.append(_canonical_post_url(pu))
    ledger = getattr(db, "exhaustion_ledger", None)
    if ledger is None:
        return
    for u in urls:
        if not u:
            continue
        ledger.update_one(
            {"operator_id": operator_id, "linkedin_url": u},
            {
                "$set": {
                    "last_seen_discovery_at": now,
                    "expires_at": expires,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "operator_id": operator_id,
                    "linkedin_url": u,
                    "engagement_count": 0,
                    "created_at": now,
                },
            },
            upsert=True,
        )


def _ensure_slate_profile_fetch_budget(db: Database, slate_run_id: ObjectId) -> None:
    """Backfill fetch_budget_remaining for slate docs created before the field existed."""
    sr = getattr(db, "slate_runs", None)
    if sr is None:
        return
    cap = settings.discovery_unipile_max_profile_fetches_per_run
    sr.update_one(
        {"_id": slate_run_id, "fetch_budget_remaining": {"$exists": False}},
        {"$set": {"fetch_budget_remaining": cap}},
    )


def _try_consume_profile_fetch_budget(
    db: Database, slate_run_id: ObjectId | None,
) -> bool:
    """Atomically consume one profile-fetch slot for this slate run. No-op True when
    slate_run_id is None (tests). Returns False when budget is exhausted."""
    if slate_run_id is None:
        return True
    sr = getattr(db, "slate_runs", None)
    if sr is None:
        return True
    _ensure_slate_profile_fetch_budget(db, slate_run_id)
    res = sr.find_one_and_update(
        {"_id": slate_run_id, "fetch_budget_remaining": {"$gt": 0}},
        {"$inc": {"fetch_budget_remaining": -1}},
    )
    return res is not None


def _profile_fetch_budget_remaining(
    db: Database, slate_run_id: ObjectId | None,
) -> int | None:
    if slate_run_id is None:
        return None
    sr = getattr(db, "slate_runs", None)
    if sr is None:
        return None
    _ensure_slate_profile_fetch_budget(db, slate_run_id)
    doc = sr.find_one({"_id": slate_run_id}, {"fetch_budget_remaining": 1})
    if not doc:
        return None
    return int(doc.get("fetch_budget_remaining") or 0)


def _discovery_wall_clock_exceeded(t0: float, max_seconds: int) -> bool:
    if max_seconds <= 0:
        return False
    return (time.monotonic() - t0) >= float(max_seconds)


def _should_skip_remaining_discovery(
    db: Database, slate_run_id: ObjectId
) -> bool:
    """Return True if the operator (via UI / API) flagged this slate to
    abandon all remaining discovery sources and let the gates/allocator/
    drafter finish on what's already been inserted.

    Checked at the top of each major discovery source AND inside each
    per-query loop, so the operator-side cancellation is responsive within
    a few seconds. Cheap projection-only Mongo query (one document) — fine
    to run on every iteration."""
    try:
        doc = db.slate_runs.find_one(
            {"_id": slate_run_id, "skip_remaining_discovery": True},
            projection={"_id": 1},
        )
    except Exception:
        return False
    return doc is not None


def _seen_post_urls(db: Database, operator_id: ObjectId) -> set[str]:
    """All post URLs we've already inserted as candidates within the lookback
    window. Used to prevent re-discovery of the same post across runs (the
    exhaustion ledger only tracks authors, not posts).

    URLs are canonicalized as we read so legacy entries (stored before the
    canonicalization patch) still dedupe against new canonical writes."""
    cutoff = utcnow() - timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    cursor = db.candidates.find(
        {
            "operator_id": operator_id,
            "created_at": {"$gte": cutoff},
            "post_url": {"$exists": True, "$nin": [None, ""]},
        },
        {"post_url": 1},
    )
    return {
        _canonical_post_url(doc["post_url"]) for doc in cursor if doc.get("post_url")
    }


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
    source_channel: str = "",
) -> dict[str, Any]:
    """`source` stays vendor-specific (apidirect_kw / exa_kw / unipile_*) for
    debugging. `source_channel` is the audit-aligned tag (RULE 15:
    keyword_topical, RULE 15-EXT: keyword_title_industry, RULE 24:
    title_search) used by allocator/drafter routing."""
    now = utcnow()
    # Single chokepoint for URL canonicalization on the WRITE side. Every
    # vendor's doc-builder (_doc_from_unipile / _doc_from_apidirect /
    # _doc_from_exa / _doc_from_inbox / _doc_from_crustdata_screener) flows
    # through here, so canonicalizing once means every stored URL is dedup-
    # ready (no per-viewer rcm tokens, no UTM tracking).
    post_url = _canonical_post_url(post_url)
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
        "source_channel": source_channel,
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
    source_channel: str = "",
    enriched_profile: dict[str, Any] | None = None,
    unipile_rubric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a candidate doc from a Unipile search post.

    When ``enriched_profile`` is supplied (output of ``_get_cached_author_profile``),
    its headline / company / location override what the search payload returned —
    those fields are much more reliable from the `/users/{slug}` endpoint than
    from the search snippet. ``unipile_rubric`` carries the author/post scores
    that qualified the candidate so downstream stages (and the UI) can show
    the reasoning.

    When ``enriched_profile`` is set we also stamp ``enriched_inline=True``
    for downstream consumers (audits / UI)."""
    ep = enriched_profile or {}
    doc = _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=post.id,
        author_name=ep.get("name") or post.author_name,
        author_title=ep.get("headline") or post.author_title,
        author_company=ep.get("company") or post.author_company,
        author_linkedin_url=post.author_profile_url,
        post_text=post.text,
        post_published_at=post.published_at,
        source=source,
        source_keyword=source_keyword,
        source_classification=source_classification,
        source_channel=source_channel,
    )
    if enriched_profile:
        doc["enriched_inline"] = True
        loc = ep.get("location") or ""
        if loc:
            doc["author_location"] = loc
        # Surface structured company-level fields (APIDirect + Crustdata)
        # onto the candidate doc so the LLM ICP gate and UI can read them
        # without re-fetching the author cache. All optional — graceful
        # absence when company enrichment failed or wasn't attempted.
        for src_key, dst_key in (
            ("company", "author_company"),
            ("employer_description", "author_company_about"),
            ("company_industry", "author_company_industry"),
            ("company_description", "author_company_description"),
            ("company_employee_range", "author_company_employee_range"),
            ("company_employees", "author_company_employees"),
            ("company_founded_year", "author_company_founded_year"),
            ("company_specialities", "author_company_specialities"),
        ):
            v = ep.get(src_key)
            if v is not None and v != "" and v != []:
                doc[dst_key] = v
    if getattr(post, "reaction_counter", None) is not None:
        doc["reaction_counter"] = int(post.reaction_counter or 0)
    if getattr(post, "comment_counter", None) is not None:
        doc["comment_counter"] = int(post.comment_counter or 0)
    if getattr(post, "repost_counter", None) is not None:
        doc["repost_counter"] = int(post.repost_counter or 0)
    if getattr(post, "is_repost", None) is not None:
        doc["is_repost"] = bool(post.is_repost)
    if getattr(post, "can_post_comments", None) is not None:
        doc["can_post_comments"] = bool(post.can_post_comments)
    if unipile_rubric:
        doc["unipile_rubric"] = unipile_rubric
    # `inline_icp_qualified` is set ONLY by source-verified paths (RULE 24
    # people-search with server-side LOCATION + INDUSTRY + degree filter,
    # plus contact_seed direct lookups). Keyword-search candidates — even
    # those that cleared Path A on the substring rubric — DO NOT get this
    # flag: the substring rubric is a recall-keeper for routing to gates,
    # not a proof of ICP. Keyword candidates must still pass the LLM
    # `non_buyer` and `icp_scoring` gates so we can verify "is this actually
    # a buyer, and does the author's title/industry/stage really match the
    # rubric?" without relying on substring evidence alone. See
    # `_run_unipile_title_search` for the only path that sets this flag.
    return doc


# ----------------------------------------------------------------------
# Inline Unipile author enrichment + per-operator rubric scoring.
#
# Ported from the reference `unipile_hybrid_sweep.py` / `client2` scripts.
# Both scripts found that scoring posts INLINE during discovery — using the
# author's enriched profile (free `/users/{slug}` call) + the operator's own
# ICP fields — dramatically reduced junk that reached the LLM gates.
#
# Two scoring vectors:
#   - Author: title pts (5 if any operator.target_title appears in headline)
#             + industry pts (3 if any operator.target_industry appears)
#             + geo pts     (2 if any operator.target_geography appears)
#   - Post:   keyword-tier hits — tier_1 = 3 pts, tier_2 = 2 pts,
#             tier_3 = 1 pt, capped at 12
#
# Dual-path qualification: either author rubric ≥ THRESHOLD_A, or post
# relevance ≥ THRESHOLD_B. Geo always required when the operator has
# target_geographies set (otherwise skip the geo check so engines without a
# defined geography still produce candidates).
# ----------------------------------------------------------------------

# Short geo tokens (e.g., "us", "uk") are word-boundary matched to avoid the
# "Indianapolis" / "Boston, MA" style false positives that substring matching
# would otherwise hit ("in" appearing inside "Indianapolis").
_RUBRIC_SHORT_TOKEN_MAX = 4


def _rubric_substring_match(haystack: str, needle: str) -> bool:
    """Case-insensitive match. Word-boundary protected for short needles
    (≤ 4 chars) to avoid e.g. "us" inside "Houston" matching."""
    if not haystack or not needle:
        return False
    if len(needle) <= _RUBRIC_SHORT_TOKEN_MAX:
        return bool(re.search(r"\b" + re.escape(needle) + r"\b", haystack))
    return needle in haystack


def _score_unipile_author_against_operator(
    profile: dict[str, Any], operator: dict[str, Any]
) -> dict[str, int]:
    """Score an enriched author profile against the operator's ICP fields.

    Returns a dict with `title`, `industry`, `geo`, and `total`. Missing
    operator fields collapse to a zero in that axis (so e.g. operators with
    no `target_titles` configured will see title=0 for every author — but
    Path B can still qualify them via post relevance)."""
    ext = operator.get("product_extracted") or {}
    titles = [t.lower() for t in (ext.get("target_titles") or []) if isinstance(t, str) and t]
    industries = [i.lower() for i in (ext.get("target_industries") or []) if isinstance(i, str) and i]
    geos = [g.lower() for g in (ext.get("target_geographies") or []) if isinstance(g, str) and g]

    headline = (profile.get("headline") or "").lower()
    location = (profile.get("location") or "").lower()
    company = (profile.get("company") or "").lower()
    # Industry scoring blob — combines every field that can contain industry
    # keywords:
    #   - headline + company name (weak signal, often misses)
    #   - employer_description: Crustdata "company about" blurb
    #   - company_industry: APIDirect /v1/linkedin/company structured field
    #     (e.g., "Hospitals and Health Care") — strongest signal when present
    #   - company_description: APIDirect company description (richer than
    #     Crustdata's employer_description on average)
    employer_desc = (profile.get("employer_description") or "").lower()
    company_industry = (profile.get("company_industry") or "").lower()
    company_description = (profile.get("company_description") or "").lower()
    blob_parts = [headline, company, employer_desc, company_industry, company_description]
    blob = " | ".join(p for p in blob_parts if p).strip(" |")

    title_pts = 5 if any(_rubric_substring_match(headline, t) for t in titles) else 0
    industry_pts = 3 if any(_rubric_substring_match(blob, i) for i in industries) else 0
    # Geo: when operator targets US, use the offline geonamescache resolver
    # (handles 'Atlanta Metropolitan Area', 'Greater Cincinnati', etc. that
    # the 87-term substring rubric misses). Otherwise fall back to substring
    # match against the operator's literal target_geographies entries.
    if _operator_targets_us(operator):
        geo_pts = 2 if is_us_location(location) is True else 0
    else:
        geo_pts = 2 if any(_rubric_substring_match(location, g) for g in geos) else 0

    return {
        "title": title_pts,
        "industry": industry_pts,
        # `geo` is surfaced for the geo-required gate (read separately in
        # `_qualifies_inline_rubric`) but NOT added to `total` — the inline
        # ICP score is title + industry only. Geo qualifies/disqualifies
        # binarily; it doesn't earn points.
        "geo": geo_pts,
        "total": title_pts + industry_pts,
    }


def _score_unipile_post_against_operator(
    post_text: str, operator: dict[str, Any]
) -> tuple[int, list[str]]:
    """Score a post body against the operator's tier_1/tier_2/tier_3
    keyword pool. Returns (score, matched_keywords). Score is capped at 12
    so a single keyword-stuffed post doesn't dominate the ranking."""
    text = (post_text or "").lower()
    if not text:
        return 0, []
    ext = operator.get("product_extracted") or {}
    kws = ext.get("suggested_keywords") or {}
    t1 = [k.lower() for k in (kws.get("tier_1") or []) if isinstance(k, str) and k]
    t2 = [k.lower() for k in (kws.get("tier_2") or []) if isinstance(k, str) and k]
    t3 = [k.lower() for k in (kws.get("tier_3") or []) if isinstance(k, str) and k]

    score = 0
    matched: list[str] = []
    for k in t1:
        if k in text:
            score += 3
            matched.append(k)
    for k in t2:
        if k in text:
            score += 2
            matched.append(k)
    for k in t3:
        if k in text:
            score += 1
            matched.append(k)
    return min(score, 12), matched


def _qualifies_inline_rubric(
    *,
    author_score: dict[str, int],
    post_relevance: int,
    operator: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Dual-path qualification with conditional geo gate.

    Path A: ``author_score.total >= path_a_threshold`` (default 6 — e.g.
        title 5 + geo 2 = 7, OR title 5 + industry 3 = 8, OR industry 3 +
        geo 2 = 5 → falls one short, won't pass alone).
    Path B: ``post_relevance >= path_b_threshold`` (default 3 — one
        tier-1 hit, or two tier-2 hits).

    Geo is required IFF the operator has ``target_geographies`` configured.
    Operators that left geography blank don't filter on location (we'd be
    second-guessing them otherwise)."""
    ext = operator.get("product_extracted") or {}
    has_geo_target = bool(ext.get("target_geographies"))
    require_geo = settings.discovery_unipile_inline_require_geo and has_geo_target
    if require_geo and author_score.get("geo", 0) < 1:
        return False, []

    paths: list[str] = []
    if author_score.get("total", 0) >= settings.discovery_unipile_inline_path_a_threshold:
        paths.append("A_author")
    if post_relevance >= settings.discovery_unipile_inline_path_b_threshold:
        paths.append("B_post")
    return bool(paths), paths


_LINKEDIN_POST_AUTHOR_SLUG_RE = re.compile(
    r"linkedin\.com/posts/([^_/?#]+)_", re.IGNORECASE
)


# Tracking-param names we strip when canonicalizing post URLs. Keep this list
# scoped to known LinkedIn / vendor instrumentation so we don't drop anything
# meaningful (e.g. ``id`` on /pulse/ URLs would be load-bearing).
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "rcm",            # LinkedIn recommendation-context (per-viewer account id)
    "refid",          # LinkedIn referral id
    "trackingId",     # LinkedIn search-result tracking
    "trk", "trkInfo", # legacy LinkedIn click-tracking
    "midToken", "midSig", "ts",  # legacy LinkedIn email-share tracking
    "fbclid", "gclid", "mc_cid", "mc_eid",
})


class _CanonicalUrlSet:
    """Set-like wrapper that canonicalizes URLs on every ``add`` and ``in``
    check. Lets every legacy ``post.url in seen_urls`` / ``seen_urls.add(...)``
    callsite in this module stay as-is while still deduping against tracking-
    param-stripped canonical URLs. Implements the subset of ``set`` the
    engine actually uses (``in``, ``add``, ``__len__``, ``__iter__``)."""

    __slots__ = ("_set",)

    def __init__(self, initial=()) -> None:
        self._set: set[str] = set()
        for u in initial:
            self.add(u)

    def add(self, url: str | None) -> None:
        if not url:
            return
        self._set.add(_canonical_post_url(url))

    def __contains__(self, url: object) -> bool:
        if not isinstance(url, str) or not url:
            return False
        return _canonical_post_url(url) in self._set

    def __len__(self) -> int:
        return len(self._set)

    def __iter__(self):
        return iter(self._set)


def _canonical_post_url(url: str | None) -> str:
    """Strip per-viewer tracking params so the same LinkedIn post resolves
    to one canonical URL across vendors (Unipile vs Exa vs APIDirect), across
    cofounders (different LinkedIn accounts produce different ``rcm`` values),
    and across runs. This is the dedup key for ``seen_urls`` / 90-day
    exhaustion ledger — without canonicalization the same post sneaks past
    dedup whenever the tracking shape changes."""
    if not url:
        return url or ""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    try:
        parts = urlsplit(url.strip())
    except Exception:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k not in _TRACKING_PARAMS
    ]
    new_query = urlencode(kept, doseq=True)
    # Drop fragment too (LinkedIn ignores it).
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, new_query, ""))


# Heuristic markers that a slug or "name" is actually a brand/company rather
# than a person. Single-token lowercase identifiers that look like a brand
# (no first/last name shape, no spaces) are downgraded — Unipile's profile
# lookup for slugs like ``innovaccer`` or ``salesforce`` either 404s or
# returns a company shape, and we end up with no usable human author.
_COMPANY_SHAPED_SLUG_RE = re.compile(
    r"^(?!.* )[a-z0-9][a-z0-9-]{2,}$"  # one lowercase token, optional dashes
)


def _looks_like_company_slug(slug: str | None) -> bool:
    if not slug:
        return False
    s = slug.strip().lower()
    if not _COMPANY_SHAPED_SLUG_RE.match(s):
        return False
    # Person slugs typically include either a first+last with a separating
    # dash (e.g. ``anand-mehta-b9106a94``) or end in a numeric LinkedIn ID
    # hash. A single non-dashed token is almost always a brand.
    if "-" not in s:
        return True
    return False


def _is_company_authored(
    raw_author: str | None,
    enriched_profile: dict[str, Any] | None,
    post_url: str | None,
) -> bool:
    """Return True only when we can POSITIVELY identify the post as
    company-authored (brand page, organization profile, etc.) rather than
    a human author. Default is False — when uncertain, keep the candidate
    and let post-relevance + LLM ICP gates make the call.

    Positive company signals (any one is enough):
      1. The post URL itself is on a ``/company/`` path.
      2. Enriched profile carries ``is_company=True`` or a company-typed
         ``type`` / ``profile_type`` value.
      3. URL author slug is brand-shaped (single lowercase token, no dashes
         — e.g. ``/posts/innovaccer_…``) AND we have no other human-author
         signal (no vendor-inline author string, no enriched name).
    """
    # Signal 1: company page post path.
    if post_url and "/company/" in post_url.lower():
        return True
    # Signal 2: explicit is_company / type from enrichment.
    if enriched_profile:
        if enriched_profile.get("is_company") is True:
            return True
        ptype = str(
            enriched_profile.get("type")
            or enriched_profile.get("profile_type")
            or ""
        ).upper()
        if "COMPANY" in ptype and "PERSON" not in ptype:
            return True
        if "ORGANIZATION" in ptype:
            return True
    # Signal 3: brand-shaped slug AND no human-author signal elsewhere.
    slug = _extract_author_slug_from_post_url(post_url)
    if slug and _looks_like_company_slug(slug):
        has_human_signal = bool(
            (raw_author and raw_author.strip())
            or (enriched_profile and (enriched_profile.get("name") or "").strip())
        )
        if not has_human_signal:
            return True
    return False


def _is_exa_non_individual_author(
    raw_author: str | None,
    enriched_profile: dict[str, Any] | None,
    post_url: str | None,
) -> tuple[bool, str | None]:
    """STRICTER variant for Exa results. Returns ``(drop, reason)``.

    Exa's neural search surfaces a lot of non-individual content — brand-
    page posts, /pulse/ articles, /newsletters/, posts where the author
    slug is brand-shaped but Exa fills in a generic display name. The
    default ``_is_company_authored`` is permissive (drop only on positive
    proof); for Exa we add stricter signals on top:

      - `_is_company_authored` early-return (existing brand-page logic).
      - URL is ``/pulse/`` (LinkedIn long-form article — often org-authored).
      - URL is ``/newsletters/`` (LinkedIn newsletter — usually org).
      - We have NO author identity at all (no raw_author, no enriched
        name, no slug we can extract) → can't verify human → drop, the
        LLM ICP gate has nothing to score against.
    """
    if _is_company_authored(raw_author, enriched_profile, post_url):
        return True, "company_authored"
    url_l = (post_url or "").lower()
    if "/pulse/" in url_l:
        return True, "linkedin_pulse_article"
    if "/newsletters/" in url_l or "/newsletter/" in url_l:
        return True, "linkedin_newsletter"
    slug = _extract_author_slug_from_post_url(post_url)
    has_any_signal = bool(
        (raw_author and raw_author.strip())
        or (enriched_profile and (enriched_profile.get("name") or "").strip())
        or slug
    )
    if not has_any_signal:
        return True, "no_author_signal"
    return False, None


def _extract_author_slug_from_post_url(url: str | None) -> str | None:
    """Pull the author's LinkedIn public-identifier slug out of a post URL.

    LinkedIn post URLs follow the pattern
    ``https://www.linkedin.com/posts/<author-slug>_<post-content>-activity-<id>-<rcm>``
    so the slug is everything between ``/posts/`` and the first underscore.
    Returns None if the URL doesn't match the pattern (e.g. share links of
    the form ``/posts/activity-<id>-...`` which have no author slug).
    """
    if not url:
        return None
    m = _LINKEDIN_POST_AUTHOR_SLUG_RE.search(url)
    if not m:
        return None
    slug = m.group(1).strip()
    if not slug or slug.lower() == "activity":
        return None
    return slug


def _get_cached_unipile_author_profile_by_slug(
    db: Database,
    *,
    operator_id: ObjectId,
    slug: str,
    account_id: str,
    ttl_days: int,
    slate_run_id: ObjectId | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Slug-keyed variant for vendors that give us a LinkedIn post URL but
    no provider_id (APIDirect, Exa).

    Same Mongo collection (``unipile_author_cache``) but a different lookup
    key (``public_identifier``) so we share cache hits with the provider_id
    path when both paths see the same author.
    """
    from app.services.unipile import (
        resolve_profile as unipile_resolve_profile,
        UnipileError,
        UnipileNotConfigured,
    )

    if not slug:
        return None, False
    now = utcnow()
    cutoff = now - timedelta(days=max(1, ttl_days))
    cached = db.unipile_author_cache.find_one(
        {
            "operator_id": operator_id,
            "public_identifier": slug,
            "fetched_at": {"$gte": cutoff},
        }
    )
    if cached:
        return cached, False

    if slate_run_id is not None and not _try_consume_profile_fetch_budget(
        db, slate_run_id
    ):
        return None, False

    try:
        raw = unipile_resolve_profile(account_id=account_id, public_identifier_or_url=slug)
    except (UnipileError, UnipileNotConfigured) as err:
        log.debug("unipile: profile fetch failed for slug=%s: %s", slug[:40], err)
        return None, True
    if not raw:
        return None, True

    work = raw.get("work_experience") or []
    first_job = work[0] if isinstance(work, list) and work else {}
    provider_id = raw.get("provider_id") or raw.get("id") or ""
    name = (
        raw.get("name")
        or " ".join(filter(None, [raw.get("first_name"), raw.get("last_name")])).strip()
        or None
    )
    doc = {
        "operator_id": operator_id,
        "provider_id": provider_id,
        "public_identifier": raw.get("public_identifier") or slug,
        "name": name or "",
        "headline": raw.get("headline") or "",
        "location": raw.get("location") or "",
        "company": (first_job.get("company") or first_job.get("company_name") or ""),
        "title": (first_job.get("title") or first_job.get("role") or ""),
        "source": "unipile",
        "fetched_at": now,
    }
    db.unipile_author_cache.update_one(
        {"operator_id": operator_id, "public_identifier": slug},
        {"$set": doc},
        upsert=True,
    )
    return doc, True


def _get_cached_unipile_author_profile(
    db: Database,
    *,
    operator_id: ObjectId,
    provider_id: str,
    account_id: str,
    ttl_days: int,
    slate_run_id: ObjectId | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(profile, was_fetched)`` for the given provider_id, using
    ``unipile_author_cache`` keyed by ``(operator_id, provider_id)``.

    `was_fetched` is True when this call hit Unipile's `/users/{slug}` —
    callers use it to track API-call budget per discovery pass.

    Cache TTL is a soft TTL (we check ``fetched_at`` against the cutoff in
    the query rather than relying on a Mongo TTL index, so refreshes happen
    on first miss rather than on a background sweep)."""
    from app.services.unipile import (
        resolve_profile as unipile_resolve_profile,
        UnipileError,
        UnipileNotConfigured,
    )

    if not provider_id:
        return None, False
    now = utcnow()
    cutoff = now - timedelta(days=max(1, ttl_days))
    cached = db.unipile_author_cache.find_one(
        {
            "operator_id": operator_id,
            "provider_id": provider_id,
            "fetched_at": {"$gte": cutoff},
        }
    )
    if cached:
        return cached, False

    if slate_run_id is not None and not _try_consume_profile_fetch_budget(
        db, slate_run_id
    ):
        return None, False

    try:
        raw = unipile_resolve_profile(
            account_id=account_id, public_identifier_or_url=provider_id
        )
    except (UnipileError, UnipileNotConfigured) as err:
        log.debug(
            "unipile: profile fetch failed for provider_id=%s: %s",
            provider_id[:40], err,
        )
        return None, True  # API was attempted, just failed
    if not raw:
        return None, True

    work = raw.get("work_experience") or []
    first_job = work[0] if isinstance(work, list) and work else {}
    name = (
        raw.get("name")
        or " ".join(
            filter(None, [raw.get("first_name"), raw.get("last_name")])
        ).strip()
        or None
    )
    doc = {
        "operator_id": operator_id,
        "provider_id": provider_id,
        "public_identifier": raw.get("public_identifier") or "",
        "name": name or "",
        "headline": raw.get("headline") or "",
        "location": raw.get("location") or "",
        "company": (first_job.get("company") or first_job.get("company_name") or ""),
        "title": (first_job.get("title") or first_job.get("role") or ""),
        "source": "unipile",
        "fetched_at": now,
    }
    db.unipile_author_cache.update_one(
        {"operator_id": operator_id, "provider_id": provider_id},
        {"$set": doc},
        upsert=True,
    )
    return doc, True


def _lookup_unipile_author_cache(
    db: Database,
    *,
    operator_id: ObjectId,
    provider_id: str | None,
    public_identifier: str | None,
    ttl_days: int,
) -> dict[str, Any] | None:
    """Read-only cache probe used by the Crustdata-first enrichment flow.

    Tries the (operator_id, provider_id) key first, then the
    (operator_id, public_identifier) key. Returns the cached doc if its
    ``fetched_at`` is within ``ttl_days`` of now, else None.

    No vendor calls. No writes. No budget consumption."""
    if not (provider_id or public_identifier):
        return None
    now = utcnow()
    cutoff = now - timedelta(days=max(1, ttl_days))
    if provider_id:
        hit = db.unipile_author_cache.find_one(
            {
                "operator_id": operator_id,
                "provider_id": provider_id,
                "fetched_at": {"$gte": cutoff},
            }
        )
        if hit:
            return hit
    if public_identifier:
        hit = db.unipile_author_cache.find_one(
            {
                "operator_id": operator_id,
                "public_identifier": public_identifier,
                "fetched_at": {"$gte": cutoff},
            }
        )
        if hit:
            return hit
    return None


def _public_identifier_from_url(url: str | None) -> str | None:
    """Extract the LinkedIn slug from a ``linkedin.com/in/<slug>`` URL."""
    if not url or "/in/" not in url:
        return None
    slug = url.rsplit("/in/", 1)[-1].rstrip("/")
    slug = slug.split("?", 1)[0].split("#", 1)[0]
    return slug or None


# LinkedIn provider URNs have a distinctive shape: `ACo...`, `ACw...`, etc.
# (uppercase "AC" prefix, then 20+ chars of base64-ish identifier with no
# hyphens). They sometimes leak into URL form as `linkedin.com/in/ACoAA...`
# which `is_likely_person_slug` lets through — but Crustdata's slug index
# is NOT URN-aware and will fuzzy-match these to the wrong person. So we
# detect them locally and exclude before sending to Crustdata.
_LINKEDIN_URN_SLUG_RE = re.compile(r"^AC[A-Za-z0-9_-]{20,}$")


def _looks_like_urn_slug(slug: str | None) -> bool:
    return bool(slug and _LINKEDIN_URN_SLUG_RE.match(slug))


_COMPANY_CACHE_TTL_DAYS = 30


def _get_cached_company_details(
    db: Database,
    *,
    operator_id: ObjectId,
    company_linkedin_id: str,
    company_url: str,
) -> dict[str, Any] | None:
    """Look up or fetch APIDirect company details, cached cross-run in
    ``apidirect_company_cache`` (TTL ~30d).

    Returns a dict with industry / employee_range / description / specialties,
    or ``None`` on any failure. Failure is silent — callers never block on
    company enrichment."""
    if not company_linkedin_id:
        return None
    cache_key = str(company_linkedin_id).strip()
    if not cache_key:
        return None
    coll = getattr(db, "apidirect_company_cache", None)
    if coll is None:
        return None
    cached = coll.find_one({"_id": cache_key})
    cutoff = utcnow() - timedelta(days=_COMPANY_CACHE_TTL_DAYS)
    if cached and cached.get("fetched_at"):
        fetched_at = cached["fetched_at"]
        if getattr(fetched_at, "tzinfo", None) is None:
            from datetime import timezone as _tz
            fetched_at = fetched_at.replace(tzinfo=_tz.utc)
        if fetched_at >= cutoff:
            if cached.get("not_found"):
                return None
            return {
                "industry": cached.get("industry"),
                "description": cached.get("description"),
                "employee_range": cached.get("employee_range"),
                "employees": int(cached.get("employees") or 0),
                "founded_year": cached.get("founded_year"),
                "specialities": cached.get("specialities") or [],
                "name": cached.get("name"),
                "website": cached.get("website"),
            }
    try:
        from app.services.apidirect import (
            get_linkedin_company_details,
            ApiDirectQuotaExhausted,
            ApiDirectNotConfigured,
        )
        details = get_linkedin_company_details(company_url)
    except ApiDirectQuotaExhausted:
        coll.update_one(
            {"_id": cache_key},
            {"$set": {"_id": cache_key, "not_found": True, "fetched_at": utcnow()}},
            upsert=True,
        )
        return None
    except (ApiDirectNotConfigured, Exception) as err:
        log.warning(
            "│  [apidirect-company] fetch failed key=%s err=%s",
            cache_key[:60], err,
        )
        return None
    if details is None:
        coll.update_one(
            {"_id": cache_key},
            {"$set": {"_id": cache_key, "not_found": True, "fetched_at": utcnow()}},
            upsert=True,
        )
        return None
    doc = {
        "_id": cache_key,
        "name": details.name,
        "company_id": details.company_id,
        "industry": details.industry,
        "description": details.description,
        "website": details.website,
        "followers": details.followers,
        "employees": details.employees,
        "employee_range": details.employee_range,
        "founded_year": details.founded_year,
        "specialities": details.specialities,
        "headquarters": details.headquarters,
        "fetched_at": utcnow(),
        "not_found": False,
    }
    coll.update_one({"_id": cache_key}, {"$set": doc}, upsert=True)
    return {
        "industry": details.industry,
        "description": details.description,
        "employee_range": details.employee_range,
        "employees": details.employees,
        "founded_year": details.founded_year,
        "specialities": details.specialities,
        "name": details.name,
        "website": details.website,
    }


def _enrich_via_crustdata(
    db: Database,
    *,
    operator_id: ObjectId,
    posts_pending: list[Any],
) -> dict[str, dict[str, Any]]:
    """Batch-enrich the unique ``author_profile_url``s on ``posts_pending``
    via Crustdata Person Enrich, writing successful matches into
    ``unipile_author_cache`` keyed by (operator_id, public_identifier) with
    ``source="crustdata"``.

    Returns ``{author_profile_url: profile_dict}`` for the matched subset.
    Posts whose URL fails ``is_likely_person_slug`` (URN form, brand
    handles, malformed) are silently absent from the output — caller routes
    those to Unipile fallback.

    Crustdata circuit trip (401/402/429) raises out of ``enrich_profiles``
    and is caught here — on circuit-open we return an empty dict so the
    caller proceeds to Unipile fallback for the entire batch."""
    from app.services.crustdata_enrich import (
        enrich_profiles as crustdata_enrich_profiles,
        is_likely_person_slug,
        CrustdataEnrichError,
    )

    # De-duplicate the candidate URL set so a popular author who appears in
    # multiple posts is only enriched once per query. Author URL is sourced
    # in priority order:
    #   1. `post.author_profile_url`   (Unipile keyword post payload sets this)
    #   2. derived: `linkedin.com/in/<slug>` from post URL    (APIDirect / Exa)
    # The derived form lets APIDirect and Exa results flow through the same
    # Crustdata-first → Unipile-fallback chain as Unipile keyword.
    unique_urls: dict[str, str] = {}  # url -> slug
    skipped_urn = 0
    for post in posts_pending:
        url = (getattr(post, "author_profile_url", "") or "").strip()
        if not url:
            post_url = (getattr(post, "url", "") or "").strip()
            derived_slug = _extract_author_slug_from_post_url(post_url)
            if derived_slug:
                url = f"https://www.linkedin.com/in/{derived_slug}"
        if not url or url in unique_urls:
            continue
        if not is_likely_person_slug(url):
            continue
        slug = _public_identifier_from_url(url) or ""
        if not slug:
            continue
        if _looks_like_urn_slug(slug):
            # Crustdata's slug index isn't URN-aware — it has been observed
            # to fuzzy-match URN-form slugs to the wrong person. Skip and
            # let the Unipile fallback path handle these (Unipile's
            # /users/{provider_id} endpoint resolves URNs natively).
            skipped_urn += 1
            continue
        unique_urls[url] = slug
    if skipped_urn:
        log.info(
            "│  [crustdata-enrich] skipped %d URN-form slug(s) — routing to Unipile fallback",
            skipped_urn,
        )

    if not unique_urls:
        return {}

    # Crustdata's vendor cap is 25; we respect the configured batch_size as
    # an upper bound but enrich_profiles already batches internally so a
    # single call is fine.
    batch_size = min(
        max(1, settings.discovery_unipile_crustdata_batch_size),
        25,
    )
    out: dict[str, dict[str, Any]] = {}
    urls = list(unique_urls.keys())
    now = utcnow()
    for start in range(0, len(urls), batch_size):
        batch = urls[start : start + batch_size]
        try:
            enriched = crustdata_enrich_profiles(batch)
        except CrustdataEnrichError as err:
            log.warning(
                "│  [crustdata-enrich] batch of %d failed (circuit may be open): %s",
                len(batch),
                err,
            )
            return out  # Stop trying — caller falls back to Unipile.

        for url in batch:
            profile = enriched.get(url)
            if profile is None:
                continue
            slug = unique_urls[url]
            # Structured company-level enrichment via APIDirect — gated by
            # config so operators can disable for cost. Failure is silent;
            # the rubric and LLM ICP scoring proceed with what they have.
            company_details: dict[str, Any] | None = None
            if (
                settings.apidirect_fetch_company_details
                and profile.employer_linkedin_id
            ):
                company_url = (
                    f"https://www.linkedin.com/company/{profile.employer_linkedin_id}"
                )
                company_details = _get_cached_company_details(
                    db,
                    operator_id=operator_id,
                    company_linkedin_id=profile.employer_linkedin_id,
                    company_url=company_url,
                )
            doc = {
                "operator_id": operator_id,
                # Crustdata doesn't return a Unipile URN — leave provider_id
                # null so the (operator_id, public_identifier) index serves
                # cache lookups for these entries.
                "provider_id": None,
                "public_identifier": slug,
                "name": profile.name or "",
                "headline": profile.headline or "",
                "location": profile.location or "",
                "company": profile.employer_name or "",
                "title": profile.title or "",
                # Crustdata-only structured fields (free signal — used by the
                # inline industry rubric below to substring-match the company's
                # about blurb instead of the bare company name).
                "employer_description": profile.employer_description or "",
                "employer_linkedin_id": profile.employer_linkedin_id or "",
                # APIDirect /v1/linkedin/company — structured industry / size /
                # description. Persisted on the cache (and forward-copied to
                # the candidate doc) so the LLM ICP gate sees structured data.
                "company_industry": (company_details or {}).get("industry"),
                "company_description": (company_details or {}).get("description"),
                "company_employee_range": (company_details or {}).get("employee_range"),
                "company_employees": (company_details or {}).get("employees"),
                "company_founded_year": (company_details or {}).get("founded_year"),
                "company_specialities": (company_details or {}).get("specialities") or [],
                "source": "crustdata",
                "fetched_at": now,
            }
            db.unipile_author_cache.update_one(
                {"operator_id": operator_id, "public_identifier": slug},
                {"$set": doc},
                upsert=True,
            )
            out[url] = doc

    log.info(
        "│  [crustdata-enrich] requested=%d matched=%d (3 credits/match)",
        len(unique_urls),
        len(out),
    )
    return out


def _apidirect_optional_details(post_url: str) -> LinkedInPostDetails | None:
    """GET /v1/linkedin/post when enabled; quota exhaustion propagates."""
    if not settings.discovery_apidirect_fetch_post_details:
        return None
    try:
        return get_linkedin_post_details(post_url)
    except ApiDirectQuotaExhausted:
        raise
    except ApiDirectError as err:
        log.warning(
            "discovery: apidirect post details failed url=%s err=%s",
            post_url[:120],
            err,
        )
        return None


def _doc_from_apidirect(
    post: ApiDirectPost,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source_keyword: str,
    source_classification: str,
    source_channel: str = "keyword_topical",
    details: LinkedInPostDetails | None = None,
    enriched_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    author_name = post.author
    author_linkedin_url = None
    author_title: str | None = None
    author_company: str | None = None
    author_location: str | None = None
    post_text = post.snippet or post.title or ""
    post_published_at = post.published_at
    post_id: str | None = None
    if details is not None:
        if details.author:
            author_name = details.author
        if details.author_url:
            author_linkedin_url = details.author_url
        if details.text.strip():
            post_text = details.text.strip()
        if details.published_at is not None:
            post_published_at = details.published_at
        if details.urn:
            post_id = details.urn
    # Unipile-enriched profile (via slug extracted from post URL) wins on
    # fields the search payload doesn't carry.
    if enriched_profile:
        if enriched_profile.get("name"):
            author_name = enriched_profile["name"]
        if enriched_profile.get("headline"):
            author_title = enriched_profile["headline"]
        if enriched_profile.get("company"):
            author_company = enriched_profile["company"]
        if enriched_profile.get("location"):
            author_location = enriched_profile["location"]
    doc = _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=post_id,
        author_name=author_name,
        author_title=author_title,
        author_company=author_company,
        author_linkedin_url=author_linkedin_url,
        post_text=post_text,
        post_published_at=post_published_at,
        source="apidirect_kw",
        source_keyword=source_keyword,
        source_classification=source_classification,
        source_channel=source_channel,
    )
    if author_location:
        doc["author_location"] = author_location
        doc["enriched_inline"] = True
    return doc


def _doc_from_exa(
    post: ExaPost,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source_keyword: str,
    source_classification: str,
    source_channel: str = "keyword_topical",
    enriched_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Pre-derive author URL from the LinkedIn post slug when the URL shape
    # allows it. /pulse/ and /feed/update/ URLs return None.
    from app.engine.stages.verification import _author_url_from_post_url

    author_name = post.author
    author_title: str | None = None
    author_company: str | None = None
    author_location: str | None = None
    if enriched_profile:
        if enriched_profile.get("name"):
            author_name = enriched_profile["name"]
        if enriched_profile.get("headline"):
            author_title = enriched_profile["headline"]
        if enriched_profile.get("company"):
            author_company = enriched_profile["company"]
        if enriched_profile.get("location"):
            author_location = enriched_profile["location"]

    doc = _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=None,
        author_name=author_name,
        author_title=author_title,
        author_company=author_company,
        author_linkedin_url=_author_url_from_post_url(post.url),
        post_text=post.snippet or post.title or "",
        post_published_at=post.published_at,
        source="exa_kw",
        source_keyword=source_keyword,
        source_classification=source_classification,
        source_channel=source_channel,
    )
    if author_location:
        doc["author_location"] = author_location
        doc["enriched_inline"] = True
    return doc


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


def _screener_post_published_at(raw: dict[str, Any]) -> Any:
    """Normalize Crustdata screener ``date_posted`` (usually ``YYYY-MM-DD``)."""
    val = raw.get("date_posted")
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    s = str(val).strip()[:10]
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
            return datetime(y, m, d, tzinfo=timezone.utc)
        except ValueError:
            return val
    return val


def _doc_from_crustdata_screener(
    raw: dict[str, Any],
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source_keyword: str,
    source_classification: str,
) -> dict[str, Any]:
    post_url = (raw.get("share_url") or "").strip()
    post_id = raw.get("uid") or raw.get("backend_urn")
    if isinstance(post_id, str):
        post_id = post_id.strip() or None
    author_name = raw.get("actor_name")
    author_title = None
    author_company = None
    pd = raw.get("person_details")
    if isinstance(pd, dict):
        emps = pd.get("current_employers") or []
        if emps and isinstance(emps[0], dict):
            author_company = emps[0].get("employer_name")
            author_title = emps[0].get("employee_title")
        if not author_title:
            author_title = pd.get("person_title")
    author_linkedin_url = author_profile_url_from_screener_post(raw)
    post_text = (raw.get("text") or "")[:20000]
    return _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post_url,
        post_id=str(post_id) if post_id is not None else None,
        author_name=author_name if isinstance(author_name, str) else None,
        author_title=author_title if isinstance(author_title, str) else None,
        author_company=author_company if isinstance(author_company, str) else None,
        author_linkedin_url=author_linkedin_url,
        post_text=post_text,
        post_published_at=_screener_post_published_at(raw),
        source="crustdata_screener",
        source_keyword=source_keyword,
        source_classification=source_classification,
        source_channel="keyword_topical",
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
    scanned = 0
    skipped_dup = 0
    skipped_author = 0
    skipped_exhausted = 0
    t_inbox = time.monotonic()
    for row in cursor:
        if _discovery_wall_clock_exceeded(
            t_inbox, settings.discovery_wall_clock_cap_seconds_crustdata_inbox
        ):
            log.info(
                "discovery: crustdata_inbox wall-clock cap (%ds) — stopping drain",
                settings.discovery_wall_clock_cap_seconds_crustdata_inbox,
            )
            break
        scanned += 1
        post_url = row.get("post_url") or ""
        if not post_url or post_url in seen_urls:
            skipped_dup += 1
            db.crustdata_inbox.update_one(
                {"_id": row["_id"]},
                {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "duplicate_url"}},
            )
            continue
        author_url = row.get("author_linkedin_url") or ""
        if author_url and author_url in seen_authors_shipped:
            skipped_author += 1
            db.crustdata_inbox.update_one(
                {"_id": row["_id"]},
                {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "author_shipped"}},
            )
            continue
        if _is_exhausted(db, operator_id, author_url or post_url):
            skipped_exhausted += 1
            db.crustdata_inbox.update_one(
                {"_id": row["_id"]},
                {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "exhausted"}},
            )
            continue
        seen_urls.add(post_url)
        inbox_doc = _doc_from_inbox(
            row,
            operator_id=operator_id,
            cofounder_id=cofounder_id,
            slate_run_id=slate_run_id,
        )
        db.candidates.insert_one(inbox_doc)
        _discovery_record_insert(db, inbox_doc)
        db.crustdata_inbox.update_one(
            {"_id": row["_id"]},
            {"$set": {"consumed": True, "consumed_at": utcnow(), "consumed_reason": "inserted"}},
        )
        inserted += 1
    log.info(
        "discovery: crustdata_inbox drain cofounder=%s inserted=%d scanned=%d "
        "(skip dup_url=%d author_shipped=%d exhausted=%d) lookback_h=%d",
        cofounder_id,
        inserted,
        scanned,
        skipped_dup,
        skipped_author,
        skipped_exhausted,
        settings.crustdata_inbox_lookback_hours,
    )
    return inserted


def _run_crustdata_screener(
    db: Database,
    *,
    operator: dict[str, Any],
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    tier_1: list[str],
    tier_2: list[str],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
) -> int:
    """Crustdata realtime keyword screener — posts returned in the HTTP body."""
    if not settings.discovery_use_crustdata_screener:
        return 0
    if not settings.crustdata_api_key:
        return 0
    plan: list[tuple[str, str]] = []
    for kw in _rotate(tier_1, DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(tier_2, DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))
    max_calls = max(0, int(settings.discovery_crustdata_screener_max_keyword_calls))
    plan = plan[:max_calls]
    if not plan:
        log.info(
            "discovery: crustdata_screener skipped cofounder=%s (empty keyword plan)",
            cofounder_id,
        )
        return 0
    limit_kw = max(1, min(int(settings.discovery_crustdata_screener_limit_per_keyword), 50))
    date_posted = settings.discovery_crustdata_screener_date_posted.strip() or "past-month"
    inserted = 0
    t_scr = time.monotonic()
    for query, classification in plan:
        if _discovery_wall_clock_exceeded(
            t_scr, settings.discovery_wall_clock_cap_seconds_crustdata_screener
        ):
            log.info(
                "discovery: crustdata_screener wall-clock cap (%ds) — stopping",
                settings.discovery_wall_clock_cap_seconds_crustdata_screener,
            )
            break
        # Crustdata's screener takes a keyword string + a separate AUTHOR_LOCATION
        # filter in the request body. Suffixing geo/seniority into the keyword
        # text duplicates the geo filter and collapses recall on Crustdata's
        # literal-match index — same failure mode as Unipile keyword search.
        vendor_kw = _compose_discovery_query(
            query, operator, with_geo=False, with_seniority=False
        )[:400].strip()
        if not vendor_kw:
            continue
        try:
            posts = screener_keyword_search_posts(
                keyword=vendor_kw,
                limit=limit_kw,
                date_posted=date_posted,
            )
        except CrustdataNotConfigured:
            log.info(
                "discovery: crustdata_screener halted cofounder=%s (not configured)",
                cofounder_id,
            )
            return inserted
        except CrustdataQuotaExhausted as err:
            log.warning("discovery: crustdata_screener halted mid-run: %s", err)
            return inserted
        except CrustdataError as err:
            log.warning(
                "discovery: crustdata_screener keyword=%r failed: %s",
                query[:80],
                err,
            )
            continue
        for raw in posts:
            post_url = (raw.get("share_url") or "").strip()
            if not post_url or post_url in seen_urls:
                continue
            author_url = author_profile_url_from_screener_post(raw) or ""
            if author_url and author_url in seen_authors_shipped:
                continue
            if _is_exhausted(db, operator_id, author_url or post_url):
                continue
            seen_urls.add(post_url)
            cd_doc = _doc_from_crustdata_screener(
                raw,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                source_keyword=query,
                source_classification=classification,
            )
            db.candidates.insert_one(cd_doc)
            _discovery_record_insert(db, cd_doc)
            inserted += 1
    log.info(
        "discovery: crustdata_screener cofounder=%s inserted=%d keyword_calls=%d",
        cofounder_id,
        inserted,
        len(plan),
    )
    return inserted


def _maybe_crustdata_simulation_ping_after_empty_inbox(
    operator: dict[str, Any],
    cofounder_id: ObjectId,
) -> None:
    """When inbox drain produced nothing, optionally hit Crustdata simulation
    so this stage still performs one outbound HTTP round-trip and their service
    can POST a sample notification to our webhook (same shape as production)."""
    if not settings.discovery_crustdata_simulation_ping_on_empty_inbox:
        return
    if not settings.crustdata_api_key:
        log.info(
            "crustdata.discovery simulation_ping skipped cofounder=%s (no CRUSTDATA_API_KEY)",
            cofounder_id,
        )
        return
    try:
        from fastapi import HTTPException

        from app.routes.crustdata import RegisterWatchRequest, _spec_from_operator
        from app.services.crustdata import (
            CrustdataError,
            CrustdataNotConfigured,
            CrustdataQuotaExhausted,
            register_keyword_watch,
            webhook_url_for,
        )

        spec = _spec_from_operator(operator, RegisterWatchRequest())
        endpoint = webhook_url_for(str(cofounder_id))
    except HTTPException as e:
        log.info(
            "crustdata.discovery simulation_ping skipped cofounder=%s (no watch spec): %s",
            cofounder_id,
            e.detail,
        )
        return
    except Exception as e:
        log.warning(
            "crustdata.discovery simulation_ping skipped cofounder=%s (spec error): %s",
            cofounder_id,
            e,
        )
        return
    try:
        log.info(
            "crustdata.discovery simulation_ping cofounder=%s (empty inbox; POST %s)",
            cofounder_id,
            "simulation/watches",
        )
        register_keyword_watch(
            cofounder_id=str(cofounder_id),
            spec=spec,
            notification_endpoint=endpoint,
            simulation=True,
        )
    except CrustdataNotConfigured as e:
        log.info("crustdata.discovery simulation_ping skipped cofounder=%s: %s", cofounder_id, e)
    except (CrustdataQuotaExhausted, CrustdataError) as e:
        log.warning(
            "crustdata.discovery simulation_ping failed cofounder=%s: %s",
            cofounder_id,
            e,
        )


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
    crustdata_simulation_ping: bool = True,
) -> int:
    """Insert raw candidates per cofounder via Unipile.

    ``crustdata_simulation_ping`` is false on top-up discovery rounds so we do
    not POST Crustdata simulation watches repeatedly in one slate run.
    """
    operator_id: ObjectId = operator["_id"]
    _ensure_slate_profile_fetch_budget(db, slate_run_id)
    extracted = operator.get("product_extracted") or {}
    keywords = (extracted.get("suggested_keywords") or {}) if extracted else {}
    tier_1 = keywords.get("tier_1") or []
    tier_2 = keywords.get("tier_2") or []
    tier_3 = keywords.get("tier_3") or []

    # RULE 15-EXT — title-plus-industry pool. Source priority:
    #   1. configs/<client>/keyword_pools.json:title_industry  (curated per-tenant)
    #   2. operator.product_extracted.title_industry            (back-compat)
    # PLUS: programmatic cross-product expansion (target_titles × generic
    # company-type tokens) appended after the curated entries. The curated
    # list keeps operator-controlled priority queries first; the expander
    # covers the long tail (small-practice CEOs, specialty clinic founders,
    # etc.) that hand-curation misses.
    cfg = client_config.for_operator(operator)
    curated_ti: list[str] = (
        cfg.keyword_pools.get("title_industry")
        or extracted.get("title_industry")
        or []
    )
    expanded_ti = _expand_title_industry_queries(operator)
    seen_ti: set[str] = set()
    title_industry: list[str] = []
    for q in list(curated_ti) + list(expanded_ti):
        if not isinstance(q, str):
            continue
        key = q.strip().lower()
        if not key or key in seen_ti:
            continue
        seen_ti.add(key)
        title_industry.append(q.strip())
    if title_industry:
        log.info(
            "discovery: title_industry pool = %d (curated=%d, expanded=%d)",
            len(title_industry), len(curated_ti), len(expanded_ti),
        )

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
    # Cross-run dedup layers — re-enabled. The DEMO OVERRIDE that initialized
    # these as empty sets caused the same post to be inserted multiple times
    # across top-up rounds within a single slate run (and across slate runs
    # within the 90-day window), producing duplicate drafted comments for
    # the same URL.
    #
    # ``seen_urls`` is a canonical-URL set so every per-loop ``post.url in
    # seen_urls`` / ``seen_urls.add(post.url)`` callsite transparently
    # canonicalizes (strips ``utm_*``, ``rcm`` per-viewer tracking) and
    # dedupes the same post across vendors and cofounders.
    seen_urls = _CanonicalUrlSet(_seen_post_urls(db, operator_id))
    seen_authors_shipped: set[str] = _seen_author_urls(db, operator_id)
    log.info(
        "discovery: pre-seeded dedupe sets — %d seen post URLs, %d shipped authors",
        len(seen_urls),
        len(seen_authors_shipped),
    )

    # Manual contact seeds — searched via Unipile direct (gates bypassed).
    # When the operator has set `active_contact_group`, restrict the walk
    # to that group only. Recognised values:
    #   None / empty       → walk every contact regardless of group
    #   "__ungrouped__"    → walk ONLY contacts with no group label
    #   "<any other str>"  → walk ONLY contacts in that named group
    contact_seeds = [s for s in seeds if s.get("source") == "manual"]
    active_group = (operator.get("active_contact_group") or "").strip() or None
    if active_group == "__ungrouped__":
        before = len(contact_seeds)
        contact_seeds = [s for s in contact_seeds if not (s.get("group") or None)]
        log.info(
            "discovery: active_contact_group=__ungrouped__ filtered %d → %d seeds",
            before,
            len(contact_seeds),
        )
    elif active_group:
        before = len(contact_seeds)
        contact_seeds = [s for s in contact_seeds if (s.get("group") or None) == active_group]
        log.info(
            "discovery: active_contact_group=%r filtered %d → %d seeds",
            active_group,
            before,
            len(contact_seeds),
        )
    contacts_on = bool(contact_seeds)
    log.info(
        "discovery: %d contact seeds loaded for operator=%s%s",
        len(contact_seeds),
        operator_id,
        (
            " (all groups)"
            if not active_group
            else f" (group={active_group!r})"
        ),
    )

    # CONTACTS-ONLY MODE: when the operator has curated a contact list, run
    # ONLY the Unipile contact-direct path. Skip apidirect / Exa / Crustdata
    # / Unipile-keyword / title-search entirely so we don't burn LLM gate
    # cost on random keyword-surfaced authors the operator never asked for.
    # Restore full discovery by clearing the contacts list.
    if contacts_on:
        apidirect_on = False
        exa_on = False
        crustdata_on = False
        unipile_on = False  # disables keyword + seed-author walk + title-search
        log.info(
            "discovery: CONTACTS-ONLY mode active (%d contacts) — keyword sources skipped",
            len(contact_seeds),
        )
    else:
        apidirect_on = _apidirect_enabled()
        exa_on = _exa_enabled()
        crustdata_on = settings.discovery_use_crustdata
        unipile_on = settings.discovery_use_unipile

    log.info(
        "discovery: source order = %s%s%s%s%s%s",
        "contacts_unipile → " if contacts_on else "",
        "unipile_title_search RULE 24 (PRIMARY) → "
            if unipile_on and settings.discovery_use_title_search else "",
        "unipile_keyword → " if unipile_on else "(unipile off) → ",
        "crustdata (inbox+screener) → " if crustdata_on else "(crustdata off) → ",
        "apidirect → " if apidirect_on else "(apidirect off) → ",
        "exa" if exa_on else "(exa off)",
    )

    # Recency window for Exa's startPublishedDate — passes our recency filter
    # downstream and saves Exa from returning years-old posts.
    from datetime import date as _date
    exa_after = (
        (utcnow().date() - timedelta(days=settings.discovery_max_age_days)).isoformat()
        if settings.discovery_max_age_days > 0
        else None
    )

    if settings.exa_reset_circuit_each_discovery:
        reset_exa_circuit()

    for cofounder in cofounders:
        cofounder_id: ObjectId = cofounder["_id"]
        apidirect_inserted = 0
        exa_inserted = 0
        crustdata_inserted = 0
        unipile_inserted = 0
        title_search_inserted = 0
        contacts_inserted = 0

        # Skip-remaining-discovery flag check at the top of every source.
        # When the operator hits "skip discovery" in the UI, we abandon all
        # remaining sources for THIS cofounder; the gates/allocator/drafter
        # still process whatever's already been inserted.
        if _should_skip_remaining_discovery(db, slate_run_id):
            log.info("│  skip_remaining_discovery flag set — abandoning all sources for cofounder %s", cofounder_id)
            continue

        # ── SOURCE 1 (PRIMARY): RULE 24 title-search PEOPLE channel ───────
        # Promoted to first position — server-side LOCATION + INDUSTRY +
        # network_distance filter at Unipile means every returned candidate
        # already matches geography and industry by construction. Highest
        # precision source, cheapest qualification. Runs FIRST so it gets
        # the full profile-fetch budget and discovery wall-clock before the
        # broader keyword sources execute.
        if (
            unipile_on
            and settings.discovery_use_title_search
            and cofounder.get("unipile_account_id")
            and title_industry
        ):
            title_search_inserted = _run_unipile_title_search(
                db,
                operator=operator,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                account_id=cofounder["unipile_account_id"],
                title_industry=title_industry,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
            )
            inserted += title_search_inserted

        # ── SOURCE 2: Unipile keyword + seed-author search ─────────────────
        # Broader recall than RULE 24 — fuzzy keyword match at LinkedIn's
        # classic search index. Per-candidate enrichment via `/users/{slug}`
        # + dual-path rubric qualify inside `_run_unipile`.
        if unipile_on:
            account_id = cofounder.get("unipile_account_id")
            if not account_id:
                log.warning(
                    "discovery: cofounder %s has no unipile_account_id — "
                    "skipping Unipile (primary source); falling back to "
                    "crustdata/apidirect/exa for this cofounder",
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
            else:
                unipile_inserted = _run_unipile(
                    db,
                    operator=operator,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    account_id=account_id,
                    tier_1=tier_1,
                    tier_2=tier_2,
                    tier_3=tier_3,
                    title_industry=title_industry,
                    seeds=seeds,
                    seen_urls=seen_urls,
                    seen_authors_shipped=seen_authors_shipped,
                )
                inserted += unipile_inserted

        # ── SOURCE 3: Crustdata inbox + optional realtime screener ───────────
        if crustdata_on:
            inbox_n = _drain_crustdata_inbox(
                db,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
            )
            screener_n = _run_crustdata_screener(
                db,
                operator=operator,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                tier_1=tier_1,
                tier_2=tier_2,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
            )
            crustdata_inserted = inbox_n + screener_n
            inserted += crustdata_inserted
            if crustdata_inserted == 0 and crustdata_simulation_ping:
                _maybe_crustdata_simulation_ping_after_empty_inbox(operator, cofounder_id)
        if crustdata_on:
            log.info(
                "crustdata.discovery: cofounder=%s total=%d (inbox=%d screener=%d)",
                cofounder_id,
                crustdata_inserted,
                inbox_n,
                screener_n,
            )

        # ── SOURCE 4: apidirect synchronous keyword search ─────────────────
        if apidirect_on:
            apidirect_inserted = _run_apidirect(
                db,
                operator=operator,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                tier_1=tier_1,
                tier_2=tier_2,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
                account_id=cofounder.get("unipile_account_id"),
            )
            inserted += apidirect_inserted

        # ── SOURCE 5: Exa LinkedIn-scoped search (high numResults per call) ──
        if exa_on:
            exa_inserted = _run_exa(
                db,
                operator=operator,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                tier_1=tier_1,
                tier_2=tier_2,
                tier_3=tier_3,
                seen_urls=seen_urls,
                seen_authors_shipped=seen_authors_shipped,
                start_published_date=exa_after,
                account_id=cofounder.get("unipile_account_id"),
            )
            inserted += exa_inserted

        # ── SOURCE 6: Contact seeds (Unipile direct, gates bypassed) ─────
        # Operator-curated list: pull each contact's recent posts straight
        # from Unipile and write them with status="gate_passed" so the
        # allocator + drafter pick them up without running verification or
        # any of the four gates.
        if contacts_on:
            contacts_inserted = _run_contact_seeds_unipile(
                db,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                account_id=cofounder.get("unipile_account_id") or "",
                seeds=contact_seeds,
                seen_urls=seen_urls,
            )
            inserted += contacts_inserted

        log.info(
            "discovery: cofounder=%s total=%d (apidirect=%d, exa=%d, crustdata=%d, unipile=%d, title_search=%d, contacts=%d)",
            cofounder_id,
            apidirect_inserted + exa_inserted + crustdata_inserted + unipile_inserted + title_search_inserted + contacts_inserted,
            apidirect_inserted,
            exa_inserted,
            crustdata_inserted,
            unipile_inserted,
            title_search_inserted,
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


def _enrich_authors_for_post_batch(
    db: Database,
    *,
    operator_id: ObjectId,
    posts: list[Any],
    account_id: str | None,
    slate_run_id: ObjectId | None,
    ttl_days: int,
) -> dict[str, dict[str, Any]]:
    """Crustdata-first → Unipile-fallback enrichment for a batch of posts.

    Designed for the APIDirect and Exa keyword paths where each post has
    ``post.url`` (the LinkedIn post URL) but no pre-set ``author_profile_url``.
    Returns a dict keyed by **post.url** so the caller can look up the
    enriched profile per post: ``profiles[post.url]`` → dict or absent.

    Three passes, identical semantics to the inline chain in `_run_unipile`:

    1. **Cache** — `unipile_author_cache` by (operator_id, public_identifier).
       Free.
    2. **Crustdata batch** — only when ``enrichment_strategy`` permits it
       (crustdata_first / crustdata_only) and the slug is likely a person.
       URN-form slugs are skipped (Crustdata's index isn't URN-aware).
    3. **Unipile `/users/{slug}` fallback** — for posts still unenriched
       after passes 1+2. Slate-wide atomic budget enforced. Skipped under
       ``crustdata_only``.

    Failures are silent: callers receive an absent key and fall back to
    "no enriched profile available"."""
    if not posts:
        return {}

    enrichment_strategy = settings.discovery_unipile_enrichment_strategy
    crustdata_enabled = enrichment_strategy in ("crustdata_first", "crustdata_only")
    unipile_fallback_allowed = (
        enrichment_strategy in ("crustdata_first", "unipile_only")
        and bool(account_id)
    )

    out: dict[str, dict[str, Any]] = {}

    # PASS 1: cache lookup
    pending_for_crustdata: list[Any] = []
    pending_for_unipile: list[tuple[Any, str]] = []
    for post in posts:
        post_url = (getattr(post, "url", "") or "").strip()
        if not post_url:
            continue
        slug = _extract_author_slug_from_post_url(post_url)
        if not slug:
            continue
        cached = _lookup_unipile_author_cache(
            db,
            operator_id=operator_id,
            provider_id=None,
            public_identifier=slug,
            ttl_days=ttl_days,
        )
        if cached is not None:
            out[post_url] = cached
            continue
        pending_for_crustdata.append(post)
        pending_for_unipile.append((post, slug))

    # PASS 2: Crustdata batch
    if crustdata_enabled and pending_for_crustdata:
        cd_results = _enrich_via_crustdata(
            db,
            operator_id=operator_id,
            posts_pending=pending_for_crustdata,
        )
        if cd_results:
            for post in pending_for_crustdata:
                post_url = (getattr(post, "url", "") or "").strip()
                if not post_url or post_url in out:
                    continue
                slug = _extract_author_slug_from_post_url(post_url) or ""
                if not slug:
                    continue
                derived_url = f"https://www.linkedin.com/in/{slug}"
                prof = cd_results.get(derived_url) or cd_results.get(
                    getattr(post, "author_profile_url", "") or ""
                )
                if prof is not None:
                    out[post_url] = prof

    # PASS 3: Unipile fallback (per-post; throttle enforced inside resolve_profile)
    if unipile_fallback_allowed:
        for post, slug in pending_for_unipile:
            post_url = (getattr(post, "url", "") or "").strip()
            if not post_url or post_url in out:
                continue
            profile, _was_fetched = _get_cached_unipile_author_profile_by_slug(
                db,
                operator_id=operator_id,
                slug=slug,
                account_id=account_id or "",
                ttl_days=ttl_days,
                slate_run_id=slate_run_id,
            )
            if profile is not None:
                out[post_url] = profile

    return out


def _enrich_via_unipile_slug(
    db: Database,
    *,
    operator_id: ObjectId,
    post_url: str,
    account_id: str | None,
    ttl_days: int,
    slate_run_id: ObjectId | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Extract author slug from post URL → Unipile profile (cached). Profile
    fetch budget is enforced atomically on the slate run when ``slate_run_id``
    is set."""
    if not account_id or not post_url:
        return None, False
    slug = _extract_author_slug_from_post_url(post_url)
    if not slug:
        return None, False
    profile, was_fetched = _get_cached_unipile_author_profile_by_slug(
        db,
        operator_id=operator_id,
        slug=slug,
        account_id=account_id,
        ttl_days=ttl_days,
        slate_run_id=slate_run_id,
    )
    return profile, was_fetched


def _run_apidirect(
    db: Database,
    *,
    operator: dict[str, Any],
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    tier_1: list[str],
    tier_2: list[str],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
    account_id: str | None = None,
) -> int:
    """Synchronous keyword search via apidirect. Trips its own circuit on 402;
    we just stop calling it for the rest of the run.

    Builds the plan straight from the operator's tier pools — no no-repeat
    ledger, so the same keyword can run every day."""
    plan: list[tuple[str, str]] = []
    for kw in _rotate(tier_1, DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(tier_2, DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))

    if not plan:
        log.warning(
            "│  [apidirect]   plan EMPTY — operator has no tier_1/tier_2 keywords (tier_1=%d, tier_2=%d)",
            len(tier_1), len(tier_2),
        )
        return 0

    pages = max(1, min(int(settings.discovery_apidirect_max_pages), 5))
    enrich_on = bool(
        account_id and settings.discovery_unipile_inline_rubric_enabled
    )
    cache_ttl = settings.discovery_unipile_author_cache_ttl_days
    enriched_fetch_count = 0
    t_ap = time.monotonic()

    inserted = 0
    for query, classification in plan:
        if _should_skip_remaining_discovery(db, slate_run_id):
            log.info("│  [apidirect]   skip_remaining_discovery flag set — exiting")
            break
        if _discovery_wall_clock_exceeded(
            t_ap, settings.discovery_wall_clock_cap_seconds_apidirect
        ):
            log.info(
                "│  [apidirect]   wall-clock cap (%ds) — stopping",
                settings.discovery_wall_clock_cap_seconds_apidirect,
            )
            break
        # APIDirect's /v1/linkedin/posts is a strict text-match index — same
        # recall-collapse pattern as Unipile classic. We drop the geo +
        # seniority suffix here too; the LLM ICP gate enforces ICP author
        # filtering downstream from author metadata.
        vendor_q = _compose_discovery_query(
            query, operator, with_geo=False, with_seniority=False
        )
        try:
            posts = search_linkedin_posts_pages(vendor_q, max_pages=pages)
        except (ApiDirectQuotaExhausted, ApiDirectNotConfigured) as err:
            log.warning("discovery: apidirect halted mid-run: %s", err)
            return inserted
        except ApiDirectError as err:
            log.warning("discovery: apidirect %r failed: %s", vendor_q, err)
            continue

        # Crustdata-first → Unipile-fallback batch enrichment: pre-resolve
        # every viable post's author profile up front so a popular author
        # appearing in multiple posts only costs one Crustdata credit, and
        # Crustdata's API benefits from batching (25 URLs/call).
        viable_posts = [
            p for p in posts
            if p.url and p.url not in seen_urls
            and not _is_exhausted(db, operator_id, p.url)
        ]
        author_profiles: dict[str, dict[str, Any]] = {}
        if enrich_on and viable_posts:
            author_profiles = _enrich_authors_for_post_batch(
                db,
                operator_id=operator_id,
                posts=viable_posts,
                account_id=account_id,
                slate_run_id=slate_run_id,
                ttl_days=cache_ttl,
            )
            enriched_fetch_count += len(author_profiles)

        for post in posts:
            if not post.url or post.url in seen_urls:
                continue
            if _is_exhausted(db, operator_id, post.url):
                continue
            details = None
            if settings.discovery_apidirect_fetch_post_details:
                try:
                    details = _apidirect_optional_details(post.url)
                except ApiDirectQuotaExhausted as err:
                    log.warning("discovery: apidirect halted (post details): %s", err)
                    return inserted

            enriched_profile: dict[str, Any] | None = author_profiles.get(post.url)

            # Drop posts positively identified as company-authored (LinkedIn
            # /company/ pages, brand-shaped slugs with no human signal, or
            # enrichment flagging is_company). Posts with thin author data
            # but no company signal still flow through — the LLM ICP gate
            # and post-relevance scoring decide whether they're worth slating.
            if _is_company_authored(post.author, enriched_profile, post.url):
                log.info(
                    "│  [DROP/apidirect] %s ← company-authored post (not a human)",
                    (post.url or "<no-url>")[:90],
                )
                continue

            # Inline geo gate (US-targeting operators only). We require
            # enriched_profile.location to resolve as US via the offline
            # geonamescache resolver — same gate the keyword Unipile path
            # applies. Direct-author (RULE 24) and Exa paths already enforce
            # geo at the source so they bypass this. Posts with no enriched
            # location pass through (geo can't be evaluated → LLM ICP gate
            # downstream decides) unless we got a clearly non-US signal.
            if _operator_targets_us(operator) and enriched_profile:
                loc = (enriched_profile.get("location") or "").strip()
                if loc:
                    us_verdict = is_us_location(loc)
                    if us_verdict is False:
                        log.info(
                            "│  [DROP/apidirect] %s ← author_location not US: %r",
                            (post.url or "<no-url>")[:90], loc,
                        )
                        rejected = _doc_from_apidirect(
                            post,
                            operator_id=operator_id,
                            cofounder_id=cofounder_id,
                            slate_run_id=slate_run_id,
                            source_keyword=query,
                            source_classification=classification,
                            details=details,
                            enriched_profile=enriched_profile,
                        )
                        rejected["status"] = "rejected_inline"
                        rejected["drop_reason"] = (
                            f"inline_geo: author_location not US ({loc!r})"
                        )
                        try:
                            db.candidates.insert_one(rejected)
                            _discovery_record_insert(db, rejected)
                        except Exception as err:
                            log.warning(
                                "│  [apidirect] persist rejected_inline failed: %s", err
                            )
                        seen_urls.add(post.url)
                        continue

            seen_urls.add(post.url)
            ap_doc = _doc_from_apidirect(
                post,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                source_keyword=query,
                source_classification=classification,
                details=details,
                enriched_profile=enriched_profile,
            )
            # Mark `geo_verified_at_source=True` when the resolver confirmed
            # US — saves the LLM ICP gate from re-scoring geography on a
            # candidate we already verified deterministically.
            _mark_geo_verified_by_resolver(ap_doc, operator, enriched_profile)
            db.candidates.insert_one(ap_doc)
            _discovery_record_insert(db, ap_doc)
            inserted += 1
    if enrich_on:
        log.info(
            "│  [apidirect-enrich] profile fetches=%d cache_ttl=%dd",
            enriched_fetch_count,
            cache_ttl,
        )
    return inserted


def _run_exa(
    db: Database,
    *,
    operator: dict[str, Any],
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    tier_1: list[str],
    tier_2: list[str],
    tier_3: list[str],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
    start_published_date: str | None,
    account_id: str | None = None,
) -> int:
    """Exa LinkedIn-scoped semantic search. One API call per keyword, up to
    ~100 results per call (configurable via EXA_RESULTS_PER_QUERY, capped at 100).
    Uses a wider keyword plan than apidirect/unipile so each run pulls enough
    raw posts to survive downstream gates. Trips the circuit on 401/402/429.

    RULE 15 14-day no-repeat ledger filters all three tier pools."""
    plan: list[tuple[str, str]] = []
    for kw in _rotate(tier_1, EXA_DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(tier_2, EXA_DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))
    for kw in _rotate(tier_3, EXA_DISCOVERY_TIER_3_PER_RUN):
        plan.append((kw, "B"))

    if not plan:
        log.warning(
            "│  [exa]         plan EMPTY — operator has no tier_1/tier_2/tier_3 keywords (tier_1=%d, tier_2=%d, tier_3=%d)",
            len(tier_1), len(tier_2), len(tier_3),
        )
        return 0

    enrich_on = bool(
        account_id and settings.discovery_unipile_inline_rubric_enabled
    )
    cache_ttl = settings.discovery_unipile_author_cache_ttl_days
    enriched_fetch_count = 0
    t_ex = time.monotonic()

    inserted = 0
    for query, classification in plan:
        if _should_skip_remaining_discovery(db, slate_run_id):
            log.info("│  [exa]         skip_remaining_discovery flag set — exiting")
            break
        if _discovery_wall_clock_exceeded(
            t_ex, settings.discovery_wall_clock_cap_seconds_exa
        ):
            log.info(
                "│  [exa]         wall-clock cap (%ds) — stopping",
                settings.discovery_wall_clock_cap_seconds_exa,
            )
            break
        # Exa is a neural/semantic search — adding geo + seniority terms to
        # the query string doesn't help (the embedding handles geography as
        # a separate concept) and tends to skew results toward government /
        # job-board content that literally mentions "United States Director".
        vendor_q = _compose_discovery_query(
            query, operator, with_geo=False, with_seniority=False
        )
        try:
            posts = exa_search_linkedin_posts(
                vendor_q, start_published_date=start_published_date
            )
        except (ExaQuotaExhausted, ExaNotConfigured) as err:
            log.warning("discovery: exa halted mid-run: %s", err)
            return inserted
        except ExaError as err:
            log.warning("discovery: exa %r failed: %s", vendor_q, err)
            continue

        # Crustdata-first → Unipile-fallback batch enrichment (same as the
        # APIDirect path). Resolves all viable authors before the per-post
        # iteration so a popular author appearing in multiple Exa results
        # only costs one Crustdata credit per slate.
        viable_posts = [
            p for p in posts
            if p.url and p.url not in seen_urls
            and not _is_exhausted(db, operator_id, p.url)
        ]
        author_profiles: dict[str, dict[str, Any]] = {}
        if enrich_on and viable_posts:
            author_profiles = _enrich_authors_for_post_batch(
                db,
                operator_id=operator_id,
                posts=viable_posts,
                account_id=account_id,
                slate_run_id=slate_run_id,
                ttl_days=cache_ttl,
            )
            enriched_fetch_count += len(author_profiles)

        for post in posts:
            if not post.url or post.url in seen_urls:
                continue
            if _is_exhausted(db, operator_id, post.url):
                continue

            enriched_profile: dict[str, Any] | None = author_profiles.get(post.url)

            # STRICTER than the keyword-Unipile/APIDirect paths: Exa's
            # neural search surfaces many non-individual posts (brand pages,
            # /pulse/ articles, /newsletters/, brand-shaped slugs). Default
            # is flipped: require POSITIVE proof of an individual author
            # before keeping. Reasons surface in logs for the gate-funnel UI.
            drop_exa, exa_drop_reason = _is_exa_non_individual_author(
                post.author, enriched_profile, post.url
            )
            if drop_exa:
                log.info(
                    "│  [DROP/exa] %s ← non_individual_author (%s)",
                    (post.url or "<no-url>")[:90],
                    exa_drop_reason or "no_signal",
                )
                continue

            # Default location for Exa: Exa's API doesn't return a `location`
            # field, and many Exa-surfaced authors won't be in Unipile's
            # /users/{slug} resolver (e.g. /pulse/ articles, authors with
            # unusual slugs). Without a Location string the LLM ICP gate
            # falls back to inferring geography from post text alone, which
            # most often scores G=0 → drop. Since Exa's keyword pool is
            # operator-geo biased anyway, treat Exa results as
            # operator-primary-geo by default and let the LLM override via
            # POST-EXPLICIT when the post text clearly indicates otherwise.
            if not (enriched_profile and (enriched_profile.get("location") or "").strip()):
                primary_geo = _operator_primary_geography(operator)
                if primary_geo:
                    if enriched_profile is None:
                        enriched_profile = {}
                    else:
                        enriched_profile = dict(enriched_profile)
                    enriched_profile["location"] = primary_geo
                    enriched_profile["_location_source"] = "exa_default"

            seen_urls.add(post.url)
            exa_doc = _doc_from_exa(
                post,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                source_keyword=query,
                source_classification=classification,
                enriched_profile=enriched_profile,
            )
            # Resolver-based geo verification — skipped when location was
            # synthesized from `_operator_primary_geography` (see the
            # `_location_source='exa_default'` guard inside the helper).
            _mark_geo_verified_by_resolver(exa_doc, operator, enriched_profile)
            db.candidates.insert_one(exa_doc)
            _discovery_record_insert(db, exa_doc)
            inserted += 1
    if enrich_on:
        log.info(
            "│  [exa-enrich] profile fetches=%d cache_ttl=%dd",
            enriched_fetch_count,
            cache_ttl,
        )
    return inserted


def _run_unipile(
    db: Database,
    *,
    operator: dict[str, Any],
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    account_id: str,
    tier_1: list[str],
    tier_2: list[str],
    tier_3: list[str],
    title_industry: list[str],
    seeds: list[dict[str, Any]],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
) -> int:
    # No no-repeat ledger — the operator's tier pools feed the plan directly.
    plan: list[tuple[str, str, str, str]] = []
    for kw in _rotate(tier_1, DISCOVERY_TIER_1_PER_RUN):
        plan.append(("kw", kw, "tier_1_kw", "A"))
    for kw in _rotate(tier_2, DISCOVERY_TIER_2_PER_RUN):
        plan.append(("kw", kw, "tier_2_kw", "B"))
    for kw in _rotate(tier_3, DISCOVERY_TIER_3_PER_RUN):
        plan.append(("kw", kw, "tier_3_kw", "B"))
    for kw in _rotate(title_industry, DISCOVERY_TITLE_INDUSTRY_PER_RUN):
        plan.append(("kw", kw, "title_industry_kw", "A"))
    for seed in seeds:
        seed_source = (
            "manual_seed" if seed.get("source") == "manual" else "embedded_harvest"
        )
        # General Unipile: `search_posts_pages` only. `get_user_posts` is for
        # contact-only `_run_contact_seeds_unipile` (and RULE 24 title search).
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

    if not plan:
        log.warning(
            "│  [unipile]     plan EMPTY — operator has no tier_1/tier_2/tier_3/title_industry keywords and no seeds "
            "(t1=%d t2=%d t3=%d ti=%d seeds=%d)",
            len(tier_1), len(tier_2), len(tier_3), len(title_industry), len(seeds),
        )
        return 0

    # NOTE: we used to resolve operator geography → Unipile location IDs and
    # pass them as ``body["location"]`` on the keyword search. Empirically
    # that field is ignored by LinkedIn's content index (the standalone Unipile
    # script proved US/UK geoUrn returned identical results; this run proved
    # the same — 137 non-US authors slipped past a US-only location_ids list).
    # We no longer call PARAMETERS → location_ids for the keyword path.
    # Geo for keyword candidates is enforced by the inline rubric (post-fetch
    # author location match against operator.target_geographies).
    inserted = 0
    # Profile fetches share an atomic per-slate budget (``slate_runs.fetch_budget_remaining``).
    cache_ttl = settings.discovery_unipile_author_cache_ttl_days
    use_inline_rubric = settings.discovery_unipile_inline_rubric_enabled
    rubric_drops = {"no_profile": 0, "geo": 0, "rubric": 0}
    # Crustdata-first enrichment strategy + Unipile fallback budget.
    # See `_enrich_via_crustdata` and the 4-pass flow in the per-query loop
    # below.
    enrichment_strategy = settings.discovery_unipile_enrichment_strategy
    crustdata_enabled = enrichment_strategy in ("crustdata_first", "crustdata_only")
    unipile_fallback_allowed = enrichment_strategy in ("crustdata_first", "unipile_only")
    unipile_fallback_remaining = (
        settings.discovery_unipile_fallback_max_fetches_per_run
        if enrichment_strategy == "crustdata_first"
        else None  # unipile_only has no per-strategy cap (slate budget still applies)
    )
    enrichment_counters = {
        "cache_hit": 0,
        "crustdata_matched": 0,
        "unipile_fallback": 0,
        "no_profile": 0,
    }

    t_kw = time.monotonic()
    for kind, payload, source, classification in plan:
        assert kind == "kw"
        if _should_skip_remaining_discovery(db, slate_run_id):
            log.info("│  [unipile-kw]  skip_remaining_discovery flag set — exiting keyword source")
            break
        if _discovery_wall_clock_exceeded(
            t_kw, settings.discovery_wall_clock_cap_seconds_unipile_keyword
        ):
            log.info(
                "│  [unipile-kw]  wall-clock cap (%ds) — stopping keyword source",
                settings.discovery_wall_clock_cap_seconds_unipile_keyword,
            )
            break
        # KEY FIX: LinkedIn's classic post keyword search is strict text-match.
        # When `location_ids` is set, the engine already enforces geo
        # IMPORTANT empirical finding (confirmed in standalone Unipile script
        # AND in this engine): LinkedIn's /linkedin/search?category=posts
        # endpoint does NOT enforce the body-level ``location`` field — it's
        # a soft bias at most, not a hard server-side filter (US-targeted
        # `location_ids` returns plenty of Cairo / Bengaluru / Toronto
        # authors). Real geo enforcement on the keyword path happens
        # post-fetch in the inline rubric (`_qualifies_inline_rubric`)
        # which substring-matches the enriched author profile location
        # against the operator's target_geographies + non-US blocklist.
        # location_ids is left out below — we don't want the false sense
        # that we're geo-filtering at source, and we save one PARAMETERS
        # lookup per run by not bothering to resolve it.
        #
        # Geo-text suffix on the keyword is also OFF (we send the bare
        # payload), because LinkedIn's content index is strict text-match
        # and any extra word collapses recall.
        try:
            log.info(
                "│  [unipile-kw] query=%r (bare keyword; geo enforced post-fetch by inline rubric, not by server)",
                payload,
            )
            posts = search_posts_pages(
                account_id=account_id,
                query=payload,
                max_pages=settings.discovery_unipile_post_max_pages,
                per_page=settings.discovery_unipile_post_limit,
                sort_by=settings.discovery_unipile_post_sort_by or None,
                date_posted=settings.discovery_unipile_post_date_window or None,
                content_type=(
                    settings.discovery_unipile_post_content_type.strip()
                    if settings.discovery_unipile_post_content_type.strip()
                    else None
                ),
                author_keywords=settings.discovery_unipile_post_author_filter or None,
                location_ids=None,  # see comment above — body filter doesn't work on content search
            )
            log.info(
                "│  [unipile-kw] result query=%r posts=%d source=%s",
                payload, len(posts), source,
            )
        except UnipileNotConfigured as err:
            log.warning("discovery: unipile unconfigured, halting: %s", err)
            return inserted
        except UnipileError as err:
            log.warning("discovery: unipile kw failed for %r: %s", payload, err)
            continue

        # Tag content-search hits with source_channel for downstream routing.
        if source in ("tier_1_kw", "tier_2_kw", "tier_3_kw"):
            post_source_channel = "keyword_topical"
        elif source == "title_industry_kw":
            post_source_channel = "keyword_title_industry"
        else:
            post_source_channel = ""

        # ── PASS 1: pre-filter posts + cache lookup ──────────────────────
        # Build the list of posts that survive the cheap dedup/skip checks,
        # along with each post's cached profile (None if a miss). Cache hits
        # never trigger vendor calls.
        viable: list[tuple[UnipilePost, dict[str, Any] | None]] = []
        for post in posts:
            if not post.url or post.url in seen_urls:
                continue
            if settings.discovery_unipile_skip_company_posts and post.author_is_company:
                continue
            author_url = post.author_profile_url or ""
            if author_url and author_url in seen_authors_shipped:
                continue
            if _is_exhausted(db, operator_id, author_url or post.url):
                continue
            slug_from_url = _public_identifier_from_url(author_url)
            cached = _lookup_unipile_author_cache(
                db,
                operator_id=operator_id,
                provider_id=post.author_provider_id or None,
                public_identifier=slug_from_url,
                ttl_days=cache_ttl,
            )
            if cached is not None:
                enrichment_counters["cache_hit"] += 1
            viable.append((post, cached))

        # ── PASS 2: Crustdata batch enrichment for cache misses ─────────
        # Only attempted when strategy ∈ {crustdata_first, crustdata_only}.
        # `_enrich_via_crustdata` filters out URN-form slugs internally so
        # we don't waste credits on guaranteed-miss URLs.
        if use_inline_rubric and crustdata_enabled:
            pending_for_crustdata = [post for post, prof in viable if prof is None]
            if pending_for_crustdata:
                cd_results = _enrich_via_crustdata(
                    db,
                    operator_id=operator_id,
                    posts_pending=pending_for_crustdata,
                )
                if cd_results:
                    enrichment_counters["crustdata_matched"] += len(cd_results)
                    # Re-map viable's profile slot for each post that matched.
                    new_viable: list[tuple[UnipilePost, dict[str, Any] | None]] = []
                    for post, prof in viable:
                        if prof is None:
                            url = post.author_profile_url or ""
                            prof = cd_results.get(url)
                        new_viable.append((post, prof))
                    viable = new_viable

        # ── PASS 3: Unipile fallback for still-unenriched posts ─────────
        # Skipped entirely under strategy="crustdata_only". Capped per slate
        # by `discovery_unipile_fallback_max_fetches_per_run` under
        # strategy="crustdata_first"; uncapped (but still bound by the
        # atomic slate fetch budget) under strategy="unipile_only".
        if use_inline_rubric and unipile_fallback_allowed:
            new_viable = []
            for post, prof in viable:
                if prof is not None:
                    new_viable.append((post, prof))
                    continue
                if (
                    unipile_fallback_remaining is not None
                    and unipile_fallback_remaining <= 0
                ):
                    new_viable.append((post, None))
                    continue
                provider_id = post.author_provider_id or ""
                fetched = None
                if provider_id:
                    fetched, was_fetched = _get_cached_unipile_author_profile(
                        db,
                        operator_id=operator_id,
                        provider_id=provider_id,
                        account_id=account_id,
                        ttl_days=cache_ttl,
                        slate_run_id=slate_run_id,
                    )
                    if was_fetched and unipile_fallback_remaining is not None:
                        unipile_fallback_remaining -= 1
                    if fetched is not None:
                        enrichment_counters["unipile_fallback"] += 1
                new_viable.append((post, fetched))
            viable = new_viable

        # ── PASS 4: score + qualify + insert/drop ───────────────────────
        # The rubric / drop / insert behaviour matches the prior per-post
        # implementation byte-for-byte; only the source of `enriched_profile`
        # has changed.
        for post, enriched_profile in viable:
            rubric_snapshot: dict[str, Any] | None = None

            if use_inline_rubric:
                if enriched_profile is None:
                    enrichment_counters["no_profile"] += 1
                # Step 2: score author + post against the operator's ICP fields.
                author_score = _score_unipile_author_against_operator(
                    enriched_profile or {}, operator
                )
                post_score, post_matches = _score_unipile_post_against_operator(
                    post.text or "", operator
                )
                # Step 3: dual-path qualify (geo gate is conditional on the
                # operator having target_geographies set — see _qualifies_inline_rubric).
                ok, paths = _qualifies_inline_rubric(
                    author_score=author_score,
                    post_relevance=post_score,
                    operator=operator,
                )
                rubric_snapshot = {
                    "author": author_score,
                    "post_relevance": post_score,
                    "post_matches": post_matches[:8],
                    "paths": paths,
                }
                if not ok:
                    # Track WHY we dropped so the summary line is actionable.
                    if (
                        settings.discovery_unipile_inline_require_geo
                        and (operator.get("product_extracted") or {}).get("target_geographies")
                        and author_score.get("geo", 0) < 1
                    ):
                        rubric_drops["geo"] += 1
                        reason_key = "inline_geo"
                        reason = "geo_not_in_author_location"
                    elif not enriched_profile:
                        # Enrichment couldn't be obtained from any source:
                        # Crustdata miss, Unipile fallback exhausted /
                        # disabled, or no provider_id+slug pair to look up.
                        rubric_drops["no_profile"] = rubric_drops.get("no_profile", 0) + 1
                        reason_key = "inline_no_profile"
                        reason = (
                            "no_profile (crustdata miss + unipile fallback "
                            "unavailable; strategy="
                            f"{enrichment_strategy})"
                        )
                    else:
                        rubric_drops["rubric"] += 1
                        reason_key = "inline_rubric"
                        reason = (
                            f"rubric T={author_score['title']} I={author_score['industry']} "
                            f"G={author_score['geo']} post={post_score} → no path"
                        )
                    log.info(
                        "│  [DROP/unipile] %s  ←  %s",
                        (post.url or "<no-url>")[:90],
                        reason,
                    )
                    # Persist the rejected post so the gate-funnel UI can
                    # surface which Unipile posts fell out at discovery and
                    # why. Mark with status=rejected_inline + structured
                    # drop_reason so downstream stages skip it.
                    rejected_doc = _doc_from_unipile(
                        post,
                        operator_id=operator_id,
                        cofounder_id=cofounder_id,
                        slate_run_id=slate_run_id,
                        source=source,
                        source_keyword=payload,
                        source_classification=classification,
                        source_channel=post_source_channel,
                        enriched_profile=enriched_profile,
                        unipile_rubric=rubric_snapshot,
                    )
                    rejected_doc["status"] = "rejected_inline"
                    rejected_doc["drop_reason"] = f"{reason_key}: {reason}"
                    try:
                        db.candidates.insert_one(rejected_doc)
                        _discovery_record_insert(db, rejected_doc)
                    except Exception as err:
                        log.warning(
                            "│  [unipile] persist rejected_inline failed: %s", err
                        )
                    if post.url:
                        seen_urls.add(post.url)
                    continue
                rem = _profile_fetch_budget_remaining(db, slate_run_id)
                if rem is not None and rem <= 0 and use_inline_rubric:
                    log.info(
                        "│  [unipile]     profile-fetch budget exhausted "
                        "(%d/slate); remaining posts use search-payload data only",
                        settings.discovery_unipile_max_profile_fetches_per_run,
                    )
                    use_inline_rubric = False

            seen_urls.add(post.url)
            raw_doc = _doc_from_unipile(
                post,
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                source=source,
                source_keyword=payload,
                source_classification=classification,
                source_channel=post_source_channel,
                enriched_profile=enriched_profile,
                unipile_rubric=rubric_snapshot,
            )
            # Resolver verified US geo during inline rubric scoring — mark
            # so the LLM ICP gate doesn't redundantly re-score geography.
            _mark_geo_verified_by_resolver(raw_doc, operator, enriched_profile)
            db.candidates.insert_one(raw_doc)
            _discovery_record_insert(db, raw_doc)
            inserted += 1

    if settings.discovery_unipile_inline_rubric_enabled:
        log.info(
            "│  [unipile-rubric] dropped: geo=%d no_profile=%d rubric=%d",
            rubric_drops.get("geo", 0),
            rubric_drops.get("no_profile", 0),
            rubric_drops.get("rubric", 0),
        )
        log.info(
            "│  [unipile-enrich] strategy=%s sources: cache_hit=%d "
            "crustdata_matched=%d unipile_fallback=%d no_profile=%d "
            "(fallback budget remaining: %s)",
            enrichment_strategy,
            enrichment_counters["cache_hit"],
            enrichment_counters["crustdata_matched"],
            enrichment_counters["unipile_fallback"],
            enrichment_counters["no_profile"],
            unipile_fallback_remaining if unipile_fallback_remaining is not None else "n/a",
        )
    return inserted


def _run_unipile_title_search(
    db: Database,
    *,
    operator: dict[str, Any],
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    account_id: str,
    title_industry: list[str],
    seen_urls: set[str],
    seen_authors_shipped: set[str],
) -> int:
    """RULE 24 — LinkedIn PEOPLE search via Unipile, then walk each profile's
    recent activity. US geo + 2nd-degree filter (matches the audit URL).

    Volume envelope per cofounder per run:
      - up to 6 fresh queries from the title_industry pool (14-day no-repeat
        on its own channel)
      - top 10 people per query
      - up to 5 most-recent posts per person

    Each post is inserted as a candidate with source='unipile_people' and
    source_channel='title_search' so allocator/drafter can route the
    voice (RULE 18 / RULE 25)."""
    if not title_industry or not account_id:
        return 0

    plan = _rotate(title_industry, DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN)

    # Resolve operator's target industries → LinkedIn industry IDs once per
    # run (cached cross-run). When set, the people-search applies a hard
    # server-side INDUSTRY filter on top of geo + network distance, so we
    # only get candidates whose LinkedIn profile is in a target industry.
    industry_ids = tuple(
        _resolve_industry_ids_for_operator(
            db,
            operator_id=operator_id,
            operator=operator,
            account_id=account_id,
        )
    )
    if industry_ids:
        log.info(
            "│  [unipile-people] applying server-side INDUSTRY filter ids=%s",
            list(industry_ids),
        )

    inserted = 0
    t_ts = time.monotonic()
    for query in plan:
        if _should_skip_remaining_discovery(db, slate_run_id):
            log.info("│  [unipile-people] skip_remaining_discovery flag set — exiting title_search")
            break
        if _discovery_wall_clock_exceeded(
            t_ts, settings.discovery_wall_clock_cap_seconds_unipile_keyword
        ):
            log.info(
                "│  [unipile-people] wall-clock cap (%ds) — stopping title_search",
                settings.discovery_wall_clock_cap_seconds_unipile_keyword,
            )
            break
        # Unipile people search enforces US (or UNIPILE_RULE24_LOCATION_IDS) via
        # geoUrn server-side. Appending geo terms to the literal-text query
        # kills recall. Seniority hints are ALSO removed here — the base query
        # already contains a specific title (e.g. "Lead Cardiologist medical
        # group"), so concatenating a multi-token seniority tail like
        # "President Director Chief CMO CFO CEO COO VP" forces LinkedIn to
        # AND-match all of those, which collapses recall to 0. The curated
        # title × company_type query is the right signal.
        vendor_q = _compose_discovery_query(
            query, operator, with_geo=False, with_seniority=False
        )
        log.info(
            "│  [unipile-people] query=%r (geo via default geoUrn, not text)",
            vendor_q,
        )
        # Step 1 — people search (US, no degree filter — see network_distance
        # rationale below, optional INDUSTRY filter).
        # network_distance_degrees=() drops the degree filter entirely so we
        # don't cap recall to the cofounder's 2nd-degree network. Important
        # on accounts whose Unipile source is MESSAGING-only (people-search
        # already collapses to the local network for those); also lifts the
        # ceiling for SEARCH-source accounts when the cofounder's network
        # doesn't overlap the ICP cluster yet.
        try:
            people = search_people(
                account_id=account_id,
                query=vendor_q,
                limit=DISCOVERY_TITLE_SEARCH_PEOPLE_PER_QUERY,
                industry_ids=industry_ids or None,
                network_distance_degrees=(),
            )
        except UnipileNotConfigured as err:
            log.warning("discovery: title_search unipile unconfigured: %s", err)
            return inserted
        except UnipileError as err:
            log.warning("discovery: title_search %r failed: %s", query, err)
            continue

        # Step 2 — for each person, walk recent activity.
        for person in people:
            author_url = person.profile_url
            if author_url and author_url in seen_authors_shipped:
                continue
            if author_url and _is_exhausted(db, operator_id, author_url):
                continue
            try:
                posts = get_user_posts(
                    account_id=account_id,
                    public_identifier_or_url=person.public_identifier or author_url,
                    limit=DISCOVERY_TITLE_SEARCH_POSTS_PER_PERSON,
                )
            except UnipileError as err:
                log.warning(
                    "discovery: title_search recent-activity for %s failed: %s",
                    person.public_identifier,
                    err,
                )
                continue

            # Build a synthetic "enriched_profile" from the UnipilePerson
            # fields so the candidate doc gets `author_location` set and the
            # expensive ICP LLM gate sees explicit Location data instead of
            # having to infer geography from post text alone. This was the
            # weak link that gave most RULE 24 candidates G=0.
            people_profile = {
                "name": person.name,
                "headline": person.title,
                "company": person.company,
                "location": person.location,
            }
            for post in posts:
                if not post.url or post.url in seen_urls:
                    continue
                if settings.discovery_unipile_skip_company_posts and post.author_is_company:
                    continue
                seen_urls.add(post.url)
                doc = _doc_from_unipile(
                    post,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source="unipile_people",
                    source_keyword=query,
                    # 2nd-degree US-filtered title hits are tier-1 ICP density.
                    source_classification="A",
                    source_channel="title_search",
                    enriched_profile=people_profile,
                )
                # Tag candidates that were filtered server-side via geoUrn (and
                # optionally INDUSTRY) so the LLM ICP gate auto-credits those
                # axes at the top rubric tier instead of re-evaluating from
                # post text. We already know they're US-located and in-industry.
                doc["geo_verified_at_source"] = True
                if industry_ids:
                    doc["industry_verified_at_source"] = True
                # RULE 24 candidates are ICP-proven by construction (server-side
                # LOCATION + INDUSTRY + network_distance + title-keyword match
                # all enforced by LinkedIn). Tag inline_icp_qualified so the
                # downstream cheap/expensive gates skip non_buyer + icp_scoring
                # (post_quality and analyst_reportage still run — they judge
                # POST content, not author ICP fit). Saves ~$0.02/candidate of
                # redundant LLM ICP scoring AND prevents non_buyer from
                # misclassifying clear-ICP execs whose post happens to read as
                # commentary rather than buyer language.
                doc["inline_icp_qualified"] = True
                db.candidates.insert_one(doc)
                _discovery_record_insert(db, doc)
                inserted += 1

    log.info(
        "discovery: title_search cofounder=%s queries=%d inserted=%d",
        cofounder_id,
        len(plan),
        inserted,
    )
    return inserted


def _contact_seed_post_text(
    post: ApiDirectPost | ExaPost,
    *,
    name: str,
    title: str,
    company: str,
) -> str:
    """Merge vendor snippet with seed identity so verification min-length passes."""
    snip = (getattr(post, "snippet", None) or "").strip()
    ptitle = (getattr(post, "title", None) or "").strip()
    return " ".join(
        p
        for p in (snip, ptitle, name, title, company)
        if isinstance(p, str) and p.strip()
    ).strip()


def _run_contact_seeds(
    db: Database,
    *,
    operator: dict[str, Any],
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

        # Contact-seed queries are already specific (name+title+company);
        # adding geo+seniority terms over-constrains the literal-match
        # APIDirect query and tends to drop the seed entirely.
        vendor_query = _compose_discovery_query(
            query, operator, with_geo=False, with_seniority=False
        )

        # Track the seed's author URL for dedupe
        seed_author_url = linkedin_url or None
        if seed_author_url and seed_author_url in seen_authors_shipped:
            continue
        if seed_author_url and _is_exhausted(db, operator_id, seed_author_url):
            continue

        seed_pages = max(1, min(int(settings.discovery_contact_seed_apidirect_pages), 5))

        # ── apidirect search ──────────────────────────────────────────────
        if apidirect_on:
            try:
                posts = search_linkedin_posts_pages(vendor_query, max_pages=seed_pages)
            except (ApiDirectQuotaExhausted, ApiDirectNotConfigured) as err:
                log.warning("discovery: contact_seeds apidirect halted: %s", err)
            except ApiDirectError as err:
                log.warning("discovery: contact_seeds apidirect %r failed: %s", vendor_query, err)
            else:
                for post in posts:
                    if not post.url or post.url in seen_urls:
                        continue
                    if _is_exhausted(db, operator_id, post.url):
                        continue

                    details = None
                    if settings.discovery_apidirect_fetch_post_details:
                        try:
                            details = _apidirect_optional_details(post.url)
                        except ApiDirectQuotaExhausted as err:
                            log.warning(
                                "discovery: contact_seeds apidirect halted (details): %s",
                                err,
                            )
                            return inserted

                    text_src: ApiDirectPost | SimpleNamespace = post
                    if details is not None and details.text.strip():
                        text_src = SimpleNamespace(
                            snippet=details.text,
                            title=getattr(post, "title", None) or "",
                        )
                    body = _contact_seed_post_text(
                        text_src, name=name, title=title, company=company
                    )
                    author_nm = (details.author if details and details.author else None) or post.author or name
                    author_u = seed_author_url or (
                        details.author_url if details else None
                    )
                    pub_at = post.published_at
                    if details is not None and details.published_at is not None:
                        pub_at = details.published_at
                    post_uid = details.urn if details and details.urn else None

                    seen_urls.add(post.url)
                    cs_doc = _candidate_doc(
                        operator_id=operator_id,
                        cofounder_id=cofounder_id,
                        slate_run_id=slate_run_id,
                        post_url=post.url,
                        post_id=post_uid,
                        author_name=author_nm,
                        author_title=title or None,
                        author_company=company or None,
                        author_linkedin_url=author_u,
                        post_text=body or (post.snippet or post.title or ""),
                        post_published_at=pub_at,
                        source="contact_seed",
                        source_keyword=query,
                        source_classification="A",
                    )
                    db.candidates.insert_one(cs_doc)
                    _discovery_record_insert(db, cs_doc)
                    inserted += 1

        # ── Exa search ────────────────────────────────────────────────────
        if exa_on:
            try:
                exa_posts = exa_search_linkedin_posts(
                    vendor_query,
                    num_results=settings.exa_results_per_query,
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
                    body = _contact_seed_post_text(
                        post, name=name, title=title, company=company
                    )
                    cs2 = _candidate_doc(
                        operator_id=operator_id,
                        cofounder_id=cofounder_id,
                        slate_run_id=slate_run_id,
                        post_url=post.url,
                        post_id=None,
                        author_name=post.author or name,
                        author_title=title or None,
                        author_company=company or None,
                        author_linkedin_url=author_url,
                        post_text=body or (post.snippet or post.title or ""),
                        post_published_at=post.published_at,
                        source="contact_seed",
                        source_keyword=query,
                        source_classification="A",
                    )
                    db.candidates.insert_one(cs2)
                    _discovery_record_insert(db, cs2)
                    inserted += 1

    log.info(
        "discovery: contact_seeds searched %d contacts, inserted %d candidates",
        len(seeds),
        inserted,
    )
    return inserted


def _run_contact_seeds_unipile(
    db: Database,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    account_id: str,
    seeds: list[dict[str, Any]],
    seen_urls: set[str],
) -> int:
    """Contact-only path: pull recent posts via Unipile and skip the entire
    verification + 4-gate funnel.

    For every imported contact we walk their public LinkedIn profile via
    Unipile's `get_user_posts` endpoint. Each returned post is written
    directly with status="gate_passed" so the allocator + drafter pick them
    up unchanged — the operator already curated this list, we don't need
    the engine to second-guess them.

    A synthetic ICP score (10) is attached so the allocator's score-desc
    sort floats these candidates ahead of any keyword-discovered survivors
    in the same cofounder bucket.
    """
    if not seeds:
        return 0
    if not account_id:
        log.info(
            "discovery: contact_seeds_unipile skipped — no unipile_account_id "
            "for cofounder=%s",
            cofounder_id,
        )
        return 0

    inserted = 0
    too_old_dropped = 0
    quality_dropped = 0
    posts_per_contact = max(1, int(settings.discovery_contact_unipile_posts_per_user))

    # Recency cutoff for the bypass path. Verification's max_age filter is
    # skipped here, so enforce freshness locally. 0 disables.
    max_age_days = int(settings.discovery_contact_unipile_max_age_days or 0)
    cutoff = utcnow() - timedelta(days=max_age_days) if max_age_days > 0 else None

    # Whether to run the post_quality LLM gate inline. Drops engagement-bait,
    # event announcements, recruiting ads, vague platitudes — content that
    # would survive ICP scoring but produce a weak comment.
    run_quality_gate = bool(settings.discovery_contact_unipile_run_post_quality)

    for seed in seeds:
        linkedin_url = (seed.get("linkedin_url") or "").strip()
        if not linkedin_url:
            # No URL → can't address Unipile by slug. Skip silently; the
            # operator can edit the contact and add their LinkedIn URL.
            log.info(
                "discovery: contact_seeds_unipile skipping %s — no linkedin_url",
                seed.get("extracted_name") or seed.get("_id"),
            )
            continue

        name = seed.get("extracted_name") or ""
        title = seed.get("extracted_title") or ""
        company = seed.get("extracted_company") or ""

        try:
            posts = get_user_posts(
                account_id=account_id,
                public_identifier_or_url=linkedin_url,
                limit=posts_per_contact,
            )
        except (UnipileError, UnipileNotConfigured) as err:
            log.warning(
                "discovery: contact_seeds_unipile %r failed: %s",
                linkedin_url,
                err,
            )
            continue

        for post in posts:
            # Pass through every post URL the contact actually has — no
            # cross-contact / cross-run dedup. The operator curated this
            # list; if two contacts share a post we want to see it twice.
            if not post.url:
                continue

            # Recency: drop posts older than the cutoff. Posts with no
            # parseable date are KEPT (Unipile occasionally omits dates;
            # don't penalize missing data).
            if cutoff is not None and post.published_at is not None:
                published = post.published_at
                if hasattr(published, "tzinfo") and published.tzinfo is None:
                    from datetime import timezone as _tz
                    published = published.replace(tzinfo=_tz.utc)
                if published < cutoff:
                    too_old_dropped += 1
                    continue

            # Post-quality gate (cheap LLM): drops recruiting ads, event
            # announcements, vague platitudes, engagement bait. Skips when
            # post text is empty (LLM has nothing to score). Captures the
            # real `qualifying_signal` so it lands in gate_results below.
            quality_verdict = None
            if run_quality_gate and (post.text or "").strip():
                try:
                    quality_verdict = post_quality.evaluate(post_text=post.text or "")
                except Exception as err:
                    log.warning(
                        "discovery: contact_seeds_unipile post_quality failed for %r: %s — keeping post",
                        post.url,
                        err,
                    )
                    quality_verdict = None
                if quality_verdict and quality_verdict.drop:
                    quality_dropped += 1
                    log.info(
                        "discovery: contact_seeds_unipile drop %r — post_quality: %s",
                        post.url,
                        quality_verdict.reason,
                    )
                    continue

            doc = _candidate_doc(
                operator_id=operator_id,
                cofounder_id=cofounder_id,
                slate_run_id=slate_run_id,
                post_url=post.url,
                post_id=post.id,
                # Prefer the post-level author payload (Unipile gives it back
                # explicitly) but fall back to the seed's metadata so we never
                # ship blanks to the drafter.
                author_name=post.author_name or name or None,
                author_title=post.author_title or title or None,
                author_company=post.author_company or company or None,
                author_linkedin_url=post.author_profile_url or linkedin_url,
                post_text=post.text or "",
                post_published_at=post.published_at,
                source="contact_unipile",
                source_keyword=name or linkedin_url,
                source_classification="A",
                source_channel="contact_direct",
            )
            # Skip the rest of the funnel: verification + non_buyer +
            # analyst + ICP scoring. The allocator reads
            # status="gate_passed" so we land right on its doorstep.
            doc["status"] = "gate_passed"
            doc["bypass_gates"] = True
            # Carry the REAL post_quality verdict if we ran it; fall back to
            # the synthetic placeholder when the gate was off / errored.
            if quality_verdict is not None:
                pq_payload = {
                    **quality_verdict.model_dump(),
                    "synthetic": False,
                }
            else:
                pq_payload = {
                    "drop": False,
                    "reason": "operator_curated_contact",
                    "qualifying_signal": "operator_curated_contact",
                    "synthetic": True,
                }
            doc["gate_results"] = {
                # Synthetic top-tier ICP score so the allocator's
                # score-desc sort floats contacts above any keyword
                # survivors competing for the same cofounder bucket.
                "icp": {"score_0_10": 10, "total": 100, "synthetic": True},
                "non_buyer": {"verdict": "buyer", "synthetic": True},
                "post_quality": pq_payload,
                "analyst": {"verdict": "not_analyst", "synthetic": True},
            }
            db.candidates.insert_one(doc)
            _discovery_record_insert(db, doc)
            inserted += 1

    log.info(
        "discovery: contact_seeds_unipile cofounder=%s contacts=%d inserted=%d too_old_dropped=%d quality_dropped=%d (most gates bypassed, post_quality=%s, max_age=%dd)",
        cofounder_id,
        len(seeds),
        inserted,
        too_old_dropped,
        quality_dropped,
        "on" if run_quality_gate else "off",
        max_age_days,
    )
    return inserted


def _mark_seeds_used(db: Database, seed_ids: list[ObjectId]) -> None:
    if not seed_ids:
        return
    db.discovery_seeds.update_many(
        {"_id": {"$in": seed_ids}},
        {"$set": {"status": "used", "updated_at": utcnow()}},
    )
