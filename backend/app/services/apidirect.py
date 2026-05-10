"""
Sync httpx client for apidirect.io's LinkedIn Search Posts endpoint.

Endpoint shape derived from gtm-engine's existing apidirect_client (same vendor,
same keys):
  GET https://apidirect.io/v1/linkedin/posts?query=...&page=1
  Header: X-API-Key: <key>
  Response: {"posts": [{url, title, domain, snippet, date, authors, source}]}

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
        authors = raw.get("authors")
        if isinstance(authors, list):
            author = authors[0] if authors else None
        else:
            author = authors
        return cls(
            url=raw["url"],
            title=raw.get("title", "") or "",
            snippet=raw.get("snippet", "") or "",
            author=author,
            domain=raw.get("domain"),
            published_at=_parse_iso(raw.get("date")),
            source=raw.get("source"),
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
