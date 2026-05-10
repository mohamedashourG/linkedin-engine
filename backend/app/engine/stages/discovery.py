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
     set. Inline-enriched candidates skip ``profile_resolve`` so Crustdata
     / PDL credits aren't spent on data we already have. Reference:
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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.engine import keyword_history
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


def _compose_discovery_query(base_kw: str, operator: dict[str, Any]) -> str:
    """Append geography + seniority so LinkedIn search stays ICP-local."""
    parts = [base_kw.strip()]
    geos = _operator_geo_terms(operator)
    if geos:
        parts.append(" ".join(geos[:5]))
    sen = _seniority_hints_from_titles(operator)
    if sen:
        parts.append(sen)
    q = " ".join(p for p in parts if p).strip()
    return q[:480]


def _rotate(values: list[str], n: int) -> list[str]:
    """Pick up to N keywords with mild shuffling so successive runs vary."""
    if not values:
        return []
    pool = list(values)
    random.shuffle(pool)
    return pool[: max(0, n)]


def _is_exhausted(db: Database, operator_id: ObjectId, author_url: str) -> bool:
    # DEMO OVERRIDE: exhaustion ledger check disabled so previously-engaged
    # authors resurface as candidates. RESTORE BY REMOVING this early return.
    return False
    # Original:
    # if not author_url:
    #     return False
    # cutoff = utcnow() - timedelta(days=EXHAUSTION_LOOKBACK_DAYS)
    # doc = db.exhaustion_ledger.find_one(
    #     {"operator_id": operator_id, "linkedin_url": author_url}
    # )
    # return bool(doc and doc.get("last_engaged_at") and doc["last_engaged_at"] >= cutoff)


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
    source_channel: str = "",
) -> dict[str, Any]:
    """`source` stays vendor-specific (apidirect_kw / exa_kw / unipile_*) for
    debugging. `source_channel` is the audit-aligned tag (RULE 15:
    keyword_topical, RULE 15-EXT: keyword_title_industry, RULE 24:
    title_search) used by allocator/drafter routing."""
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
    inline_rubric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a candidate doc from a Unipile search post.

    When ``enriched_profile`` is supplied (output of ``_get_cached_author_profile``),
    its headline / company / location override what the search payload returned —
    those fields are much more reliable from the `/users/{slug}` endpoint than
    from the search snippet. ``inline_rubric`` carries the author/post scores
    that qualified the candidate so downstream stages (and the UI) can show
    the reasoning.

    When ``enriched_profile`` is set we also stamp ``enriched_inline=True``
    so ``profile_resolve`` can skip the candidate (saves Crustdata credits)."""
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
    if inline_rubric:
        doc["unipile_rubric"] = inline_rubric
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
    blob = (headline + " | " + company).strip(" |")

    title_pts = 5 if any(_rubric_substring_match(headline, t) for t in titles) else 0
    industry_pts = 3 if any(_rubric_substring_match(blob, i) for i in industries) else 0
    geo_pts = 2 if any(_rubric_substring_match(location, g) for g in geos) else 0

    return {
        "title": title_pts,
        "industry": industry_pts,
        "geo": geo_pts,
        "total": title_pts + industry_pts + geo_pts,
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


def _get_cached_unipile_author_profile(
    db: Database,
    *,
    operator_id: ObjectId,
    provider_id: str,
    account_id: str,
    ttl_days: int,
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
        "fetched_at": now,
    }
    db.unipile_author_cache.update_one(
        {"operator_id": operator_id, "provider_id": provider_id},
        {"$set": doc},
        upsert=True,
    )
    return doc, True


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
) -> dict[str, Any]:
    author_name = post.author
    author_linkedin_url = None
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
    return _candidate_doc(
        operator_id=operator_id,
        cofounder_id=cofounder_id,
        slate_run_id=slate_run_id,
        post_url=post.url,
        post_id=post_id,
        author_name=author_name,
        author_title=None,
        author_company=None,
        author_linkedin_url=author_linkedin_url,
        post_text=post_text,
        post_published_at=post_published_at,
        source="apidirect_kw",
        source_keyword=source_keyword,
        source_classification=source_classification,
        source_channel=source_channel,
    )


def _doc_from_exa(
    post: ExaPost,
    *,
    operator_id: ObjectId,
    cofounder_id: ObjectId,
    slate_run_id: ObjectId,
    source_keyword: str,
    source_classification: str,
    source_channel: str = "keyword_topical",
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
        source_channel=source_channel,
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
    for row in cursor:
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
    fresh_t1 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_1
    )
    fresh_t2 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_2
    )
    plan: list[tuple[str, str]] = []
    for kw in _rotate(fresh_t1, DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(fresh_t2, DISCOVERY_TIER_2_PER_RUN):
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
    for query, classification in plan:
        vendor_kw = _compose_discovery_query(query, operator)[:400].strip()
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
        keyword_history.mark_used(
            db, operator_id=operator_id, source_channel="keyword_topical", query=query
        )
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
            db.candidates.insert_one(
                _doc_from_crustdata_screener(
                    raw,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source_keyword=query,
                    source_classification=classification,
                )
            )
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
    extracted = operator.get("product_extracted") or {}
    keywords = (extracted.get("suggested_keywords") or {}) if extracted else {}
    tier_1 = keywords.get("tier_1") or []
    tier_2 = keywords.get("tier_2") or []
    tier_3 = keywords.get("tier_3") or []

    # RULE 15-EXT — title-plus-industry pool. Source priority:
    #   1. configs/<client>/keyword_pools.json:title_industry  (curated per-tenant)
    #   2. operator.product_extracted.title_industry            (back-compat)
    # Empty pool is fine — the unipile path skips the title-industry plan.
    cfg = client_config.for_operator(operator)
    title_industry: list[str] = (
        cfg.keyword_pools.get("title_industry")
        or extracted.get("title_industry")
        or []
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
    # DEMO OVERRIDE: ALL three cross-run dedup layers disabled so every run
    # surfaces every match — useful for "show the raw data" demos. Restore
    # by uncommenting the original lines and removing the empty-set fallbacks.
    #
    # Original (90-day post-URL dedup pre-seed):
    # seen_urls: set[str] = _seen_post_urls(db, operator_id)
    # Original (90-day already-shipped-author dedup pre-seed):
    # seen_authors_shipped: set[str] = _seen_author_urls(db, operator_id)
    seen_urls: set[str] = set()
    seen_authors_shipped: set[str] = set()
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
        "discovery: source order = %s%s%s%s%s",
        "contacts_unipile → " if contacts_on else "",
        "unipile (PRIMARY) → " if unipile_on else "(unipile off) → ",
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
        contacts_inserted = 0

        # ── SOURCE 1 (PRIMARY): Unipile keyword + seed-author search ──────
        # Promoted to first position — Unipile gives the best per-candidate
        # signal (free `/users/{slug}` profile enrichment + dual-path rubric
        # qualify inside `_run_unipile`) so it should run before the more
        # expensive / less-signal-rich keyword sources downstream.
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

        # ── SOURCE 2: Crustdata inbox + optional realtime screener ───────────
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

        # ── SOURCE 3: apidirect synchronous keyword search ─────────────────
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
            )
            inserted += apidirect_inserted

        # ── SOURCE 4: Exa LinkedIn-scoped search (high numResults per call) ──
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
            )
            inserted += exa_inserted

        # ── SOURCE 4b: RULE 24 title-search PEOPLE channel via Unipile.
        # Runs only when unipile is on AND the title_search flag is set AND
        # we have a connected account_id for the cofounder.
        title_search_inserted = 0
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

        # ── SOURCE 5: Contact seeds (Unipile direct, gates bypassed) ─────
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
) -> int:
    """Synchronous keyword search via apidirect. Trips its own circuit on 402;
    we just stop calling it for the rest of the run.

    RULE 15: filter the tier pools through the 14-day no-repeat ledger
    before rotating, then mark each query used after we've called the
    vendor (idempotent — same query in the same day/run is fine)."""
    fresh_t1 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_1
    )
    fresh_t2 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_2
    )
    plan: list[tuple[str, str]] = []
    for kw in _rotate(fresh_t1, DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(fresh_t2, DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))

    if not plan:
        log.warning(
            "│  [apidirect]   plan EMPTY — all kws in 14d ledger (tier_1=%d, tier_2=%d, "
            "fresh_t1=%d, fresh_t2=%d). Lower KEYWORD_HISTORY_LOOKBACK_DAYS or expand the pool.",
            len(tier_1), len(tier_2), len(fresh_t1), len(fresh_t2),
        )
        return 0

    pages = max(1, min(int(settings.discovery_apidirect_max_pages), 5))
    inserted = 0
    for query, classification in plan:
        vendor_q = _compose_discovery_query(query, operator)
        try:
            posts = search_linkedin_posts_pages(vendor_q, max_pages=pages)
        except (ApiDirectQuotaExhausted, ApiDirectNotConfigured) as err:
            log.warning("discovery: apidirect halted mid-run: %s", err)
            return inserted
        except ApiDirectError as err:
            log.warning("discovery: apidirect %r failed: %s", vendor_q, err)
            continue

        keyword_history.mark_used(
            db, operator_id=operator_id, source_channel="keyword_topical", query=query
        )
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

            seen_urls.add(post.url)
            db.candidates.insert_one(
                _doc_from_apidirect(
                    post,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source_keyword=query,
                    source_classification=classification,
                    details=details,
                )
            )
            inserted += 1
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
) -> int:
    """Exa LinkedIn-scoped semantic search. One API call per keyword, up to
    ~100 results per call (configurable via EXA_RESULTS_PER_QUERY, capped at 100).
    Uses a wider keyword plan than apidirect/unipile so each run pulls enough
    raw posts to survive downstream gates. Trips the circuit on 401/402/429.

    RULE 15 14-day no-repeat ledger filters all three tier pools."""
    fresh_t1 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_1
    )
    fresh_t2 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_2
    )
    fresh_t3 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_3
    )
    plan: list[tuple[str, str]] = []
    for kw in _rotate(fresh_t1, EXA_DISCOVERY_TIER_1_PER_RUN):
        plan.append((kw, "A"))
    for kw in _rotate(fresh_t2, EXA_DISCOVERY_TIER_2_PER_RUN):
        plan.append((kw, "B"))
    for kw in _rotate(fresh_t3, EXA_DISCOVERY_TIER_3_PER_RUN):
        plan.append((kw, "B"))

    if not plan:
        log.warning(
            "│  [exa]         plan EMPTY — all kws in ledger (tier_1=%d, tier_2=%d, tier_3=%d, "
            "fresh_t1=%d, fresh_t2=%d, fresh_t3=%d)",
            len(tier_1), len(tier_2), len(tier_3), len(fresh_t1), len(fresh_t2), len(fresh_t3),
        )
        return 0

    inserted = 0
    for query, classification in plan:
        vendor_q = _compose_discovery_query(query, operator)
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

        keyword_history.mark_used(
            db, operator_id=operator_id, source_channel="keyword_topical", query=query
        )
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
    # RULE 15: filter the topical pools through the 14-day no-repeat ledger.
    fresh_t1 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_1
    )
    fresh_t2 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_2
    )
    fresh_t3 = keyword_history.filter_unused(
        db, operator_id=operator_id, source_channel="keyword_topical", queries=tier_3
    )
    # RULE 15-EXT: same 14-day rule on a separate channel so a query that
    # appears in BOTH pools (rare but possible, e.g. "VP Sales biotech")
    # tracks per-channel.
    fresh_ti = keyword_history.filter_unused(
        db,
        operator_id=operator_id,
        source_channel="keyword_title_industry",
        queries=title_industry,
    )
    plan: list[tuple[str, str, str, str]] = []
    for kw in _rotate(fresh_t1, DISCOVERY_TIER_1_PER_RUN):
        plan.append(("kw", kw, "tier_1_kw", "A"))
    for kw in _rotate(fresh_t2, DISCOVERY_TIER_2_PER_RUN):
        plan.append(("kw", kw, "tier_2_kw", "B"))
    for kw in _rotate(fresh_t3, DISCOVERY_TIER_3_PER_RUN):
        plan.append(("kw", kw, "tier_3_kw", "B"))
    for kw in _rotate(fresh_ti, DISCOVERY_TITLE_INDUSTRY_PER_RUN):
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
            "│  [unipile]     plan EMPTY — all kws in ledger (kw_topical: t1=%d/%d t2=%d/%d t3=%d/%d, "
            "title_industry: %d/%d, seeds=%d)",
            len(fresh_t1), len(tier_1),
            len(fresh_t2), len(tier_2),
            len(fresh_t3), len(tier_3),
            len(fresh_ti), len(title_industry),
            len(seeds),
        )
        return 0

    post_location_ids = _unipile_post_location_ids_for_operator(account_id, operator)
    inserted = 0
    # Cap on `/users/{slug}` profile fetches per cofounder run — protects
    # the LinkedIn account from a quota spike on a high-yield keyword pass.
    fetch_budget_remaining = settings.discovery_unipile_max_profile_fetches_per_run
    cache_ttl = settings.discovery_unipile_author_cache_ttl_days
    use_inline_rubric = settings.discovery_unipile_inline_rubric_enabled
    rubric_drops = {"no_profile": 0, "geo": 0, "rubric": 0}
    for kind, payload, source, classification in plan:
        assert kind == "kw"
        try:
            q = (
                _compose_discovery_query(payload, operator)
                if source
                in ("tier_1_kw", "tier_2_kw", "tier_3_kw", "title_industry_kw")
                else payload
            )
            posts = search_posts_pages(
                account_id=account_id,
                query=q,
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
                location_ids=post_location_ids or None,
            )
        except UnipileNotConfigured as err:
            log.warning("discovery: unipile unconfigured, halting: %s", err)
            return inserted
        except UnipileError as err:
            log.warning("discovery: unipile kw failed for %r: %s", payload, err)
            continue

        if source in ("tier_1_kw", "tier_2_kw", "tier_3_kw"):
            keyword_history.mark_used(
                db,
                operator_id=operator_id,
                source_channel="keyword_topical",
                query=payload,
            )
        elif source == "title_industry_kw":
            keyword_history.mark_used(
                db,
                operator_id=operator_id,
                source_channel="keyword_title_industry",
                query=payload,
            )
        # Tag content-search hits with source_channel for downstream routing.
        if source in ("tier_1_kw", "tier_2_kw", "tier_3_kw"):
            post_source_channel = "keyword_topical"
        elif source == "title_industry_kw":
            post_source_channel = "keyword_title_industry"
        else:
            post_source_channel = ""

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

            enriched_profile: dict[str, Any] | None = None
            inline_rubric: dict[str, Any] | None = None

            if use_inline_rubric:
                # Step 1: enrich author via cached /users/{slug} (free Unipile
                # endpoint). Cache hits don't count against the per-run fetch
                # budget; misses do.
                provider_id = post.author_provider_id or ""
                if provider_id:
                    cached_or_fresh, was_fetched = _get_cached_unipile_author_profile(
                        db,
                        operator_id=operator_id,
                        provider_id=provider_id,
                        account_id=account_id,
                        ttl_days=cache_ttl,
                    )
                    if was_fetched:
                        fetch_budget_remaining -= 1
                    enriched_profile = cached_or_fresh
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
                inline_rubric = {
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
                        reason = "geo_not_in_author_location"
                    elif not enriched_profile and provider_id:
                        # Had a provider_id but enrichment failed/budget
                        # exhausted → can't verify geo → drop.
                        rubric_drops["no_profile"] = rubric_drops.get("no_profile", 0) + 1
                        reason = "no_profile (fetch failed or budget exhausted)"
                    else:
                        rubric_drops["rubric"] += 1
                        reason = (
                            f"rubric T={author_score['title']} I={author_score['industry']} "
                            f"G={author_score['geo']} post={post_score} → no path"
                        )
                    log.info(
                        "│  [DROP/unipile] %s  ←  %s",
                        (post.url or "<no-url>")[:90],
                        reason,
                    )
                    continue
                if fetch_budget_remaining <= 0 and use_inline_rubric:
                    log.info(
                        "│  [unipile]     profile-fetch budget exhausted "
                        "(%d/run); remaining posts use search-payload data only",
                        settings.discovery_unipile_max_profile_fetches_per_run,
                    )
                    # Don't stop the loop — just stop enriching. Subsequent
                    # posts will fall through to the search-payload path
                    # below (author score will be 0 for title/industry, geo
                    # may still match if search included location info).
                    use_inline_rubric = False

            seen_urls.add(post.url)
            db.candidates.insert_one(
                _doc_from_unipile(
                    post,
                    operator_id=operator_id,
                    cofounder_id=cofounder_id,
                    slate_run_id=slate_run_id,
                    source=source,
                    source_keyword=payload,
                    source_classification=classification,
                    source_channel=post_source_channel,
                    enriched_profile=enriched_profile,
                    inline_rubric=inline_rubric,
                )
            )
            inserted += 1

    if settings.discovery_unipile_inline_rubric_enabled:
        log.info(
            "│  [unipile-rubric] dropped: geo=%d no_profile=%d rubric=%d",
            rubric_drops.get("geo", 0),
            rubric_drops.get("no_profile", 0),
            rubric_drops.get("rubric", 0),
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

    fresh_queries = keyword_history.filter_unused(
        db,
        operator_id=operator_id,
        source_channel="title_search",
        queries=title_industry,
    )
    plan = _rotate(fresh_queries, DISCOVERY_TITLE_SEARCH_QUERIES_PER_RUN)

    inserted = 0
    for query in plan:
        vendor_q = _compose_discovery_query(query, operator)
        # Step 1 — people search (US, 2nd-degree).
        try:
            people = search_people(
                account_id=account_id,
                query=vendor_q,
                limit=DISCOVERY_TITLE_SEARCH_PEOPLE_PER_QUERY,
            )
        except UnipileNotConfigured as err:
            log.warning("discovery: title_search unipile unconfigured: %s", err)
            return inserted
        except UnipileError as err:
            log.warning("discovery: title_search %r failed: %s", query, err)
            continue

        keyword_history.mark_used(
            db,
            operator_id=operator_id,
            source_channel="title_search",
            query=query,
        )

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

            for post in posts:
                if not post.url or post.url in seen_urls:
                    continue
                if settings.discovery_unipile_skip_company_posts and post.author_is_company:
                    continue
                seen_urls.add(post.url)
                db.candidates.insert_one(
                    _doc_from_unipile(
                        post,
                        operator_id=operator_id,
                        cofounder_id=cofounder_id,
                        slate_run_id=slate_run_id,
                        source="unipile_people",
                        source_keyword=query,
                        # 2nd-degree US-filtered title hits are tier-1 ICP density.
                        source_classification="A",
                        source_channel="title_search",
                    )
                )
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

        vendor_query = _compose_discovery_query(query, operator)

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
                    db.candidates.insert_one(
                        _candidate_doc(
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
                    )
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
                            post_text=body or (post.snippet or post.title or ""),
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
            # profile_resolve + analyst + ICP scoring. The allocator reads
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
