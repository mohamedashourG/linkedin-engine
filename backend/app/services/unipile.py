"""
Sync Unipile client.

Endpoint shapes mirror gtm-engine's working outreach client (connect / message)
plus the LinkedIn post + comment endpoints documented in Unipile's API. The
post/comment paths aren't exercised by gtm-engine yet, so they're written
defensively — if Unipile's API surface for these calls differs in your tenant,
the single chokepoint to adjust is here.

Base URL pattern: https://{subdomain}.unipile.com:{port}/api/v1
Auth: X-API-KEY header
account_id: required on every authenticated call (per-LinkedIn-account)

Mock mode (UNIPILE_MOCK=true): canned data so the engine can be exercised
without burning real Unipile calls during dev.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
# Transient 429 from Unipile/LinkedIn — brief exponential backoff then re-raise via _check_resp.
_UNIPILE_429_MAX_ATTEMPTS = 3
_UNIPILE_429_BACKOFF_BASE_S = 0.75


class UnipileNotConfigured(RuntimeError):
    pass


class UnipileError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnipileAccount:
    id: str
    name: str
    profile_url: str | None
    avatar_url: str | None
    account_type: str | None


@dataclass(frozen=True)
class UnipileComment:
    comment_id: str
    text: str
    author_name: str | None
    author_provider_id: str | None
    author_public_identifier: str | None
    author_profile_url: str | None
    published_at: datetime | None
    reaction_count: int = 0
    reply_count: int = 0


@dataclass(frozen=True)
class UnipilePost:
    id: str
    url: str
    text: str
    author_name: str | None
    author_title: str | None
    author_company: str | None
    author_profile_url: str | None
    published_at: datetime | None
    author_is_company: bool = False
    # Provider URN (e.g. ``ACoAA...``). Used as the path component for
    # ``GET /users/{provider_id}`` when we need to enrich the author's
    # headline / location / company beyond what the search payload returns.
    # None when the search response omitted the author id entirely.
    author_provider_id: str | None = None
    reaction_counter: int = 0
    comment_counter: int = 0
    repost_counter: int = 0
    is_repost: bool = False
    can_post_comments: bool = True
    has_poll: bool = False
    poll_total_votes: int = 0


@dataclass(frozen=True)
class UnipileCommentPostResult:
    comment_id: str
    posted_at: datetime | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class UnipilePerson:
    """A LinkedIn profile returned from a people-search call (RULE 24).
    Distinct from UnipilePost because RULE 24 walks the person's recent
    activity AFTER the people-search returns the roster."""

    name: str
    public_identifier: str
    profile_url: str
    title: str | None
    company: str | None
    location: str | None
    network_distance: str | None  # "1", "2", "3" — LinkedIn's degree label


# RULE 24 — LinkedIn's geoUrn for the United States. Matches the audit's
# people-search URL: linkedin.com/search/results/people/?geoUrn=%5B%22103644278%22%5D
LINKEDIN_GEO_URN_US = "103644278"


def _api_root() -> str:
    """The base Unipile tenant URL (no /api/v1 suffix). Used as `api_url` in
    the hosted-auth-link payload so Unipile knows which tenant to bind the
    newly-connected account to."""
    sub = settings.unipile_subdomain
    port = settings.unipile_port
    if not sub:
        raise UnipileNotConfigured(
            "UNIPILE_SUBDOMAIN is not set. Add it to .env to enable Unipile."
        )
    if port and port != 443:
        return f"https://{sub}.unipile.com:{port}"
    return f"https://{sub}.unipile.com"


def _base_url() -> str:
    return f"{_api_root()}/api/v1"


def _client() -> httpx.Client:
    if not settings.unipile_api_key:
        raise UnipileNotConfigured("UNIPILE_API_KEY is not set.")
    return httpx.Client(
        base_url=_base_url(),
        headers={"X-API-KEY": settings.unipile_api_key, "accept": "application/json"},
        timeout=_TIMEOUT,
    )


def _request_with_429_retry(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Run one HTTP call; on 429 retry with exponential backoff (in-process)."""
    m = method.upper()
    last: httpx.Response | None = None
    for attempt in range(_UNIPILE_429_MAX_ATTEMPTS):
        last = client.request(m, path, **kwargs)
        if last.status_code != 429:
            return last
        if attempt < _UNIPILE_429_MAX_ATTEMPTS - 1:
            delay = _UNIPILE_429_BACKOFF_BASE_S * (2**attempt)
            log.warning(
                "unipile 429 %s %s attempt %d/%d, sleeping %.2fs",
                m,
                path[:120],
                attempt + 1,
                _UNIPILE_429_MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
    assert last is not None
    return last


def _rule24_default_location_ids() -> tuple[str, ...]:
    """Geo filter for RULE 24 people search; env override or US default."""
    raw = (settings.unipile_rule24_location_ids or "").strip()
    if not raw:
        return (LINKEDIN_GEO_URN_US,)
    ids = [x.strip() for x in raw.split(",") if x.strip()][:10]
    return tuple(ids) if ids else (LINKEDIN_GEO_URN_US,)


def _check_resp(resp: httpx.Response, context: str) -> dict[str, Any]:
    if resp.status_code == 401:
        raise UnipileError(f"unipile 401 ({context}): {resp.text[:300]}")
    if resp.status_code == 402:
        raise UnipileError(f"unipile 402 quota ({context}): {resp.text[:300]}")
    if resp.status_code == 429:
        raise UnipileError(f"unipile 429 rate-limit ({context})")
    if resp.status_code >= 400:
        raise UnipileError(
            f"unipile {resp.status_code} ({context}): {resp.text[:300]}"
        )
    try:
        return resp.json() or {}
    except ValueError:
        return {}


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- accounts

def list_accounts() -> list[UnipileAccount]:
    """List connected LinkedIn (and other) accounts on this Unipile tenant."""
    if settings.unipile_mock:
        return [
            UnipileAccount(
                id="mock-account-1",
                name="Alex Cofounder (mock LinkedIn)",
                profile_url="https://linkedin.com/in/alexdemo",
                avatar_url=None,
                account_type="LINKEDIN",
            )
        ]
    with _client() as client:
        resp = _request_with_429_retry(client, "GET", "/accounts", params={"limit": 50})
    payload = _check_resp(resp, "list_accounts")
    items = payload.get("items") or []
    out: list[UnipileAccount] = []
    for raw in items:
        out.append(
            UnipileAccount(
                id=str(raw.get("id") or ""),
                name=raw.get("name") or "(unnamed)",
                profile_url=raw.get("profile_url") or (raw.get("params") or {}).get("profile_url"),
                avatar_url=raw.get("avatar_url"),
                account_type=raw.get("type") or raw.get("account_type"),
            )
        )
    return out


# ---------------------------------------------------------------- post search / fetch


def _infer_author_is_company(author: dict[str, Any], profile_url: str | None) -> bool:
    """True when the post actor is a LinkedIn company, not a person.

    Mirrors common Unipile/LinkedIn shapes (`is_company`, `type`, `/company/` URLs)
    so keyword sweeps can drop brand pages like a lightweight hybrid pipeline."""
    if author.get("is_company") is True:
        return True
    t = str(author.get("type") or author.get("profile_type") or "").upper()
    if "COMPANY" in t and "PERSON" not in t:
        return True
    url = (profile_url or "").lower()
    if "/company/" in url:
        return True
    return False


def _safe_int(v: Any) -> int:
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, float):
        return max(0, int(v))
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return 0


