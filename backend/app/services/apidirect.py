"""
Sync httpx client for apidirect.io LinkedIn endpoints (same vendor / API key):

  GET https://apidirect.io/v1/linkedin/posts?query=...&page=1  — search
  GET https://apidirect.io/v1/linkedin/post?url=...             — single post

Header: X-API-Key: <key>

Search response: {"posts": [{url, title, domain, snippet, date, author | authors, ...}]}
Post details: single JSON object with text, author, author_url, urn, ...

Concurrency: spec says 3 concurrent per endpoint per user; a threading semaphore
keeps the engine within that bound when called from multiple Celery tasks.

Quota: HTTP 402 trips a process-wide circuit breaker so we don't burn credits
hammering a dead account.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://apidirect.io"
_TIMEOUT = 45.0
# 3 concurrent per endpoint is APIDirect's hard server-side cap — exceeding
# it returns HTTP 429 `concurrency_limit_exceeded` (verified live 2026-05-14
# when we briefly tested with 8 workers and got 12/15 jobs back as 429).
# Our local semaphore must equal or be below the server's cap to avoid
# self-inflicted 429s.
_MAX_CONCURRENCY = 3

_concurrency = threading.Semaphore(_MAX_CONCURRENCY)
_circuit_open = False
_circuit_lock = threading.Lock()


class ApiDirectError(RuntimeError):
    pass


class ApiDirectQuotaExhausted(ApiDirectError):
    pass


class ApiDirectNotConfigured(ApiDirectError):
    pass


@dataclass(frozen=True)
class LinkedInPost:
    url: str
    title: str
    snippet: str
    author: str | None
    domain: str | None
    published_at: datetime | None
    source: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LinkedInPost":
        # Vendor docs use singular `author` (string); older payloads used `authors`.
        authors = raw.get("authors")
        if isinstance(authors, list):
            author = authors[0] if authors else None
        elif isinstance(authors, str) and authors.strip():
            author = authors.strip()
        else:
            a = raw.get("author")
            author = a.strip() if isinstance(a, str) and a.strip() else None
        return cls(
            url=raw["url"],
            title=raw.get("title", "") or "",
            snippet=raw.get("snippet", "") or "",
            author=author,
            domain=raw.get("domain"),
            published_at=_parse_iso(raw.get("date")),
            source=raw.get("source"),
        )


@dataclass(frozen=True)
class LinkedInPostDetails:
    """Response shape for GET /v1/linkedin/post (single-post enrichment)."""

    url: str
    text: str
    author: str | None
    author_url: str | None
    author_description: str | None
    published_at: datetime | None
    urn: str | None
    is_repost: bool | None
    likes: int = 0
    comments: int = 0
    shares: int = 0
    reactions_by_type: dict[str, int] | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LinkedInPostDetails":
        ir = raw.get("is_repost")
        is_repost = ir if isinstance(ir, bool) else None
        tx = raw.get("text")
        text = tx.strip() if isinstance(tx, str) else ""
        au = raw.get("author_url")
        author_url = au.strip() if isinstance(au, str) and au.strip() else None
        ad = raw.get("author_description")
        author_description = ad if isinstance(ad, str) else None
        an = raw.get("author")
        author = an.strip() if isinstance(an, str) and an.strip() else None
        u = raw.get("url")
        url = u.strip() if isinstance(u, str) and u.strip() else ""
        urn_raw = raw.get("urn")
        urn = urn_raw.strip() if isinstance(urn_raw, str) and urn_raw.strip() else None
        likes = 0
        comments = 0
        shares = 0
        react_map: dict[str, int] | None = None
        if settings.apidirect_fetch_reaction_breakdown:
            likes = int(raw.get("likes") or raw.get("num_likes") or 0)
            comments = int(raw.get("comments") or raw.get("num_comments") or 0)
            shares = int(raw.get("shares") or raw.get("num_shares") or 0)
            rb = raw.get("reactions_by_type") or raw.get("reactions")
            if isinstance(rb, dict):
                react_map = {}
                for k, v in rb.items():
                    if isinstance(v, (int, float)):
                        react_map[str(k)] = int(v)
                    elif isinstance(v, str) and v.strip().isdigit():
                        react_map[str(k)] = int(v.strip())
                if not react_map:
                    react_map = None
        return cls(
            url=url,
            text=text,
            author=author,
            author_url=author_url,
            author_description=author_description,
            published_at=_parse_iso(raw.get("date")),
            urn=urn,
            is_repost=is_repost,
            likes=likes,
            comments=comments,
            shares=shares,
            reactions_by_type=react_map,
        )


@dataclass(frozen=True)
class LinkedInCompanyDetails:
    """Response shape for GET /v1/linkedin/company.

    Structured industry classification, employee count, description, and
    specialties — the fields the Crustdata person endpoint doesn't surface.
    Used to score author-company fit against operator target_industries
    without relying on substring-matching the company name.

    Per APIDirect docs: $0.006 per request, 50 free monthly requests."""

    url: str
    name: str | None
    company_id: int | None
    description: str | None
    industry: str | None
    website: str | None
    followers: int
    employees: int
    employee_range: str | None
    founded_year: int | None
    specialities: list[str]
    headquarters: dict[str, Any] | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LinkedInCompanyDetails":
        def _str(v: Any) -> str | None:
            return v.strip() if isinstance(v, str) and v.strip() else None

        def _int(v: Any) -> int:
            if isinstance(v, bool):
                return 0
            if isinstance(v, int):
                return max(0, v)
            if isinstance(v, float):
                return max(0, int(v))
            if isinstance(v, str) and v.strip().isdigit():
                return int(v.strip())
            return 0

        cid_raw = raw.get("company_id")
        cid = cid_raw if isinstance(cid_raw, int) else None
        fy_raw = raw.get("founded_year")
        fy = fy_raw if isinstance(fy_raw, int) else None
        spec = raw.get("specialities") or raw.get("specialties") or []
        spec_list = [str(s).strip() for s in spec if isinstance(s, str) and s.strip()] if isinstance(spec, list) else []
        hq = raw.get("headquarters")
        hq_dict = hq if isinstance(hq, dict) else None
        return cls(
            url=_str(raw.get("url")) or "",
            name=_str(raw.get("name")),
            company_id=cid,
            description=_str(raw.get("description")),
            industry=_str(raw.get("industry")),
            website=_str(raw.get("website")),
            followers=_int(raw.get("followers")),
            employees=_int(raw.get("employees")),
            employee_range=_str(raw.get("employee_range")),
            founded_year=fy,
            specialities=spec_list,
            headquarters=hq_dict,
        )


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _client() -> httpx.Client:
    if not settings.apidirect_api_key:
        raise ApiDirectNotConfigured(
            "APIDIRECT_API_KEY is not set. Add it to .env to enable LinkedIn discovery."
        )
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"X-API-Key": settings.apidirect_api_key},
        timeout=_TIMEOUT,
    )


def _check_circuit() -> None:
    global _circuit_open
    with _circuit_lock:
        if _circuit_open:
            raise ApiDirectQuotaExhausted(
                "apidirect quota exhausted (HTTP 402). Circuit open; clear by "
                "restarting the worker after topping up credits."
            )


def _trip_circuit() -> None:
    global _circuit_open
    with _circuit_lock:
        _circuit_open = True


def search_linkedin_posts(query: str, *, page: int = 1) -> list[LinkedInPost]:
    """One LinkedIn search call. Returns up to ~20 posts per page."""
    if not query.strip():
        return []
    if settings.apidirect_mock:
        return _mock_search(query)
    _check_circuit()

    with _concurrency:
        try:
            with _client() as client:
                resp = client.get(
                    "/v1/linkedin/posts",
                    params={"query": query[:500], "page": max(1, min(page, 5))},
                )
        except httpx.RequestError as err:
            raise ApiDirectError(f"apidirect transport error: {err}") from err

    if resp.status_code == 402:
        _trip_circuit()
        raise ApiDirectQuotaExhausted(
            f"apidirect 402 quota exhausted: {resp.text[:300]}"
        )
    if resp.status_code == 401:
        raise ApiDirectError(f"apidirect 401 auth failure: {resp.text[:300]}")
    if resp.status_code == 429:
        raise ApiDirectError(f"apidirect 429 rate limit: {resp.text[:300]}")
    if resp.status_code >= 400:
        raise ApiDirectError(
            f"apidirect {resp.status_code}: {resp.text[:300]}"
        )

    payload = resp.json()
    raw_posts = payload.get("posts") or []
    out: list[LinkedInPost] = []
    for raw in raw_posts:
        try:
            out.append(LinkedInPost.from_api(raw))
        except (KeyError, TypeError) as err:
            log.warning("apidirect post parse skipped: %s (raw=%r)", err, raw)
    # Cost accounting — one billable call per search page, regardless of
    # how many posts came back.
    try:
        from app.services import cost_tracker
        cost_tracker.record_apidirect_call("search")
    except Exception as err:  # noqa: BLE001
        log.debug("apidirect.search: cost record skipped: %s", err)
    return out


def search_linkedin_posts_pages(query: str, *, max_pages: int) -> list[LinkedInPost]:
    """Fetch pages 1..max_pages (inclusive), deduping by post URL. Stops early
    when a page returns no posts. max_pages is clamped to [1, 5]."""
    if not query.strip():
        return []
    cap = max(1, min(int(max_pages), 5))
    seen: set[str] = set()
    merged: list[LinkedInPost] = []
    for page in range(1, cap + 1):
        batch = search_linkedin_posts(query, page=page)
        if not batch:
            break
        for p in batch:
            if p.url and p.url not in seen:
                seen.add(p.url)
                merged.append(p)
    return merged


def get_linkedin_post_details(url: str) -> LinkedInPostDetails | None:
    """GET /v1/linkedin/post — full post + author profile URL, text, URN.

    Returns None on 404 or empty body. Raises ApiDirectQuotaExhausted on 402."""
    url = (url or "").strip()
    if not url:
        return None
    if settings.apidirect_mock:
        return _mock_post_details(url)
    _check_circuit()

    params: dict[str, Any] = {"url": url[:500]}
    if settings.discovery_apidirect_post_details_get_sentiment:
        params["get_sentiment"] = "true"

    with _concurrency:
        try:
            with _client() as client:
                resp = client.get("/v1/linkedin/post", params=params)
        except httpx.RequestError as err:
            raise ApiDirectError(f"apidirect transport error: {err}") from err

    if resp.status_code == 402:
        _trip_circuit()
        raise ApiDirectQuotaExhausted(
            f"apidirect 402 quota exhausted: {resp.text[:300]}"
        )
    if resp.status_code == 401:
        raise ApiDirectError(f"apidirect 401 auth failure: {resp.text[:300]}")
    if resp.status_code == 429:
        raise ApiDirectError(f"apidirect 429 rate limit: {resp.text[:300]}")
    if resp.status_code == 404:
        log.warning("apidirect post details 404 url=%s", url[:120])
        return None
    if resp.status_code >= 400:
        log.warning(
            "apidirect post details %s url=%s body=%s",
            resp.status_code,
            url[:120],
            resp.text[:200],
        )
        return None

    try:
        raw = resp.json()
    except ValueError:
        log.warning("apidirect post details invalid JSON url=%s", url[:120])
        return None
    if not isinstance(raw, dict):
        return None
    try:
        details = LinkedInPostDetails.from_api(raw)
    except (KeyError, TypeError) as err:
        log.warning("apidirect post details parse skipped: %s url=%s", err, url[:120])
        return None
    # Cost accounting — one billable call per 2xx response (we got data).
    try:
        from app.services import cost_tracker
        cost_tracker.record_apidirect_call("post")
    except Exception as err:  # noqa: BLE001
        log.debug("apidirect.post: cost record skipped: %s", err)
    return details


def get_linkedin_company_details(url: str) -> LinkedInCompanyDetails | None:
    """GET /v1/linkedin/company — structured industry, employee count,
    description, specialties for a LinkedIn company page.

    Returns ``None`` on **any** failure (404, 502 upstream, transport error,
    invalid JSON, malformed parser input). The caller treats absence as
    "no extra company context available" and proceeds without it — never
    blocks the discovery pipeline on company enrichment.

    Raises ``ApiDirectQuotaExhausted`` on 402 so the slate-wide circuit
    breaker can trip and skip subsequent calls for the rest of the run.
    Per docs: $0.006/call, 50 free monthly requests."""
    url = (url or "").strip()
    if not url:
        return None
    if settings.apidirect_mock:
        return None
    _check_circuit()

    with _concurrency:
        try:
            with _client() as client:
                resp = client.get("/v1/linkedin/company", params={"url": url[:500]})
        except httpx.RequestError as err:
            log.warning("apidirect company transport error: %s url=%s", err, url[:120])
            return None

    if resp.status_code == 402:
        _trip_circuit()
        raise ApiDirectQuotaExhausted(
            f"apidirect 402 quota exhausted: {resp.text[:300]}"
        )
    if resp.status_code == 401:
        log.warning("apidirect company 401 auth failure url=%s", url[:120])
        return None
    if resp.status_code == 429:
        log.warning("apidirect company 429 rate limit url=%s", url[:120])
        return None
    if resp.status_code == 404:
        log.info("apidirect company 404 (not found) url=%s", url[:120])
        return None
    if resp.status_code >= 400:
        log.warning(
            "apidirect company %s url=%s body=%s",
            resp.status_code, url[:120], resp.text[:200],
        )
        return None

    try:
        raw = resp.json()
    except ValueError:
        log.warning("apidirect company invalid JSON url=%s", url[:120])
        return None
    if not isinstance(raw, dict):
        return None
    try:
        details = LinkedInCompanyDetails.from_api(raw)
    except (KeyError, TypeError) as err:
        log.warning("apidirect company parse skipped: %s url=%s", err, url[:120])
        return None
    # Cost accounting — $0.006 per successful company fetch.
    try:
        from app.services import cost_tracker
        cost_tracker.record_apidirect_call("company")
    except Exception as err:  # noqa: BLE001
        log.debug("apidirect.company: cost record skipped: %s", err)
    return details


# ---------------------------------------------------------------- mock mode
# Hand-curated dataset for offline demos / when apidirect quota is dry. Each
# post is intentionally varied so the 4-gate filter has something to actually
# filter — about half should survive into the drafter.

_MOCK_DATASET: list[dict[str, str]] = [
    {
        "url": "https://www.linkedin.com/posts/jane-rivera-platform_internal-developer-platform-activity-1",
        "author": "Jane Rivera",
        "title": "What we learned shipping our IDP v2",
        "snippet": (
            "Jane Rivera, VP of Platform Engineering at a Series B B2B SaaS in "
            "San Francisco. We rebuilt our internal developer platform last "
            "quarter and the biggest surprise wasn't technical. Half our 'paved "
            "road' adoption problems were because we never deprecated the old "
            "way. Engineers kept routing around us because the legacy bash "
            "scripts still worked. Once we put a hard cutover date on the old "
            "path and named an owner for any blockers, adoption went from 31% "
            "to 78% in six weeks."
        ),
    },
    {
        "url": "https://www.linkedin.com/posts/marcus-chen-eng_ci-pipeline-test-isolation-activity-2",
        "author": "Marcus Chen",
        "title": "Our CI was 22 minutes",
        "snippet": (
            "Marcus Chen, Director of Platform Engineering, Series B B2B SaaS "
            "(US-based developer tools). Our CI sat at 22 minutes for almost a "
            "year before we admitted it wasn't a CI problem. It was a test-"
            "isolation problem dressed up in CI clothing. Three engineers "
            "spent two weeks rewriting our fixtures so tests didn't share "
            "state, and CI dropped to 7 minutes with the same hardware. The "
            "lesson: most slow CI is actually expensive setup/teardown you "
            "forgot about."
        ),
    },
    {
        "url": "https://www.linkedin.com/posts/priya-shah-vp_on-call-burnout-rotation-activity-3",
        "author": "Priya Shah",
        "title": "Two-week on-call rotations",
        "snippet": (
            "Priya Shah, VP of Engineering at a Series B SaaS in New York "
            "(developer tools / B2B). We moved from one-week to two-week on-"
            "call rotations and the alert volume per shift dropped 40%. "
            "Counter-intuitive at first, but a longer rotation gave the on-"
            "call enough time to actually fix the underlying alert source "
            "instead of just acking and moving on. We now retire one alert "
            "class per rotation as a hard target."
        ),
    },
    {
        "url": "https://www.linkedin.com/posts/danni-okafor-recruiter_hiring-platform-engineers-activity-4",
        "author": "Danni Okafor",
        "title": "Hiring platform engineers",
        "snippet": (
            "Technical Recruiter at FastGrowth Labs. Open to hiring platform "
            "engineers, SRE, and DevOps folks for our growing team! We have "
            "great benefits, remote-first, equity, and a supportive culture. "
            "DM me if you're looking, happy to chat about open roles. Reposts "
            "appreciated! #hiring #platformengineering"
        ),
    },
    {
        "url": "https://www.linkedin.com/posts/research-team-gartner_devops-report-2026-activity-5",
        "author": "DevOps Insights Weekly",
        "title": "Gartner's new DevOps report",
        "snippet": (
            "DevOps Insights Weekly — research roundup. Gartner just released "
            "their 2026 DevOps Maturity report and the headline finding is "
            "staggering: 73% of enterprises rate their own DevOps maturity as "
            "'low' or 'beginning', up from 64% last year. Meanwhile only 22% "
            "have an internal developer platform. The report concludes that "
            "platform engineering is the single highest-leverage investment "
            "for the next 24 months. Full link in comments."
        ),
    },
    {
        "url": "https://www.linkedin.com/posts/agree-or-disagree_engagement-poll-activity-6",
        "author": "Hot Takes Daily",
        "title": "Type AGREE if you do this",
        "snippet": (
            "Type AGREE if you think CI should be under 10 minutes! 🚀 "
            "Comment a 1 if your CI is fast, a 2 if it's slow. Reposting helps "
            "more engineers see this!! 👇👇 What's your CI time? Drop it below "
            "and let's see who's the fastest! 💪"
        ),
    },
    {
        "url": "https://www.linkedin.com/posts/sam-tellez-staff_devex-team-of-one-activity-7",
        "author": "Sam Tellez",
        "title": "DevEx team of one",
        "snippet": (
            "Sam Tellez, Head of Platform Engineering at a Series B SaaS in "
            "Seattle. Year one as a DevEx team of one at a 220-person eng "
            "org. The biggest unlock wasn't tooling, it was getting permission "
            "to remove things. We sunsetted four internal CLIs, three "
            "deployment dashboards, and an entire wiki. Cognitive load went "
            "down for every team. Took six months to convince leadership the "
            "ROI was real."
        ),
    },
]


def _mock_post_details(url: str) -> LinkedInPostDetails | None:
    """Offline enrichment aligned with `_MOCK_DATASET` URLs."""
    for raw in _MOCK_DATASET:
        if raw["url"] == url:
            slug = url.rstrip("/").rsplit("/", 1)[-1].split("-activity-")[0]
            fake_profile = f"https://www.linkedin.com/in/{slug[:48]}"
            return LinkedInPostDetails(
                url=url,
                text=raw["snippet"],
                author=raw["author"],
                author_url=fake_profile,
                author_description=None,
                published_at=None,
                urn=None,
                is_repost=False,
            )
    log.warning("apidirect MOCK: no post details for url=%s", url[:120])
    return None


def _mock_search(query: str) -> list[LinkedInPost]:
    """Deterministic-ish slice based on the query hash so different keywords get
    different subsets but the totals stay stable."""
    log.info("apidirect MOCK mode: query=%r", query)
    seed = sum(ord(c) for c in query.lower()) % len(_MOCK_DATASET)
    out: list[LinkedInPost] = []
    for i, raw in enumerate(_MOCK_DATASET):
        if (i + seed) % 2 == 0 or i in (0, 1, 2):
            out.append(
                LinkedInPost(
                    url=raw["url"],
                    title=raw["title"],
                    snippet=raw["snippet"],
                    author=raw["author"],
                    domain="linkedin.com",
                    published_at=None,
                    source="mock",
                )
            )
    return out
