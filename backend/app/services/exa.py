"""
Exa semantic-search client (sync version, scoped to LinkedIn posts).

Used as an additional discovery source alongside apidirect. Hits Exa's
`/search` endpoint with `includeDomains=["linkedin.com"]` and `type=keyword`
so we only get LinkedIn-hosted content. Per-call cost is ~1 credit regardless
of `numResults`, so we crank `numResults` high (50-100) to maximize unique
posts per credit.

Quota: 401/402/429 trip a process-wide circuit so a misconfigured run doesn't
burn calls.
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

_BASE_URL = "https://api.exa.ai"
_TIMEOUT = 45.0


class ExaError(RuntimeError):
    pass


class ExaNotConfigured(ExaError):
    pass


class ExaQuotaExhausted(ExaError):
    pass


@dataclass(frozen=True)
class ExaPost:
    url: str
    title: str
    snippet: str
    author: str | None
    domain: str | None
    published_at: datetime | None
    raw: dict[str, Any]


_circuit_open = False
_circuit_lock = threading.Lock()


def _check_circuit() -> None:
    with _circuit_lock:
        if _circuit_open:
            raise ExaQuotaExhausted("Exa circuit open (prior 401/402/429).")


def _trip_circuit() -> None:
    global _circuit_open
    with _circuit_lock:
        _circuit_open = True


def reset_exa_circuit() -> None:
    """Clear the process-wide quota/auth circuit (e.g. after fixing EXA_API_KEY
    without restarting the worker). Safe to call at the start of each discovery run."""
    global _circuit_open
    with _circuit_lock:
        _circuit_open = False


def _client() -> httpx.Client:
    if not settings.exa_api_key:
        raise ExaNotConfigured("EXA_API_KEY is not set.")
    return httpx.Client(
        base_url=_BASE_URL,
        headers={
            "x-api-key": settings.exa_api_key,
            "Content-Type": "application/json",
        },
        timeout=_TIMEOUT,
    )


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        v = value
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _normalize_hit(raw: dict[str, Any]) -> ExaPost | None:
    url = str(raw.get("url") or "").strip()
    if not url:
        return None
    # Exa returns LinkedIn posts under paths like /posts/, /pulse/. Drop
    # everything else (company pages, /jobs/, /school/, etc.) — they're not
    # individual posts we can comment on.
    if "/posts/" not in url and "/pulse/" not in url and "/feed/update" not in url:
        return None

    title = str(raw.get("title") or "")
    text = str(raw.get("text") or "").strip()
    highlights = raw.get("highlights") if isinstance(raw.get("highlights"), list) else []
    hl_texts = [str(h).strip() for h in highlights if isinstance(h, str) and h.strip()]
    # Prefer Exa's full ``text`` over the first highlight excerpt: highlights
    # are query-matched middle phrases, so when the drafter sees only the
    # highlight it loses the post's opening context and the comment ends up
    # responding to a sub-clause instead of the real post.
    if text:
        snippet = text[:3000]
    elif hl_texts:
        snippet = hl_texts[0][:3000]
    else:
        snippet = ""

    author = raw.get("author")
    if author is not None:
        author = str(author).strip() or None

    domain = None
    try:
        host = httpx.URL(url).host
        domain = host.removeprefix("www.").lower() if host else None
    except Exception:
        pass

    return ExaPost(
        url=url,
        title=title,
        snippet=snippet,
        author=author,
        domain=domain,
        published_at=_parse_iso(raw.get("publishedDate") or raw.get("published_date")),
        raw=raw,
    )


def search_linkedin_posts(
    query: str,
    *,
    num_results: int | None = None,
    include_text: bool = True,
    start_published_date: str | None = None,
) -> list[ExaPost]:
    """Run a single Exa search scoped to LinkedIn. Returns up to ~num_results
    LinkedIn-post hits.

    `start_published_date` (ISO date string like "2026-04-25") narrows results
    to posts after that date — useful with our recency filter so we don't
    waste an Exa call retrieving 2-year-old posts.
    """
    if not query.strip():
        return []
    _check_circuit()
    n = num_results or settings.exa_results_per_query
    n = max(1, min(n, 100))

    # type=neural gives ~10x more recall on LinkedIn than type=keyword
    # (verified empirically on pharma/biotech queries: neural=24 vs keyword=2).
    # Crustdata-grade semantic match against post text.
    payload: dict[str, Any] = {
        "query": query[:500],
        "type": "neural",
        "numResults": n,
        "includeDomains": ["linkedin.com"],
        "contents": {
            "highlights": {"maxCharacters": 500, "numSentences": 3},
        },
    }
    if include_text:
        payload["contents"]["text"] = {"maxCharacters": 1500}
    if start_published_date:
        payload["startPublishedDate"] = start_published_date

    with _client() as client:
        try:
            resp = client.post("/search", json=payload)
        except httpx.RequestError as err:
            raise ExaError(f"exa transport error: {err}") from err

    if resp.status_code in (401, 402):
        _trip_circuit()
        raise ExaQuotaExhausted(f"exa {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 429:
        _trip_circuit()
        raise ExaError(f"exa 429 rate-limited: {resp.text[:300]}")
    if resp.status_code >= 400:
        raise ExaError(f"exa {resp.status_code}: {resp.text[:300]}")

    try:
        body = resp.json()
    except ValueError as err:
        raise ExaError(f"exa returned non-JSON: {err}") from err

    raw_results = body.get("results") or []
    out: list[ExaPost] = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        post = _normalize_hit(r)
        if post is not None:
            out.append(post)
    log.info("exa.search: query=%r returned %d LinkedIn posts (raw=%d)", query[:60], len(out), len(raw_results))
    return out
