"""Wiza Person Enrich (linkedin-engine port).

Provider used as the *first* enrichment tier in discovery:
    Wiza  →  Crustdata  →  Unipile /users/{slug} fallback (capped per-run).

Why Wiza first (decided 2026-05-14):
  - 1 credit/match (vs Crustdata's 3) and free on miss — strictly cheaper.
  - Returns `company_industry` inline at `enrichment_level: "none"`, so we
    do NOT need to follow up with APIDirect `/v1/linkedin/company` when
    Wiza matches. That replaces two paid calls (Crustdata 3 cr + APIDirect
    $0.006) with one (Wiza 1 cr).
  - Handles URN-form `/in/<urn-style-slug>` profiles Crustdata can't (verified
    via direct curl 2026-05-14 against Tarpan Patel and Yair Lurie).

Why not WizaAdapter from search-service:
  - That adapter is async (`httpx.AsyncClient`) and built around wave-fan-out
    for email reveal. linkedin-engine's discovery stage is sync. Porting the
    request/poll/parse logic 1:1 here (sync `httpx.Client` + ThreadPoolExecutor
    for concurrency) keeps both repos independent and avoids cross-repo coupling.
  - Response shape and field extraction match the search-service adapter
    exactly (`enrichment_level: "none"` response keys verified against live
    Wiza API on 2026-05-14).

Response shape at `enrichment_level: "none"` with `profile_url`:
  POST start →  {"data": {"id": <int>, "status": "queued", "is_complete": false}}
  GET  poll  →  {"data": {
      "id", "status" ('queued'|'processing'|'finished'|'failed'),
      "is_complete", "name", "title", "sub_title", "location",
      "company", "company_domain", "company_industry", "company_subindustry",
      "company_size", "company_size_range", "company_country", "company_region",
      "company_locality", "company_location", "company_description",
      "company_linkedin", "company_linkedin_id", "company_founded",
      "company_revenue_range", "linkedin_profile_url", "work_history",
      "credits": {"api_credits": {"total", "email_credits", "phone_credits",
                                  "scrape_credits"}},
      ...
  }}

Cost: 1 `scrape_credits` on a successful match, 0 on miss.

Concurrency / pacing:
  Wiza tolerates ~30 concurrent reveals per account (cited in the
  search-service adapter docstring). We default to a much more conservative 5
  here because we run this inline inside discovery's sync hot path — going
  wider risks a thundering herd of threads competing with the rest of the
  pipeline. Configurable via ``settings.wiza_max_concurrent_reveals``.

Quota / circuit: 401/402/429 trip a process-wide circuit; mirrors the same
pattern in ``apidirect.py`` and ``crustdata_enrich.py``.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


_BASE_URL = "https://wiza.co/api"
_START_PATH = "/individual_reveals"
_TIMEOUT = 30.0

# Per-reveal poll cadence + deadline. Wiza's level=none reveals are real-time
# (~5s typical). We poll every 2s with a 60s ceiling per reveal. Past the
# ceiling we abandon the reveal as `timeout` — Wiza usually doesn't charge
# for unmatched reveals, but if it did the lost credit is far cheaper than
# letting one queue-stuck reveal block the entire discovery batch.
_POLL_INTERVAL_S = 2.0
_POLL_TIMEOUT_S = 60.0


# Circuit breaker: 401/402/429 → open, requires worker restart to clear.
_circuit_open = False
_circuit_lock = threading.Lock()


class WizaEnrichError(RuntimeError):
    pass


class WizaEnrichNotConfigured(WizaEnrichError):
    pass


class WizaEnrichQuotaExhausted(WizaEnrichError):
    pass


@dataclass(frozen=True)
class WizaProfile:
    """Parsed Wiza response — superset of Crustdata's ``EnrichedProfile``.

    The discovery code adapts this into ``EnrichedProfile`` at the call
    boundary so both providers feed the existing cache-write path without
    branching on field availability."""
    linkedin_url: str
    name: str | None
    title: str | None
    headline: str | None         # Wiza's `sub_title`
    location: str | None
    employer_name: str | None    # Wiza's `company`
    employer_domain: str | None  # Wiza's `company_domain`
    employer_linkedin_id: str | None  # Wiza's `company_linkedin_id`
    employer_description: str | None  # Wiza's `company_description`
    company_industry: str | None
    company_subindustry: str | None
    company_size: int | None
    company_size_range: str | None
    company_country: str | None
    company_region: str | None
    company_location: str | None
    company_founded: int | None
    raw: dict[str, Any]


def _check_circuit() -> None:
    with _circuit_lock:
        if _circuit_open:
            raise WizaEnrichQuotaExhausted(
                "wiza quota / auth circuit open (401/402/429 observed). "
                "Top up credits and restart the worker to clear."
            )


def _trip_circuit() -> None:
    global _circuit_open
    with _circuit_lock:
        _circuit_open = True


def _client() -> httpx.Client:
    api_key = (settings.wiza_api_key or "").strip()
    if not api_key:
        raise WizaEnrichNotConfigured(
            "WIZA_API_KEY is not set. Add it to .env to enable Wiza enrichment."
        )
    return httpx.Client(
        base_url=_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        },
        timeout=_TIMEOUT,
    )


def _safe_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _parse_data_block(data: dict[str, Any]) -> WizaProfile:
    """Map Wiza's flat ``data`` block → ``WizaProfile``. Tolerant of None
    on every field — Wiza omits or nulls anything LinkedIn doesn't expose."""
    return WizaProfile(
        linkedin_url=(data.get("linkedin_profile_url") or "").strip(),
        name=(data.get("name") or None) and data["name"].strip(),
        title=(data.get("title") or None),
        headline=(data.get("sub_title") or None),
        location=(data.get("location") or None),
        employer_name=(data.get("company") or None),
        employer_domain=(data.get("company_domain") or None),
        employer_linkedin_id=(
            str(data["company_linkedin_id"]).strip()
            if data.get("company_linkedin_id") not in (None, "")
            else None
        ),
        employer_description=(data.get("company_description") or None),
        company_industry=(data.get("company_industry") or None),
        company_subindustry=(data.get("company_subindustry") or None),
        company_size=_safe_int(data.get("company_size")),
        company_size_range=(data.get("company_size_range") or None),
        company_country=(data.get("company_country") or None),
        company_region=(data.get("company_region") or None),
        company_location=(data.get("company_location") or None),
        company_founded=_safe_int(data.get("company_founded")),
        raw=data,
    )


