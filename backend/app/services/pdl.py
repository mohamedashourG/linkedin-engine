"""
People Data Labs (PDL) profile-enrichment client.

PDL's `/v5/person/enrich` takes a LinkedIn URL (and/or name) and returns the
author's job_title, job_company_name, location, etc. We use it to fill the gap
where apidirect's search response only gives us a name — without title/company,
the ICP scorer has nothing concrete to score against.

Costs ~$0.05–$0.30 per match. The engine calls PDL once per candidate that
makes it past verification, so a ~10-candidate run is well under $5/day.

Cache: per-process LRU keyed on linkedin_url. Within one daily_run we'll only
hit each URL once anyway, but multiple cofounders sharing a candidate would
otherwise pay twice.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://api.peopledatalabs.com"
_TIMEOUT = 20.0


class PDLNotConfigured(RuntimeError):
    pass


class PDLError(RuntimeError):
    pass


@dataclass(frozen=True)
class PDLProfile:
    job_title: str | None
    job_company_name: str | None
    job_company_size: str | None
    location_country: str | None
    full_name: str | None
    headline: str | None


def _client() -> httpx.Client:
    if not settings.pdl_api_key:
        raise PDLNotConfigured("PDL_API_KEY is not set.")
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"X-Api-Key": settings.pdl_api_key},
        timeout=_TIMEOUT,
    )


@lru_cache(maxsize=512)
def enrich_by_linkedin_url(linkedin_url: str) -> PDLProfile | None:
    """Returns None when PDL has no record (404) or returns a low-likelihood match."""
    if not linkedin_url:
        return None
    try:
        with _client() as client:
            resp = client.get(
                "/v5/person/enrich",
                params={"profile": linkedin_url, "min_likelihood": 6},
            )
    except httpx.RequestError as err:
        log.warning("pdl transport error for %s: %s", linkedin_url, err)
        return None

    if resp.status_code == 404:
        return None
    if resp.status_code == 401:
        raise PDLError(f"PDL 401: {resp.text[:300]}")
    if resp.status_code == 402:
        raise PDLError(f"PDL 402 quota: {resp.text[:300]}")
    if resp.status_code == 429:
        log.warning("pdl 429 rate-limited for %s", linkedin_url)
        return None
    if resp.status_code >= 400:
        log.warning("pdl %d for %s: %s", resp.status_code, linkedin_url, resp.text[:200])
        return None

    payload = resp.json() or {}
    data = payload.get("data") or {}
    if not data:
        return None
    return PDLProfile(
        job_title=data.get("job_title"),
        job_company_name=data.get("job_company_name"),
        job_company_size=data.get("job_company_size"),
        location_country=data.get("location_country"),
        full_name=data.get("full_name"),
        headline=data.get("headline") or data.get("job_title"),
    )