def _parse_unipile_post(raw: dict[str, Any]) -> UnipilePost | None:
    """
    Unipile's LinkedIn post payload nests the author under different keys
    across endpoints (author / poster / user / actor). This unifies them.
    """
    if not raw:
        return None
    author = (
        raw.get("author")
        or raw.get("poster")
        or raw.get("user")
        or raw.get("actor")
        or {}
    )
    text = (
        raw.get("text")
        or raw.get("body")
        or raw.get("content")
        or raw.get("commentary")
        or ""
    )
    url = (
        raw.get("share_url")
        or raw.get("url")
        or raw.get("post_url")
        or raw.get("link")
        or ""
    )
    if not url and not raw.get("id"):
        return None
    prof_url = (
        author.get("public_profile_url")
        or author.get("profile_url")
        or author.get("url")
    )
    stats = raw.get("social_stats") if isinstance(raw.get("social_stats"), dict) else {}
    reaction_counter = (
        _safe_int(raw.get("reaction_counter"))
        or _safe_int(raw.get("num_reactions"))
        or _safe_int(raw.get("reactions_count"))
        or _safe_int(stats.get("reaction_counter"))
        or _safe_int(stats.get("reactions_count"))
    )
    comment_counter = (
        _safe_int(raw.get("comment_counter"))
        or _safe_int(raw.get("num_comments"))
        or _safe_int(raw.get("comments_count"))
        or _safe_int(stats.get("comment_counter"))
        or _safe_int(stats.get("comments_count"))
    )
    repost_counter = (
        _safe_int(raw.get("repost_counter"))
        or _safe_int(raw.get("num_shares"))
        or _safe_int(raw.get("reposts_count"))
        or _safe_int(stats.get("repost_counter"))
        or _safe_int(stats.get("reposts_count"))
    )
    ir = raw.get("is_repost")
    if ir is None:
        ir = raw.get("reshared")
    is_repost = bool(ir) if isinstance(ir, (bool, int)) else False
    perms = raw.get("permissions")
    can_post_comments = True
    if isinstance(perms, dict) and "can_post_comments" in perms:
        can_post_comments = bool(perms.get("can_post_comments"))
    poll = raw.get("poll")
    has_poll = isinstance(poll, dict) and bool(poll)
    poll_total_votes = _safe_int((poll or {}).get("total_votes")) if has_poll else 0
    return UnipilePost(
        id=str(raw.get("id") or raw.get("social_id") or raw.get("urn") or ""),
        url=url or "",
        text=text or "",
        author_name=author.get("name")
        or " ".join(filter(None, [author.get("first_name"), author.get("last_name")])).strip()
        or None,
        author_title=author.get("headline") or author.get("title") or author.get("occupation"),
        author_company=author.get("company") or author.get("company_name"),
        author_profile_url=str(prof_url).strip() if prof_url else None,
        # Unipile returns `date` as a relative string like "2d" / "3h" — not
        # ISO-parseable. The actual ISO timestamp lives in `parsed_datetime`.
        # Try the ISO fields first; fall back to `date` only for older
        # responses that did inline ISO there.
        published_at=_parse_iso(
            raw.get("parsed_datetime")
            or raw.get("created_at")
            or raw.get("published_at")
            or raw.get("date")
        ),
        author_is_company=_infer_author_is_company(author, str(prof_url) if prof_url else None),
        author_provider_id=(
            str(author.get("id") or author.get("provider_id") or author.get("urn") or "").strip()
            or None
        ),
        reaction_counter=reaction_counter,
        comment_counter=comment_counter,
        repost_counter=repost_counter,
        is_repost=is_repost,
        can_post_comments=can_post_comments,
        has_poll=has_poll,
        poll_total_votes=poll_total_votes,
    )