def _handle_error_status(resp: httpx.Response, context: str) -> None:
    """Translate Wiza error responses into typed exceptions.

    Trips the circuit on 401/402/429 so the rest of the discovery batch
    doesn't keep paying ms-by-ms for failing reveals."""
    sc = resp.status_code
    if sc == 401:
        _trip_circuit()
        raise WizaEnrichError(f"wiza 401 auth ({context}): {resp.text[:300]}")
    if sc == 402:
        _trip_circuit()
        raise WizaEnrichQuotaExhausted(
            f"wiza 402 quota exhausted ({context}): {resp.text[:300]}"
        )
    if sc == 429:
        _trip_circuit()
        raise WizaEnrichError(f"wiza 429 rate-limit ({context}): {resp.text[:300]}")
    if sc >= 400:
        raise WizaEnrichError(f"wiza {sc} ({context}): {resp.text[:300]}")


def enrich_profile(linkedin_url: str) -> WizaProfile | None:
    """Look up ONE LinkedIn profile via Wiza at ``enrichment_level: "none"``.

    Cost: 1 credit on match, 0 on miss.

    Returns ``None`` when:
      - the URL is empty / not a /in/ profile,
      - Wiza fails to match the profile (status=failed or no LinkedIn URL
        in the response — i.e. Wiza didn't actually find a person),
      - the poll loop times out at 60s.

    Raises:
      ``WizaEnrichNotConfigured`` — missing API key.
      ``WizaEnrichQuotaExhausted`` — 402 hit, circuit open.
      ``WizaEnrichError`` — 401/429/other vendor error; caller decides whether
        to fall through to Crustdata (recommended).
    """
    if not linkedin_url or "/in/" not in linkedin_url:
        return None
    if settings.wiza_mock:
        return None  # mocks not implemented — discovery short-circuits to Crustdata.
    _check_circuit()

    body = {
        "individual_reveal": {"profile_url": linkedin_url.strip()},
        "enrichment_level": "none",
    }

    t0 = time.monotonic()
    with _client() as client:
        try:
            start_resp = client.post(_START_PATH, json=body)
        except httpx.HTTPError as err:
            raise WizaEnrichError(f"wiza transport error on POST: {err}") from err
        _handle_error_status(start_resp, "start")
        start = start_resp.json()
        reveal_id = (start.get("data") or {}).get("id")
        if not reveal_id:
            log.warning("wiza.enrich no reveal_id in start response url=%s", linkedin_url[:80])
            return None

        # Poll until terminal status or deadline.
        poll_path = f"{_START_PATH}/{reveal_id}"
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        final_data: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_S)
            try:
                poll_resp = client.get(poll_path)
            except httpx.HTTPError as err:
                # Transient — keep polling until deadline.
                log.debug("wiza.enrich transient poll error: %s", err)
                continue
            if poll_resp.status_code != 200:
                _handle_error_status(poll_resp, "poll")
                continue
            d = poll_resp.json()
            st = ((d.get("data") or {}).get("status") or "").lower()
            if st in ("finished", "failed"):
                final_data = d
                break

    elapsed_s = time.monotonic() - t0
    if not final_data:
        log.info(
            "wiza.enrich timeout after %.1fs url=%s",
            elapsed_s, linkedin_url[:80],
        )
        return None

    data = final_data.get("data") or {}
    status = (data.get("status") or "").lower()
    # Wiza marks 'failed' when it couldn't match anyone — treat as miss.
    if status != "finished":
        log.info(
            "wiza.enrich miss status=%s url=%s",
            status or "<empty>", linkedin_url[:80],
        )
        return None
    # Even on 'finished', if Wiza didn't return a LinkedIn URL it didn't
    # actually find the person we requested.
    if not data.get("linkedin_profile_url"):
        log.info("wiza.enrich finished-but-empty url=%s", linkedin_url[:80])
        return None

    # Cost accounting — Wiza only charges on actual matches (the `finished`
    # + linkedin_profile_url path). We record per match rather than reading
    # `data.credits.api_credits.total` because the credit field has been
    # observed to occasionally lag the billing system on retries; the
    # contract is "1 scrape credit per matched reveal" which is what we
    # bill against.
    try:
        from app.services import cost_tracker
        cost_tracker.record_wiza_match()
    except Exception as err:  # noqa: BLE001
        log.debug("wiza.enrich: cost record skipped: %s", err)

    return _parse_data_block(data)


