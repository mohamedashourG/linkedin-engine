"""
Crustdata People Enrichment client (`/screener/person/enrich`).

Used as a higher-quality, batched replacement for PDL in profile_resolve.
Returns title + employer_name + headline + location for each LinkedIn URL —
the fields ICP scoring needs but apidirect's post search doesn't include.

Quotas: 3 credits per matched profile (database lookup), 5 credits per
realtime lookup. Crustdata bills per match, not per call. The endpoint
accepts up to 25 LinkedIn URLs per request via comma-separated query param,
so we batch heavily to minimize round-trips.

Circuit breaker: 401/402/429 trip a process-wide flag so a misconfigured run
or budget exhaustion doesn't burn calls.

URL pre-filter (`is_likely_person_slug`): apidirect-derived author URLs are
~35% brand handles (kwello-com, msl-mastery, hayloarc) that Crustdata can't
match. Caller should filter through this helper before paying for an
enrichment lookup.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://api.crustdata.com"
_ENRICH_PATH = "/screener/person/enrich"
_TIMEOUT = 60.0
_BATCH_SIZE = 25  # Crustdata's per-request limit on linkedin_profile_url

# Brand-handle suffixes that look like people URLs but aren't. apidirect
# returns post URLs whose author slug is whatever follows /posts/ — for
# company-published posts, that's the brand handle. Skip these before
# spending an enrichment credit.
_BRAND_HANDLE_SUFFIXES = (
    "-com", "-co", "-inc", "-ltd", "-llc", "-app", "-ai", "-io",
    "-jobs", "-careers", "-hiring", "-recruiting",
    "-mastery", "-academy", "-institute", "-network", "-news",
    "-magazine", "-podcast", "-media", "-press", "-blog",
    "-team", "-group", "-labs", "-studios", "-systems",
)
_BRAND_HANDLE_KEYWORDS = (
    "official", "company", "corporate", "global",
)
# Slugs that are pure single tokens (no hyphen) AND don't match common
# personal-name patterns are also suspicious — but we only filter the
# obvious ones. Real person slugs often have a dash (firstname-lastname).
_PURE_NUMERIC_RE = re.compile(r"/in/[a-z0-9]*\d{4,}", re.I)
# apidirect sometimes returns post URLs like
# `linkedin.com/posts/activity-7345843680987049986-XYz_...` which our
# author-URL extractor turns into `linkedin.com/in/activity-...`. That's a
# malformed slug — Crustdata 400s the entire batch on it.
_APIDIRECT_MANGLED_PREFIXES = (
    "activity-", "ugcpost-", "share-", "feed-",
)
# Strict slug shape Crustdata accepts: alphanumeric + hyphens. Anything
# with a `?`, `&`, `=`, `%`, `:`, etc. is malformed.
_LINKEDIN_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$", re.I)


class CrustdataEnrichError(RuntimeError):
    pass


class CrustdataEnrichNotConfigured(CrustdataEnrichError):
    pass


class CrustdataEnrichQuotaExhausted(CrustdataEnrichError):
    pass


_circuit_open = False
_circuit_lock = threading.Lock()


def _check_circuit() -> None:
    with _circuit_lock:
        if _circuit_open:
            raise CrustdataEnrichQuotaExhausted(
                "Crustdata enrich circuit open (prior 401/402/429). Restart "
                "worker after fixing credentials/credit."
            )


def _trip_circuit() -> None:
    global _circuit_open
    with _circuit_lock:
        _circuit_open = True


@dataclass(frozen=True)
class EnrichedProfile:
    linkedin_url: str
    name: str | None
    title: str | None
    employer_name: str | None
    employer_domain: str | None
    headline: str | None
    location: str | None
    num_connections: int | None
    raw: dict[str, Any]


def is_likely_person_slug(linkedin_url: str | None) -> bool:
    """Return True if the URL looks like a real person profile, False if it
    looks like a brand handle, malformed, or apidirect-mangled. Conservative —
    favours false negatives over paying credits on guaranteed-bad batches."""
    if not linkedin_url:
        return False
    if "/in/" not in linkedin_url:
        return False
    slug = linkedin_url.rsplit("/in/", 1)[-1].rstrip("/").lower()
    if not slug:
        return False
    # Drop trailing query/fragment if any leaked through
    slug = slug.split("?", 1)[0].split("#", 1)[0]
    # apidirect-mangled: post URLs that got turned into bogus /in/ slugs
    if any(slug.startswith(pfx) for pfx in _APIDIRECT_MANGLED_PREFIXES):
        return False
    # Strict slug-shape check (alphanumeric + hyphen + underscore only).
    # Crustdata returns 400 on anything else — and 400s blow up the whole batch.
    if not _LINKEDIN_SLUG_RE.match(slug):
        return False
    if any(slug.endswith(sfx) for sfx in _BRAND_HANDLE_SUFFIXES):
        return False
    if any(kw in slug for kw in _BRAND_HANDLE_KEYWORDS):
        return False
    return True


def _client() -> httpx.Client:
    if not settings.crustdata_api_key:
        raise CrustdataEnrichNotConfigured("CRUSTDATA_API_KEY is not set.")
    return httpx.Client(
        base_url=_BASE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Token {settings.crustdata_api_key}",
        },
        timeout=_TIMEOUT,
    )


def _parse_one(raw: dict[str, Any]) -> EnrichedProfile:
    employers = raw.get("current_employers") or []
    first = employers[0] if employers else {}
    domains = first.get("employer_company_website_domain") or []
    domain = domains[0] if isinstance(domains, list) and domains else None
    return EnrichedProfile(
        linkedin_url=raw.get("linkedin_flagship_url") or raw.get("linkedin_profile_url") or "",
        name=raw.get("name") or None,
        title=raw.get("title") or first.get("employee_title") or None,
        employer_name=first.get("employer_name"),
        employer_domain=domain,
        headline=raw.get("headline") or None,
        location=raw.get("location") or None,
        num_connections=raw.get("num_of_connections"),
        raw=raw,
    )


def enrich_profiles(linkedin_urls: list[str]) -> dict[str, EnrichedProfile]:
    """Look up multiple LinkedIn profiles in Crustdata's database.

    Batches up to 25 URLs per call. Returns a dict keyed by *requested* URL;
    URLs Crustdata couldn't match are absent from the dict (caller should
    treat them as no-match without erroring).

    Cost: 3 credits per matched profile (no charge for no-match).
    """
    if not linkedin_urls:
        return {}
    _check_circuit()
    out: dict[str, EnrichedProfile] = {}

    # Crustdata identifies returned profiles by `linkedin_flagship_url` (the
    # public slug form). Build a slug index so we can join responses back to
    # the URLs the caller asked for, even when they passed `/in/<urn>` form.
    def _slug(url: str) -> str:
        if "/in/" not in url:
            return url.lower()
        s = url.rsplit("/in/", 1)[-1].rstrip("/").lower()
        return s.split("?", 1)[0].split("#", 1)[0]

    slug_to_input: dict[str, str] = {_slug(u): u for u in linkedin_urls if u}

    with _client() as client:
        for i in range(0, len(linkedin_urls), _BATCH_SIZE):
            batch = linkedin_urls[i : i + _BATCH_SIZE]
            _process_batch(client, batch, out, slug_to_input, depth=0)
    return out


def _process_batch(
    client: httpx.Client,
    batch: list[str],
    out: dict[str, "EnrichedProfile"],
    slug_to_input: dict[str, str],
    *,
    depth: int,
) -> None:
    """Issue one Crustdata enrich call. On HTTP 400 (a single bad URL kills
    the whole batch), recursively halve and retry to isolate the bad one
    instead of losing every URL in the batch."""
    if not batch:
        return
    joined = ",".join(quote(u, safe=":/?&=") for u in batch)
    try:
        resp = client.get(f"{_ENRICH_PATH}?linkedin_profile_url={joined}")
    except httpx.RequestError as err:
        log.warning("crustdata enrich transport error: %s", err)
        return

    if resp.status_code in (401, 402):
        _trip_circuit()
        raise CrustdataEnrichQuotaExhausted(
            f"crustdata enrich {resp.status_code}: {resp.text[:300]}"
        )
    if resp.status_code == 429:
        _trip_circuit()
        raise CrustdataEnrichError(
            f"crustdata enrich 429 rate-limited: {resp.text[:300]}"
        )
    if resp.status_code == 404:
        # No matches in this batch (not a quota issue, just no data). Skip.
        return
    if resp.status_code == 400:
        # One bad URL kills the whole batch. Halve and retry to isolate it,
        # capped at depth 4 (25 → 12 → 6 → 3 → 1) so a single bad URL costs
        # at most ~9 extra calls, not 25.
        if len(batch) == 1 or depth >= 4:
            log.warning(
                "crustdata enrich 400 (depth=%d, giving up on %d URL(s)): %s",
                depth,
                len(batch),
                resp.text[:160],
            )
            return
        mid = len(batch) // 2
        _process_batch(client, batch[:mid], out, slug_to_input, depth=depth + 1)
        _process_batch(client, batch[mid:], out, slug_to_input, depth=depth + 1)
        return
    if resp.status_code >= 400:
        log.warning("crustdata enrich %d: %s", resp.status_code, resp.text[:200])
        return

    try:
        payload = resp.json()
    except ValueError:
        log.warning("crustdata enrich returned non-JSON")
        return

    if isinstance(payload, dict):
        payload = [payload]
    elif not isinstance(payload, list):
        return

    def _slug(url: str) -> str:
        if "/in/" not in url:
            return url.lower()
        s = url.rsplit("/in/", 1)[-1].rstrip("/").lower()
        return s.split("?", 1)[0].split("#", 1)[0]

    for raw in payload:
        if not isinstance(raw, dict):
            continue
        if raw.get("error") or raw.get("error_code"):
            continue
        profile = _parse_one(raw)
        returned_slug = _slug(profile.linkedin_url)
        input_url = slug_to_input.get(returned_slug)
        if input_url is None:
            raw_urn = raw.get("linkedin_profile_url") or ""
            input_url = slug_to_input.get(_slug(raw_urn)) or profile.linkedin_url
        out[input_url] = profile


def filter_likely_person_urls(urls: list[str]) -> tuple[list[str], int]:
    """Apply is_likely_person_slug to a batch; return (kept, dropped_count)."""
    kept: list[str] = []
    dropped = 0
    seen: set[str] = set()
    for u in urls:
        if not u or u in seen:
            continue
        seen.add(u)
        if is_likely_person_slug(u):
            kept.append(u)
        else:
            dropped += 1
    return kept, dropped