_VALID_DATE_POSTED = {"past_day", "past_week", "past_month"}
_VALID_SORT_BY = {"date", "relevance"}


def _post_search_body(
    *,
    query: str | None = None,
    sort_by: str | None = "date",
    date_posted: str | None = None,
    content_type: str | list[str] | None = None,
    author_keywords: str | None = None,
    location_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Shared body builder for the two POST search variants (kw + url).
    `query` is None for the URL-paste variant which has no keywords field."""
    body: dict[str, Any] = {"api": "classic", "category": "posts"}
    if query is not None:
        body["keywords"] = query[:300]
    if sort_by:
        sb = sort_by.lower()
        if sb in _VALID_SORT_BY:
            body["sort_by"] = sb
    if date_posted:
        dp = date_posted.lower()
        if dp in _VALID_DATE_POSTED:
            body["date_posted"] = dp
    if content_type:
        if isinstance(content_type, str):
            body["content_type"] = content_type
        else:
            body["content_type"] = list(content_type)
    if author_keywords and author_keywords.strip():
        body["author"] = {"keywords": author_keywords.strip()[:200]}
    if location_ids:
        loc = [str(x).strip() for x in location_ids if str(x).strip()]
        if loc:
            body["location"] = loc[:10]
    return body


def _search_posts_call(
    body: dict[str, Any],
    *,
    account_id: str,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[UnipilePost], str | None]:
    """One POST /linkedin/search call. Returns parsed posts and the next
    cursor (None when the result set is exhausted).

    **First page:** omit ``cursor`` — sending e.g. ``cursor=1`` triggers
    Unipile ``invalid_cursor``. **Later pages:** pass the opaque ``cursor``
    string from the previous JSON response."""
    params: dict[str, Any] = {"account_id": account_id, "limit": limit}
    if cursor is not None and str(cursor).strip():
        params["cursor"] = cursor
    with _client() as client:
        resp = _request_with_429_retry(
            client, "POST", "/linkedin/search", params=params, json=body
        )
    payload = _check_resp(resp, "search_posts")
    items = payload.get("items") or payload.get("results") or []
    out: list[UnipilePost] = []
    for raw in items:
        post = _parse_unipile_post(raw)
        if post:
            out.append(post)
    return out, payload.get("cursor")


def search_posts(
    *,
    account_id: str,
    query: str,
    limit: int = 50,
    sort_by: str | None = "relevance",
    date_posted: str | None = None,
    content_type: str | list[str] | None = None,
    author_keywords: str | None = None,
    location_ids: list[str] | None = None,
) -> list[UnipilePost]:
    """LinkedIn keyword post search via Unipile (single page).

    POST /linkedin/search with {api: 'classic', category: 'posts', ...}.
    Sales Navigator does NOT have a parallel posts-by-keyword path — its
    strength is people/account targeting. Per Unipile's docs only the
    classic API exposes the post-specific filters below.

    Filters (all optional, all server-side):
      sort_by         "date" (newest first) | "relevance" | None
      date_posted     "past_day" | "past_week" | "past_month" | None
      content_type    e.g. "documents", or ["images", "videos"]
      author_keywords boolean string against the author's headline,
                      e.g. "CEO OR VP OR Founder"
      location_ids    optional LinkedIn geo id strings (same as people search)

    For multi-page walks use `search_posts_pages` instead.
    """
    if settings.unipile_mock or not query.strip():
        return []
    body = _post_search_body(
        query=query,
        sort_by=sort_by,
        date_posted=date_posted,
        content_type=content_type,
        author_keywords=author_keywords,
        location_ids=location_ids,
    )
    posts, _next_cursor = _search_posts_call(
        body, account_id=account_id, limit=limit, cursor=None
    )
    return posts


def search_posts_pages(
    *,
    account_id: str,
    query: str,
    max_pages: int = 3,
    per_page: int = 50,
    sort_by: str | None = "relevance",
    date_posted: str | None = None,
    content_type: str | list[str] | None = None,
    author_keywords: str | None = None,
    location_ids: list[str] | None = None,
) -> list[UnipilePost]:
    """Cursor-walking POST /linkedin/search (classic, category=posts).

    First request per date window omits ``cursor``; follow-up pages use the
    ``cursor`` value returned in the previous response body.

    When ``date_posted`` is None or empty, runs three passes in order:
    **past_day** → **past_week** → **past_month**, merging results with
    dedupe by URL/id. When ``date_posted`` is set, only that window is used.

    ``max_pages`` applies per date window (each window is clamped to [1, 10]
    pages).

    ``content_type`` defaults to ``None`` (no server-side content-type filter),
    matching ``search_posts``. Discovery passes e.g. ``documents`` via settings.

    ``location_ids`` are passed as ``body["location"]`` (up to 10 ids) when set.
    """
    if settings.unipile_mock or not query.strip():
        return []
    cap = max(1, min(int(max_pages), 10))
    per_page = max(1, min(int(per_page), 50))

    if date_posted and str(date_posted).strip():
        windows = [date_posted.strip()]
    else:
        windows = ["past_day", "past_week", "past_month"]

    seen: set[str] = set()
    merged: list[UnipilePost] = []

    for window in windows:
        body = _post_search_body(
            query=query,
            sort_by=sort_by,
            date_posted=window,
            content_type=content_type,
            author_keywords=author_keywords,
            location_ids=location_ids,
        )
        cursor: str | None = None
        for _ in range(cap):
            page, next_cursor = _search_posts_call(
                body, account_id=account_id, limit=per_page, cursor=cursor
            )
            if not page:
                break
            for post in page:
                key = post.url or post.id
                if key and key not in seen:
                    seen.add(key)
                    merged.append(post)
            if not next_cursor:
                break
            cursor = str(next_cursor)
    log.info(
        "unipile search_posts_pages merged_posts=%d query=%.120s",
        len(merged),
        query,
    )
    return merged


def search_posts_by_url(
    *,
    account_id: str,
    search_url: str,
    max_pages: int = 3,
    per_page: int = 20,
) -> list[UnipilePost]:
    """URL-paste mode — replay a saved LinkedIn search by passing the full
    URL Unipile parses out the filters server-side, so this is the easiest
    way to wire up an exact search you've already crafted in the LinkedIn
    UI (Classic, Sales Nav, or Recruiter posts/people/companies — the URL
    determines the api+category).

    POST /linkedin/search with {"url": ...}. Walks cursors up to
    `max_pages` pages, deduped."""
    if settings.unipile_mock or not search_url.strip():
        return []
    cap = max(1, min(int(max_pages), 10))
    body = {"url": search_url.strip()}
    seen: set[str] = set()
    merged: list[UnipilePost] = []
    cursor: str | None = None
    for _ in range(cap):
        page, next_c = _search_posts_call(
            body, account_id=account_id, limit=per_page, cursor=cursor
        )
        if not page:
            break
        for post in page:
            key = post.url or post.id
            if key and key not in seen:
                seen.add(key)
                merged.append(post)
        if not next_c:
            break
        cursor = str(next_c)
    return merged


@dataclass(frozen=True)
class UnipileSearchParameter:
    """One row from /linkedin/search/parameters — an id you can pass back
    into a search body's id-typed filters (location, industry, skill,
    company, etc.)."""
    id: str
    title: str
    type: str | None = None


def search_parameter_ids(
    *,
    account_id: str,
    type: str,
    keywords: str,
    limit: int = 20,
) -> list[UnipileSearchParameter]:
    """Resolve a free-text query into Unipile/LinkedIn parameter ids.

    GET /linkedin/search/parameters?type=...&keywords=...&account_id=...

    `type` examples: LOCATION, INDUSTRY, COMPANY, SCHOOL, FUNCTION,
    SENIORITY, SKILL, LANGUAGE, TITLE. See Unipile's "Get LinkedIn Search
    Parameters API" for the full enum.

    Use the returned `id` values in the corresponding search body field
    (e.g., `location: ["102448103"]` for Los Angeles)."""
    if settings.unipile_mock or not keywords.strip() or not type.strip():
        return []
    params = {
        "account_id": account_id,
        "type": type.strip().upper(),
        "keywords": keywords.strip()[:200],
        "limit": max(1, min(int(limit), 100)),
    }
    with _client() as client:
        resp = _request_with_429_retry(
            client, "GET", "/linkedin/search/parameters", params=params
        )
    payload = _check_resp(resp, "search_parameter_ids")
    items = payload.get("items") or []
    out: list[UnipileSearchParameter] = []
    for raw in items:
        rid = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or raw.get("text") or "").strip()
        if not rid or not title:
            continue
        out.append(UnipileSearchParameter(id=rid, title=title, type=raw.get("type")))
    return out


def _normalize_location_phrase(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _location_query_variants(location_text: str) -> list[str]:
    """Short keyword strings to try against GET /linkedin/search/parameters.

    LinkedIn/Unipile match better on city or region fragments than on a full
    postal-style string alone; we still try the full string first."""
    base = _normalize_location_phrase(location_text)[:200]
    if len(base) < 2:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(fragment: str) -> None:
        frag = _normalize_location_phrase(fragment)[:200]
        if len(frag) < 2:
            return
        key = frag.casefold()
        if key not in seen:
            seen.add(key)
            out.append(frag)

    add(base)
    parts = [p.strip() for p in base.split(",") if p.strip()]
    for p in parts:
        add(p)
    if len(parts) >= 2:
        add(", ".join(parts[:2]))
    if len(parts) >= 3:
        add(", ".join(parts[:3]))
    word_src = re.sub(r"[^\w\s-]", " ", base)
    words = [w for w in word_src.split() if len(w) >= 2]
    if len(words) >= 2:
        add(" ".join(words[: min(5, len(words))]))
    if len(words) >= 3:
        add(" ".join(words[:3]))
    return out[:8]


def _score_location_title_match(wanted: str, title: str) -> float:
    """How well a PARAMETERS row title matches the user's geography phrase.

    Prefers exact / prefix / LinkedIn-style expansions (e.g. city vs metro)
    and penalizes unrelated places that merely contain the same substring
    (e.g. South San Francisco when the ICP asked for San Francisco)."""
    w = _normalize_location_phrase(wanted).casefold()
    t = _normalize_location_phrase(title).casefold()
    if not w or not t:
        return 0.0
    if w == t:
        return 100.0
    if t.startswith(w + ",") or t.startswith(w + " ") or t.startswith(w + "("):
        return 95.0
    if t.startswith(w):
        return 94.0
    escaped = re.escape(w)
    m = re.search(rf"(?<!\w){escaped}\b", t)
    if not m:
        if w in t:
            return 45.0
        wt = {x for x in re.split(r"\W+", w) if len(x) >= 3}
        tt = {x for x in re.split(r"\W+", t) if len(x) >= 3}
        if not wt:
            return 0.0
        inter = len(wt & tt)
        return 25.0 * (inter / len(wt))

    if m.start() == 0:
        return 90.0
    before = t[: m.start()].rstrip()
    if not before:
        return 90.0
    if before[-1] in ",(":
        return 88.0
    tail = re.search(r"([\w'-]+)$", before)
    if tail:
        tw = tail.group(1).casefold()
        if tw not in w.split() and tw in ("greater", "upper", "lower", "metro"):
            return 78.0
        if tw not in w.split():
            return 35.0
    return 72.0


def resolve_location_ids_from_text(
    *,
    account_id: str,
    location_text: str,
    max_api_calls: int = 5,
    limit_per_query: int = 20,
    max_ids: int = 3,
    min_score: float = 40.0,
) -> list[str]:
    """Resolve free-text geography (e.g. ``San Francisco`` or ``London, UK``)
    to LinkedIn geo id strings via ``search_parameter_ids`` (type=LOCATION).

    Tries several query variants, merges results, and picks the best title
    matches so arbitrary ICP strings map to sensible geo filters."""
    if settings.unipile_mock or not (account_id or "").strip():
        return []
    phrase = _normalize_location_phrase(location_text)
    if len(phrase) < 2:
        return []
    variants = _location_query_variants(phrase)
    if not variants:
        return []
    by_id: dict[str, UnipileSearchParameter] = {}
    calls = 0
    for kw in variants:
        if calls >= max_api_calls:
            break
        rows = search_parameter_ids(
            account_id=account_id,
            type="LOCATION",
            keywords=kw,
            limit=limit_per_query,
        )
        calls += 1
        for r in rows:
            if r.id not in by_id:
                by_id[r.id] = r
        if len(by_id) >= 24:
            break
    if not by_id:
        return []
    scored: list[tuple[float, UnipileSearchParameter]] = []
    for row in by_id.values():
        s = _score_location_title_match(phrase, row.title)
        scored.append((s, row))
    scored.sort(key=lambda x: (-x[0], x[1].title.casefold()))
    ids: list[str] = []
    for score, row in scored:
        if score < min_score:
            break
        if row.id not in ids:
            ids.append(row.id)
        if len(ids) >= max_ids:
            break
    return ids[:10]


def _parse_unipile_person(raw: dict[str, Any]) -> UnipilePerson | None:
    """Normalize Unipile's people-search payload. Different endpoints nest the
    public_identifier under different keys; tolerate the variants."""
    if not raw:
        return None
    name = (
        raw.get("name")
        or " ".join(filter(None, [raw.get("first_name"), raw.get("last_name")])).strip()
        or None
    )
    public_id = (
        raw.get("public_identifier")
        or raw.get("public_id")
        or raw.get("slug")
        or ""
    )
    profile_url = (
        raw.get("public_profile_url")
        or raw.get("profile_url")
        or raw.get("url")
        or (f"https://www.linkedin.com/in/{public_id}" if public_id else "")
    )
    if not public_id and "/in/" in profile_url:
        public_id = profile_url.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if not public_id:
        return None
    return UnipilePerson(
        name=name or "(unknown)",
        public_identifier=public_id,
        profile_url=profile_url,
        title=raw.get("headline") or raw.get("title") or raw.get("occupation"),
        company=raw.get("company") or raw.get("company_name"),
        location=raw.get("location"),
        # Unipile returns "DISTANCE_1" / "DISTANCE_2" / "DISTANCE_3" /
        # "OUT_OF_NETWORK". Pass through verbatim so the caller can filter
        # without ambiguity.
        network_distance=(
            raw.get("network_distance")
            or raw.get("distance")
            or raw.get("connection_degree")
            or None
        ),
    )


def search_people(
    *,
    account_id: str,
    query: str,
    limit: int = 10,
    location_ids: tuple[str, ...] | None = None,
    industry_ids: tuple[str, ...] | None = None,
    network_distance_degrees: tuple[int, ...] = (2,),
) -> list[UnipilePerson]:
    """RULE 24 — LinkedIn classic people search via Unipile.

    Per Unipile's documented schema (POST /linkedin/search, classic+people):

      location           array of digit-STRINGS (e.g. ["103644278"] for US)
      network_distance   array of NUMBERS, enum {1, 2, 3}

    Default ``location`` is US (``LINKEDIN_GEO_URN_US``) unless overridden via
    ``UNIPILE_RULE24_LOCATION_IDS`` (comma-separated). ``None`` uses that default;
    pass ``location_ids=()`` to skip the geo filter entirely.

    Default network filter is 2nd-degree only, matching the audit URL.
    Pass ``network_distance_degrees=()`` to accept any connection degree. Both
    filters are sent server-side AND
    re-checked client-side as a belt-and-suspenders against tenant-side
    schema drift.

    Locked / private profiles ("LinkedIn Member" with public_identifier=None)
    are dropped by _parse_unipile_person."""
    if settings.unipile_mock or not query.strip():
        return []
    loc_ids = _rule24_default_location_ids() if location_ids is None else location_ids
    body: dict[str, Any] = {
        "api": "classic",
        "category": "people",
        "keywords": query[:300],
    }
    if loc_ids:
        body["location"] = list(loc_ids)
    if industry_ids:
        body["industry"] = list(industry_ids)
    if network_distance_degrees:
        body["network_distance"] = list(network_distance_degrees)
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "POST",
            "/linkedin/search",
            params={"account_id": account_id, "limit": limit},
            json=body,
        )
    payload = _check_resp(resp, "search_people")
    items = payload.get("items") or payload.get("results") or []
    out: list[UnipilePerson] = []
    allowed = (
        {f"DISTANCE_{d}" for d in network_distance_degrees}
        if network_distance_degrees
        else None
    )
    for raw in items:
        person = _parse_unipile_person(raw)
        if not person:
            continue
        if allowed:
            nd = (person.network_distance or "").upper()
            if nd not in allowed:
                continue
        out.append(person)
    log.info("unipile search_people people=%d query=%.120s", len(out), query)
    return out


def _resolve_user_provider_id(client: httpx.Client, *, account_id: str, slug: str) -> str | None:
    """Resolve a LinkedIn public-identifier slug to its `provider_id` URN.

    Unipile's `/users/{slug}/posts` endpoint rejects public slugs with
    422 "invalid_recipient" — it only accepts the provider_id URN
    (e.g. `ACoAA...`). The `/users/{slug}` endpoint, however, accepts
    slugs and returns the URN in `provider_id`.

    Returns None if the slug can't be resolved (locked profile, etc.).
    """
    try:
        resp = _request_with_429_retry(
            client, "GET", f"/users/{slug}", params={"account_id": account_id}
        )
    except httpx.RequestError:
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        # Don't raise — caller will treat None as "skip" without aborting
        # the entire seed loop.
        return None
    try:
        body = resp.json() or {}
    except ValueError:
        return None
    pid = body.get("provider_id")
    return str(pid) if pid else None


def get_user_posts(
    *, account_id: str, public_identifier_or_url: str, limit: int = 10
) -> list[UnipilePost]:
    """Fetch a specific LinkedIn user's recent posts.

    Two-step against Unipile:
      1. `/users/{slug}` resolves the public-identifier to a provider_id URN.
      2. `/users/{provider_id}/posts` returns the actual feed.

    Why two calls: Unipile's posts endpoint rejects public slugs with
    422 "invalid_recipient" — only URNs work. The resolve step accepts
    slugs and gives back the URN, so we never have to ask the caller
    to provide one.
    """
    if settings.unipile_mock:
        return []
    slug = public_identifier_or_url
    if "/in/" in slug:
        slug = slug.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if not slug:
        return []
    with _client() as client:
        # If the caller already passed a provider_id (URN), skip the resolve
        # step. URNs always start with "AC" or contain "fsd_profile:".
        if slug.startswith("AC") and len(slug) >= 30:
            user_id = slug
        else:
            user_id = _resolve_user_provider_id(client, account_id=account_id, slug=slug)
            if not user_id:
                return []
        resp = _request_with_429_retry(
            client,
            "GET",
            f"/users/{user_id}/posts",
            params={"account_id": account_id, "limit": limit},
        )
    if resp.status_code == 404:
        return []
    payload = _check_resp(resp, "get_user_posts")
    items = payload.get("items") or payload.get("posts") or []
    out: list[UnipilePost] = []
    for raw in items:
        post = _parse_unipile_post(raw)
        if post:
            out.append(post)
    return out


_POST_URL_ACTIVITY_RE = re.compile(r"activity[:\-](\d{15,25})")
_POST_URL_UGC_RE = re.compile(r"ugcPost[:\-](\d{15,25})")
_POST_URL_SHARE_RE = re.compile(r"share[:\-](\d{15,25})")


def extract_post_id_from_url(post_url_or_id: str) -> str | None:
    """Convert a LinkedIn post URL into the `post_id` Unipile expects.

    Per the Unipile API contract:
      • activity URLs → numeric id (e.g. `7332661864792854528`)
      • ugcPost URLs  → `urn:li:ugcPost:<id>`
      • share URLs    → `urn:li:share:<id>`

    If `post_url_or_id` is already in one of these forms (numeric id, or
    `urn:li:...`), it's returned unchanged. Returns None if no id can be
    parsed."""
    if not post_url_or_id:
        return None
    s = post_url_or_id.strip()
    # Already a URN / numeric id.
    if s.startswith("urn:li:") or (s.isdigit() and len(s) >= 15):
        return s
    # ugcPost / share URLs map to URN form per Unipile's docs.
    m = _POST_URL_UGC_RE.search(s)
    if m:
        return f"urn:li:ugcPost:{m.group(1)}"
    m = _POST_URL_SHARE_RE.search(s)
    if m:
        return f"urn:li:share:{m.group(1)}"
    # activity URLs map to the bare numeric id.
    m = _POST_URL_ACTIVITY_RE.search(s)
    if m:
        return m.group(1)
    return None


def get_post(*, account_id: str, post_id_or_url: str) -> UnipilePost | None:
    """Fetch the details of a single LinkedIn post.

    Wraps `GET /posts/{post_id}?account_id=...`. `post_id_or_url` can be a
    Unipile post_id (numeric for activity, `urn:li:ugcPost:...` /
    `urn:li:share:...` for the other forms) or a LinkedIn post URL — the
    URL is converted to the right id form via `extract_post_id_from_url`.

    Returns None when the post cannot be parsed into an id, when Unipile
    returns 404 (not found / hidden), or when the response cannot be
    parsed.
    """
    if not account_id:
        raise UnipileError("get_post: account_id required")
    if settings.unipile_mock:
        return None
    post_id = extract_post_id_from_url(post_id_or_url)
    if not post_id:
        return None

    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "GET",
            f"/posts/{post_id}",
            params={"account_id": account_id},
        )
    if resp.status_code == 404:
        return None
    payload = _check_resp(resp, "get_post")
    return _parse_unipile_post(payload)


# ---------------------------------------------------------------- comments

def get_post_comments(*, account_id: str, post_url: str) -> list[UnipileComment]:
    """
    Fetch comments on a LinkedIn post. Unipile resolves `post` from a URL or
    LinkedIn social_id; we pass the URL directly.
    """
    if settings.unipile_mock:
        return _MOCK_COMMENTS.get(post_url, [])

    if not account_id:
        raise UnipileError("get_post_comments: account_id required")
    if not post_url:
        return []
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "GET",
            "/posts/comments",
            params={"account_id": account_id, "post_url": post_url, "limit": 100},
        )
    payload = _check_resp(resp, "get_post_comments")
    items = payload.get("items") or []
    out: list[UnipileComment] = []
    for raw in items:
        author = raw.get("author") or {}
        out.append(
            UnipileComment(
                comment_id=str(raw.get("id") or ""),
                text=raw.get("text") or "",
                author_name=author.get("name"),
                author_provider_id=author.get("provider_id") or author.get("urn"),
                author_public_identifier=author.get("public_identifier"),
                author_profile_url=author.get("public_profile_url"),
                published_at=_parse_iso(raw.get("date") or raw.get("published_at")),
                reaction_count=_safe_int(raw.get("reaction_counter") or raw.get("num_reactions")),
                reply_count=_safe_int(raw.get("reply_counter") or raw.get("num_replies")),
            )
        )
    return out


def post_comment(
    *,
    account_id: str,
    post_url: str,
    text: str,
    parent_comment_id: str | None = None,
) -> UnipileCommentPostResult:
    """POST a top-level or threaded comment on a LinkedIn post via Unipile."""
    if not account_id:
        raise UnipileError("post_comment: account_id required")
    if settings.unipile_mock:
        now = datetime.now(timezone.utc)
        return UnipileCommentPostResult(
            comment_id=f"urn:li:comment:(MOCK:{hash(text) % 10_000_000})",
            posted_at=now,
            raw={"mock": True, "text": text[:200]},
        )
    post_id = extract_post_id_from_url(post_url)
    if not post_id:
        raise UnipileError("post_comment: could not parse post id from URL")
    body: dict[str, Any] = {"text": text[:8000]}
    if parent_comment_id:
        body["comment_id"] = parent_comment_id
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "POST",
            f"/posts/{post_id}/comments",
            params={"account_id": account_id},
            json=body,
        )
    payload = _check_resp(resp, "post_comment")
    cid = str(payload.get("id") or payload.get("comment_id") or payload.get("urn") or "")
    return UnipileCommentPostResult(
        comment_id=cid,
        posted_at=_parse_iso(payload.get("date") or payload.get("created_at")),
        raw=payload,
    )


# ---------------------------------------------------------------- outreach

def send_invite(
    *, account_id: str, provider_id: str, message: str | None = None
) -> str:
    """Send a LinkedIn connection request. Returns the invitation_id."""
    if settings.unipile_mock:
        return f"mock-invite-{provider_id[:8]}"
    body: dict[str, Any] = {"account_id": account_id, "provider_id": provider_id}
    if message:
        body["message"] = message[:300]
    with _client() as client:
        resp = _request_with_429_retry(client, "POST", "/users/invite", json=body)
    payload = _check_resp(resp, "send_invite")
    return str(payload.get("invitation_id") or payload.get("id") or "")


# ---------------------------------------------------------------- hosted auth

def create_hosted_auth_link(
    *,
    name: str,
    success_redirect_url: str,
    failure_redirect_url: str,
    notify_url: str | None = None,
    expires_minutes: int = 15,
) -> str:
    """
    Returns a Unipile-hosted URL that walks the user through LinkedIn login.
    On success, Unipile creates an account on this tenant with the given `name`,
    which we use later to find + attach it to the right cofounder.
    """
    if settings.unipile_mock:
        return f"https://mock.unipile.local/auth?name={name}"

    body = {
        "type": "create",
        "providers": ["LINKEDIN"],
        "api_url": _api_root(),
        "expiresOn": (
            datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "name": name,
        "success_redirect_url": success_redirect_url,
        "failure_redirect_url": failure_redirect_url,
    }
    if notify_url:
        body["notify_url"] = notify_url

    with _client() as client:
        resp = _request_with_429_retry(
            client, "POST", "/hosted/accounts/link", json=body
        )
    payload = _check_resp(resp, "create_hosted_auth_link")
    url = payload.get("url") or payload.get("hosted_url")
    if not url:
        raise UnipileError(f"hosted_auth_link returned no url: {payload}")
    return str(url)


def find_account_by_name(name: str) -> UnipileAccount | None:
    """Find the most recent account on this tenant whose `name` correlates."""
    if settings.unipile_mock:
        return None
    with _client() as client:
        resp = _request_with_429_retry(
            client, "GET", "/accounts", params={"name": name, "limit": 5}
        )
    payload = _check_resp(resp, "find_account_by_name")
    items = payload.get("items") or []
    if not items:
        return None
    raw = items[0]
    return UnipileAccount(
        id=str(raw.get("id") or ""),
        name=raw.get("name") or "(unnamed)",
        profile_url=raw.get("profile_url") or (raw.get("params") or {}).get("profile_url"),
        avatar_url=raw.get("avatar_url"),
        account_type=raw.get("type") or raw.get("account_type"),
    )


# ---------------------------------------------------------------- profile lookup

def resolve_profile(*, account_id: str, public_identifier_or_url: str) -> dict[str, Any]:
    """Resolve a LinkedIn slug or URL to provider_id + member_urn."""
    if settings.unipile_mock:
        slug = public_identifier_or_url.rstrip("/").rsplit("/", 1)[-1]
        return {
            "provider_id": f"urn:li:fsd_profile:MOCK_{slug}",
            "public_identifier": slug,
            "first_name": slug.split("-")[0].title(),
            "last_name": " ".join(s.title() for s in slug.split("-")[1:]),
        }
    slug = public_identifier_or_url
    if "/in/" in slug:
        slug = slug.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    with _client() as client:
        resp = _request_with_429_retry(
            client, "GET", f"/users/{slug}", params={"account_id": account_id}
        )
    return _check_resp(resp, "resolve_profile")


# ---------------------------------------------------------------- mock data

_MOCK_COMMENTS: dict[str, list[UnipileComment]] = {
    "https://www.linkedin.com/posts/jane-rivera-platform_internal-developer-platform-activity-1": [
        UnipileComment(
            comment_id="mock-c1",
            text=(
                "Love the deprecation-date angle. We tried this without naming an "
                "owner and it stalled at 50% — the named owner is what got us "
                "across. Curious how you handled the team that *owned* the legacy path?"
            ),
            author_name="Marcus Chen",
            author_provider_id="urn:li:fsd_profile:MOCK_marcus-chen-eng",
            author_public_identifier="marcus-chen-eng",
            author_profile_url="https://linkedin.com/in/marcus-chen-eng",
            published_at=None,
        ),
        UnipileComment(
            comment_id="mock-c2",
            text=(
                "Strong piece. We just hired a Director of Platform Eng for exactly "
                "this — would love to chat about the cutover playbook if you're open."
            ),
            author_name="Priya Shah",
            author_provider_id="urn:li:fsd_profile:MOCK_priya-shah-vp",
            author_public_identifier="priya-shah-vp",
            author_profile_url="https://linkedin.com/in/priya-shah-vp",
            published_at=None,
        ),
        # Second reply from Marcus — triggers his lead.reply_count to 2,
        # which fires the PERMANENT-RULE-5 auto-CR path.
        UnipileComment(
            comment_id="mock-c3",
            text=(
                "Following up — we ended up making the legacy owners advisors on the "
                "cutover plan, not blockers. Worth a 15 min call to compare notes?"
            ),
            author_name="Marcus Chen",
            author_provider_id="urn:li:fsd_profile:MOCK_marcus-chen-eng",
            author_public_identifier="marcus-chen-eng",
            author_profile_url="https://linkedin.com/in/marcus-chen-eng",
            published_at=None,
        ),
    ],
}