def enrich_profiles(linkedin_urls: list[str]) -> dict[str, WizaProfile]:
    """Batch helper — fan out one-reveal-per-URL across a thread pool.

    Wiza has no batch endpoint, so this loops `enrich_profile` in parallel.
    Concurrency capped at ``settings.wiza_max_concurrent_reveals`` (default 5)
    to avoid swamping the discovery worker.

    Returns a dict keyed by the **requested** URL. Misses are absent. Per-URL
    exceptions are logged + swallowed so a single bad input never aborts
    the whole batch — *except* for ``WizaEnrichQuotaExhausted``, which we
    re-raise so the caller can stop trying for the rest of the slate run.
    """
    if not linkedin_urls:
        return {}
    _check_circuit()

    # Dedup while preserving order so logs match what the caller passed.
    seen: set[str] = set()
    deduped: list[str] = []
    for u in linkedin_urls:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    if not deduped:
        return {}

    out: dict[str, WizaProfile] = {}
    n_matched = 0
    n_miss = 0
    quota_err: WizaEnrichQuotaExhausted | None = None
    max_workers = max(1, int(getattr(settings, "wiza_max_concurrent_reveals", 5)))

    log.info(
        "wiza.enrich.start profile_count=%d concurrency=%d",
        len(deduped), max_workers,
    )

    # ── Cost-tracker context propagation ───────────────────────────────────
    # `cost_tracker.record_wiza_match()` reads the slate_run_id from a
    # ContextVar + threading.local. ThreadPoolExecutor worker threads do
    # NOT inherit either from the parent — so without this propagation,
    # every Wiza match is recorded against `slate_run_id=None` and the
    # `_record()` no-ops silently. Result before this fix: real credit
    # spent, $0.00 reported on the slate. Capture the parent's context
    # once, then re-attach it inside each worker via a wrapper.
    from app.services.cost_tracker import (
        get_current_slate_run as _get_sid,
        set_current_slate_run as _set_sid,
    )
    _parent_sid = _get_sid()

    def _enrich_with_ctx(url: str):
        if _parent_sid is not None:
            _set_sid(_parent_sid)
        return enrich_profile(url)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wiza") as ex:
        future_to_url = {ex.submit(_enrich_with_ctx, u): u for u in deduped}
        for fut in as_completed(future_to_url):
            url = future_to_url[fut]
            try:
                profile = fut.result()
            except WizaEnrichQuotaExhausted as err:
                # Latch the first quota error; let the rest of the futures
                # settle (they'll short-circuit on the circuit-open check)
                # then re-raise.
                if quota_err is None:
                    quota_err = err
                continue
            except WizaEnrichError as err:
                log.warning("wiza.enrich error url=%s err=%s", url[:80], err)
                n_miss += 1
                continue
            except Exception as err:  # noqa: BLE001 — never let one bad URL kill the batch
                log.warning("wiza.enrich unexpected url=%s err=%s", url[:80], err)
                n_miss += 1
                continue
            if profile is None:
                n_miss += 1
                continue
            out[url] = profile
            n_matched += 1

    log.info(
        "wiza.enrich.done matched=%d miss=%d (1 credit/match)",
        n_matched, n_miss,
    )

    if quota_err is not None:
        raise quota_err
    return out
