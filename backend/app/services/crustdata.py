"""
Crustdata Watcher API client (`linkedin-post-with-keyword` event).

Crustdata's Watcher API is webhook-driven: you register a watch describing the
filter universe (keyword expression + author title + author company + author
location + industry + post intent + headcount + lead filters) and Crustdata
POSTs matching posts to a notification_endpoint as they happen ("at least 1
hour" cadence per their docs).

**Simulation watches** (`POST /watcher/simulation/watches`) use the same core
fields as production (`event_type_slug`, `event_filters`, `account_filters`,
`lead_filters`, `notification_endpoint`, `frequency`, `expiration_date`,
`max_notifications_per_execution`) and deliver an example notification to the
endpoint immediately. Per Crustdata, `max_notifications_per_execution` must be a
positive multiple of 50. Production watches may additionally send
`approximate_notification_time`; simulation requests omit it to match their API
examples.

Event mapping → engine ICP rubric:
    KEYWORD          ← cofounder.tier_1 + tier_2 keywords (boolean expression)
    AUTHOR_TITLE     ← operator's tier-1 hard-required titles
    AUTHOR_COMPANY   ← operator's tracked-company LinkedIn URLs (optional)
    AUTHOR_LOCATION  ← operator's geo target (single value)
    INDUSTRY         ← operator's industry list
    POST_INTENT      ← short prompt derived from product_extracted
    COMPANY_HEADCOUNT← operator's stage_tier headcount buckets (account filter)
    PAST_TITLE       ← optional, lead filter

Webhook URL shape: `<base_url>?cofounder_id=<id>&token=<hmac>` — token is a
base64 HMAC-SHA256 of the cofounder_id keyed by `CRUSTDATA_WEBHOOK_SECRET`,
verified at receive time so random spam can't write to our inbox.

Quota / circuit: Crustdata is paid per call; 401/402/429 trip a process-wide
circuit so a misconfigured run doesn't burn credits.
"""
from __future__ import annotations

import base64
import hashlib
import json
import hmac
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urljoin

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Redact HMAC token in webhook URLs before logging.
_TOKEN_IN_URL_RE = re.compile(r"([?&])token=[^&]*", re.IGNORECASE)


def redact_notification_url(url: str | None) -> str:
    if not url:
        return ""
    return _TOKEN_IN_URL_RE.sub(r"\1token=***", str(url))


def _redact_payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    """Shallow copy for logging — masks ``notification_endpoint`` token."""
    out = dict(payload)
    ne = out.get("notification_endpoint")
    if isinstance(ne, str):
        out["notification_endpoint"] = redact_notification_url(ne)
    return out


_BASE_URL = "https://api.crustdata.com"
_TIMEOUT = 60.0
# Realtime screener endpoints (keyword + person posts) are slower than watcher.
_SCREENER_TIMEOUT = 120.0
_KEYWORD_EVENT_SLUG = "linkedin-post-with-keyword"
_PRODUCTION_PATH = "/watcher/watches"
_SIMULATION_PATH = "/watcher/simulation/watches"

_circuit_open = False
_circuit_lock = threading.Lock()


class CrustdataError(RuntimeError):
    pass


class CrustdataNotConfigured(CrustdataError):
    pass


class CrustdataQuotaExhausted(CrustdataError):
    pass


@dataclass(frozen=True)
class CrustdataWatchSpec:
    """Filter shape for a `linkedin-post-with-keyword` watch.

    Crustdata expects each filter as `{filter_type, type, value}` (or just
    `{filter_type}` for boolean toggles). We assemble these from a higher-level
    spec so callers don't hand-roll the wire format.
    """

    keyword_expression: str  # Boolean syntax: "X AND (Y OR Z) NOT W"
    actor_types: list[str] | None = None  # ["person"], ["company"], or both
    author_titles: list[str] | None = None
    author_company_urls: list[str] | None = None
    author_location: str | None = None  # single region per Crustdata's max
    industries: list[str] | None = None
    post_intent: str | None = None
    post_categories: list[str] | None = None
    fetch_reactors: bool = False
    detailed_reactor_data: bool = False
    headcount_buckets: list[str] | None = None
    company_hq_country: str | None = None
    past_company: list[str] | None = None
    past_title: list[str] | None = None

    def to_event_filters(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {
                "filter_type": "KEYWORD",
                "type": "in",
                "value": [self.keyword_expression],
            }
        ]
        if self.actor_types:
            out.append(
                {"filter_type": "ACTOR_TYPE", "type": "in", "value": self.actor_types}
            )
        if self.author_titles:
            out.append(
                {
                    "filter_type": "AUTHOR_TITLE",
                    "type": "in",
                    "value": self.author_titles,
                }
            )
        if self.author_company_urls:
            out.append(
                {
                    "filter_type": "AUTHOR_COMPANY",
                    "type": "in",
                    "value": self.author_company_urls,
                }
            )
        if self.author_location:
            out.append(
                {
                    "filter_type": "AUTHOR_LOCATION",
                    "type": "in",
                    "value": [self.author_location],
                }
            )
        if self.industries:
            out.append(
                {"filter_type": "INDUSTRY", "type": "in", "value": self.industries}
            )
        if self.post_intent:
            out.append(
                {
                    "filter_type": "POST_INTENT",
                    "type": "in",
                    "value": [self.post_intent],
                }
            )
        if self.post_categories:
            out.append(
                {
                    "filter_type": "POST_CATEGORY",
                    "type": "in",
                    "value": self.post_categories[:10],
                }
            )
        if self.fetch_reactors:
            out.append({"filter_type": "REACTORS"})
        if self.detailed_reactor_data:
            out.append({"filter_type": "DETAILED_REACTOR_DATA"})
        return out

    def to_account_filters(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.headcount_buckets:
            out.append(
                {
                    "filter_type": "COMPANY_HEADCOUNT",
                    "type": "in",
                    "value": self.headcount_buckets,
                }
            )
        if self.company_hq_country:
            out.append(
                {
                    "filter_type": "COMPANY_HQ_COUNTRY",
                    "type": "in",
                    "value": [self.company_hq_country],
                }
            )
        return out

    def to_lead_filters(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.past_company:
            out.append(
                {"filter_type": "PAST_COMPANY", "type": "in", "value": self.past_company}
            )
        if self.past_title:
            out.append(
                {"filter_type": "PAST_TITLE", "type": "in", "value": self.past_title}
            )
        return out


def webhook_token_for(cofounder_id: str) -> str:
    """Derive the per-cofounder HMAC token used to sign the webhook URL."""
    secret = (settings.crustdata_webhook_secret or "").encode("utf-8")
    if not secret:
        raise CrustdataNotConfigured("CRUSTDATA_WEBHOOK_SECRET is empty")
    digest = hmac.new(secret, cofounder_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_webhook_token(cofounder_id: str, token: str) -> bool:
    if not token:
        return False
    try:
        expected = webhook_token_for(cofounder_id)
    except CrustdataNotConfigured:
        return False
    return hmac.compare_digest(expected, token)


def webhook_url_for(cofounder_id: str) -> str:
    base = (settings.crustdata_webhook_base_url or "").rstrip("/")
    if not base:
        raise CrustdataNotConfigured(
            "CRUSTDATA_WEBHOOK_BASE_URL is empty. In dev, use simulation mode "
            "or set this to a public URL (ngrok). The path /api/webhooks/crustdata "
            "will be appended automatically."
        )
    qs = urlencode({"cofounder_id": cofounder_id, "token": webhook_token_for(cofounder_id)})
    return f"{base}/api/webhooks/crustdata?{qs}"


def _check_circuit() -> None:
    with _circuit_lock:
        if _circuit_open:
            log.warning(
                "crustdata: watcher API call blocked — circuit open "
                "(prior 401/402/429); restart worker after fixing credentials."
            )
            raise CrustdataQuotaExhausted(
                "Crustdata circuit is open (prior 401/402/429). Restart worker "
                "after fixing credentials/credit."
            )


def _trip_circuit() -> None:
    global _circuit_open
    with _circuit_lock:
        if not _circuit_open:
            log.warning("crustdata: watcher circuit tripped — further watcher calls blocked until worker restart")
        _circuit_open = True


def _client() -> httpx.Client:
    if not settings.crustdata_api_key:
        raise CrustdataNotConfigured("CRUSTDATA_API_KEY is not set.")
    return httpx.Client(
        base_url=_BASE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Token {settings.crustdata_api_key}",
        },
        timeout=_TIMEOUT,
    )


def _validate_max_notifications_per_execution(n: int) -> int:
    """Crustdata simulation API: must be > 0 and a multiple of 50."""
    if n <= 0 or n % 50 != 0:
        raise CrustdataError(
            "max_notifications_per_execution must be > 0 and a multiple of 50 "
            f"(Crustdata /watcher/simulation/watches); got {n}"
        )
    return n


def _post(
    path: str,
    payload: dict[str, Any],
    *,
    log_label: str | None = None,
) -> dict[str, Any]:
    """POST JSON to Crustdata. When ``log_label`` is set (e.g. watcher register),
    logs full request JSON and full response body to Docker/backend logs."""
    if log_label:
        log.info(
            "%s.request method=POST base=%s path=%s body=%s",
            log_label,
            _BASE_URL,
            path,
            json.dumps(
                _redact_payload_for_log(payload),
                ensure_ascii=False,
                default=str,
            ),
        )
    _check_circuit()
    with _client() as client:
        try:
            resp = client.post(path, json=payload)
        except httpx.RequestError as err:
            if log_label:
                log.warning("%s.transport_error err=%s", log_label, err)
            raise CrustdataError(f"crustdata transport error: {err}") from err
    if log_label:
        preview = resp.text
        if len(preview) > 4000:
            preview = preview[:4000] + "…[truncated]"
        log.info(
            "%s.http_response status=%s body=%s",
            log_label,
            resp.status_code,
            preview,
        )
    if resp.status_code in (401, 402):
        _trip_circuit()
        log.warning(
            "crustdata._post path=%s status=%s (circuit open) body_prefix=%r",
            path,
            resp.status_code,
            resp.text[:300],
        )
        raise CrustdataQuotaExhausted(
            f"crustdata {resp.status_code}: {resp.text[:300]}"
        )
    if resp.status_code == 429:
        _trip_circuit()
        log.warning(
            "crustdata._post path=%s 429 body_prefix=%r",
            path,
            resp.text[:300],
        )
        raise CrustdataError(f"crustdata 429 rate-limited: {resp.text[:300]}")
    if resp.status_code >= 400:
        raise CrustdataError(f"crustdata {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def register_simulation_watch(
    *,
    event_type_slug: str,
    event_filters: list[dict[str, Any]],
    account_filters: list[dict[str, Any]] | None = None,
    lead_filters: list[dict[str, Any]] | None = None,
    notification_endpoint: str,
    frequency: int = 1,
    expiration_date: date | None = None,
    max_notifications_per_execution: int = 50,
    log_label: str | None = "crustdata.simulation_watch",
) -> dict[str, Any]:
    """POST `/watcher/simulation/watches` — instant sample notification to the URL.

    Use for integration tests or any event slug (e.g.
    ``job-posting-with-keyword-and-location``). Payload matches Crustdata's
    documented shape; omits ``approximate_notification_time`` (production-only).
    """
    if expiration_date is None:
        expiration_date = date.today() + timedelta(
            days=settings.crustdata_default_expiration_days
        )
    max_n = _validate_max_notifications_per_execution(max_notifications_per_execution)
    payload: dict[str, Any] = {
        "event_type_slug": event_type_slug,
        "event_filters": event_filters,
        "account_filters": list(account_filters or []),
        "lead_filters": list(lead_filters or []),
        "notification_endpoint": notification_endpoint,
        "frequency": frequency,
        "expiration_date": expiration_date.isoformat(),
        "max_notifications_per_execution": max_n,
    }
    return _post(_SIMULATION_PATH, payload, log_label=log_label)


def register_keyword_watch(
    *,
    cofounder_id: str,
    spec: CrustdataWatchSpec,
    notification_endpoint: str | None = None,
    expiration_date: date | None = None,
    frequency: int = 1,
    approximate_notification_time: int = 4,
    max_notifications_per_execution: int = 50,
    simulation: bool = False,
) -> dict[str, Any]:
    """Create a Crustdata watch on the keyword-post event.

    Returns the raw Crustdata response. ``simulation=True`` uses
    `/watcher/simulation/watches` (instant sample POST to ``notification_endpoint``)
    with the documented simulation payload. Production uses `/watcher/watches`
    and includes ``approximate_notification_time``.
    """
    if notification_endpoint is None:
        notification_endpoint = webhook_url_for(cofounder_id)
    if expiration_date is None:
        expiration_date = date.today() + timedelta(
            days=settings.crustdata_default_expiration_days
        )

    log.info(
        "crustdata.register: cofounder=%s simulation=%s path=%s notification_endpoint=%s",
        cofounder_id,
        simulation,
        _SIMULATION_PATH if simulation else _PRODUCTION_PATH,
        redact_notification_url(notification_endpoint),
    )

    if simulation:
        return register_simulation_watch(
            event_type_slug=_KEYWORD_EVENT_SLUG,
            event_filters=spec.to_event_filters(),
            account_filters=spec.to_account_filters(),
            lead_filters=spec.to_lead_filters(),
            notification_endpoint=notification_endpoint,
            frequency=frequency,
            expiration_date=expiration_date,
            max_notifications_per_execution=max_notifications_per_execution,
            log_label=f"crustdata.watch[{cofounder_id}]",
        )

    payload: dict[str, Any] = {
        "event_type_slug": _KEYWORD_EVENT_SLUG,
        "event_filters": spec.to_event_filters(),
        "account_filters": spec.to_account_filters(),
        "lead_filters": spec.to_lead_filters(),
        "notification_endpoint": notification_endpoint,
        "frequency": frequency,
        "expiration_date": expiration_date.isoformat(),
        "approximate_notification_time": approximate_notification_time,
        "max_notifications_per_execution": max_notifications_per_execution,
    }
    return _post(
        _PRODUCTION_PATH,
        payload,
        log_label=f"crustdata.watch[{cofounder_id}]",
    )


def list_watches() -> list[dict[str, Any]]:
    _check_circuit()
    log.info(
        "crustdata.list_watches.request method=GET base=%s path=%s",
        _BASE_URL,
        _PRODUCTION_PATH,
    )
    with _client() as client:
        try:
            resp = client.get(_PRODUCTION_PATH)
        except httpx.RequestError as err:
            log.warning("crustdata.list_watches.transport_error err=%s", err)
            raise CrustdataError(f"crustdata transport error: {err}") from err
    if resp.status_code in (401, 402):
        _trip_circuit()
        log.warning(
            "crustdata.list_watches.response status=%s body_prefix=%r",
            resp.status_code,
            resp.text[:400],
        )
        raise CrustdataQuotaExhausted(
            f"crustdata {resp.status_code}: {resp.text[:300]}"
        )
    # Crustdata returns 404 "Watch not found" instead of an empty list when
    # no watches exist for this account. Treat that as "no watches" rather
    # than as an error.
    if resp.status_code == 404:
        log.info(
            "crustdata.list_watches.response status=404 (treating as empty list)"
        )
        return []
    if resp.status_code >= 400:
        log.warning(
            "crustdata.list_watches.response status=%s body_prefix=%r",
            resp.status_code,
            resp.text[:500],
        )
        raise CrustdataError(f"crustdata {resp.status_code}: {resp.text[:300]}")
    body = resp.json() if resp.content else []
    if isinstance(body, dict):
        watches = body.get("watches") or body.get("results") or []
    else:
        watches = body or []
    log.info(
        "crustdata.list_watches.response status=%s watch_count=%d",
        resp.status_code,
        len(watches),
    )
    return watches


def _first_query_value(qs: dict[str, list[str]], key: str) -> str | None:
    vals = qs.get(key)
    if not vals:
        return None
    v = vals[0]
    return v if v is not None else None


def notification_endpoint_matches_cofounder(
    endpoint: str | None, cofounder_id: str
) -> bool:
    """True when URL query ``cofounder_id`` and ``token`` match ``webhook_url_for``."""
    if not endpoint or not str(endpoint).strip():
        return False
    try:
        expected_url = webhook_url_for(cofounder_id)
    except CrustdataNotConfigured:
        return False
    ep_q = parse_qs(urlparse(str(endpoint).strip()).query, keep_blank_values=True)
    ex_q = parse_qs(urlparse(expected_url).query, keep_blank_values=True)
    if _first_query_value(ep_q, "cofounder_id") != cofounder_id:
        return False
    return _first_query_value(ep_q, "token") == _first_query_value(ex_q, "token")


def _watch_notification_endpoint(watch: dict[str, Any]) -> str | None:
    ep = watch.get("notification_endpoint")
    if isinstance(ep, str) and ep.strip():
        return ep.strip()
    n = watch.get("notification")
    if isinstance(n, dict):
        inner = n.get("endpoint") or n.get("url")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return None


def _extract_watch_id(watch: dict[str, Any]) -> str | None:
    for key in ("id", "watch_id", "uuid"):
        v = watch.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _keyword_expression_from_watch(watch: dict[str, Any]) -> str | None:
    kwe = watch.get("keyword_expression")
    if isinstance(kwe, str) and kwe.strip():
        return kwe.strip()
    ev = watch.get("event_filters")
    if isinstance(ev, list):
        for f in ev:
            if not isinstance(f, dict):
                continue
            if f.get("filter_type") == "KEYWORD":
                val = f.get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def find_reconciled_production_watch(cofounder_id: str) -> dict[str, Any] | None:
    """Remote production watch whose notification URL matches ``webhook_url_for``.

    Returns ``{"watch_id": str, "keyword_expression": str | None}``, or None.
    """
    log.info(
        "crustdata.reconcile.start cofounder=%s (GET list_watches then match endpoint)",
        cofounder_id,
    )
    remote = list_watches()
    log.info(
        "crustdata.reconcile.list_result cofounder=%s remote_watch_count=%d",
        cofounder_id,
        len(remote),
    )
    matches: list[dict[str, Any]] = []
    for w in remote:
        ep = _watch_notification_endpoint(w)
        if not notification_endpoint_matches_cofounder(ep, cofounder_id):
            continue
        wid = _extract_watch_id(w)
        if wid:
            kwx = _keyword_expression_from_watch(w)
            matches.append({"watch_id": wid, "keyword_expression": kwx})
    if not matches:
        log.info(
            "crustdata.reconcile.done cofounder=%s matched=no (will POST new watch if caller registers)",
            cofounder_id,
        )
        return None
    if len(matches) > 1:
        log.warning(
            "crustdata reconcile: cofounder=%s matched %d watches, using watch_id=%s",
            cofounder_id,
            len(matches),
            matches[0]["watch_id"],
        )
    log.info(
        "crustdata.reconcile.done cofounder=%s matched=yes watch_id=%s",
        cofounder_id,
        matches[0]["watch_id"],
    )
    return matches[0]


def delete_watch(watch_id: str | int) -> bool:
    path = f"{_PRODUCTION_PATH}/{watch_id}"
    log.info(
        "crustdata.delete_watch.request method=DELETE base=%s path=%s",
        _BASE_URL,
        path,
    )
    _check_circuit()
    with _client() as client:
        try:
            resp = client.delete(path)
        except httpx.RequestError as err:
            log.warning("crustdata.delete_watch.transport_error err=%s", err)
            raise CrustdataError(f"crustdata transport error: {err}") from err
    if resp.status_code in (401, 402):
        _trip_circuit()
        log.warning(
            "crustdata.delete_watch.response watch_id=%s status=%s",
            watch_id,
            resp.status_code,
        )
        raise CrustdataQuotaExhausted(
            f"crustdata {resp.status_code}: {resp.text[:300]}"
        )
    if resp.status_code == 404:
        log.info(
            "crustdata.delete_watch.response watch_id=%s status=404 (already gone)",
            watch_id,
        )
        return False
    if resp.status_code >= 400:
        log.warning(
            "crustdata.delete_watch.response watch_id=%s status=%s body_prefix=%r",
            watch_id,
            resp.status_code,
            resp.text[:300],
        )
        raise CrustdataError(f"crustdata {resp.status_code}: {resp.text[:300]}")
    log.info("crustdata.delete_watch.response watch_id=%s status=%s ok", watch_id, resp.status_code)
    return True


# ---------------------------------------------------------------- realtime screener
# POST /screener/linkedin_posts/keyword_search/  — keyword + optional filters
# GET  /screener/linkedin_posts                     — posts for one profile URL


def _screener_headers() -> dict[str, str]:
    if not settings.crustdata_api_key:
        raise CrustdataNotConfigured("CRUSTDATA_API_KEY is not set.")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Token {settings.crustdata_api_key}",
        "Accept": "application/json, text/plain, */*",
    }


def _screener_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    _check_circuit()
    kw = (payload.get("keyword") or "")[:80]
    log.info(
        "crustdata.screener.request method=POST base=%s path=%s keyword_prefix=%r limit=%s",
        _BASE_URL,
        path,
        kw,
        payload.get("limit"),
    )
    with httpx.Client(
        base_url=_BASE_URL,
        headers=_screener_headers(),
        timeout=_SCREENER_TIMEOUT,
    ) as client:
        try:
            resp = client.post(path, json=payload)
        except httpx.RequestError as err:
            log.warning("crustdata.screener.transport_error path=%s err=%s", path, err)
            raise CrustdataError(f"crustdata screener transport error: {err}") from err
    if resp.status_code in (401, 402):
        _trip_circuit()
        log.warning(
            "crustdata.screener.response POST path=%s status=%s (quota/auth)",
            path,
            resp.status_code,
        )
        raise CrustdataQuotaExhausted(
            f"crustdata screener {resp.status_code}: {resp.text[:300]}"
        )
    if resp.status_code == 429:
        _trip_circuit()
        log.warning("crustdata.screener.response POST path=%s status=429", path)
        raise CrustdataError(f"crustdata screener 429: {resp.text[:300]}")
    if resp.status_code == 404:
        log.info("crustdata.screener.response POST path=%s status=404 empty", path)
        return {}
    if resp.status_code >= 400:
        log.warning(
            "crustdata.screener.response path=%s status=%s body_prefix=%r",
            path,
            resp.status_code,
            resp.text[:400],
        )
        raise CrustdataError(f"crustdata screener {resp.status_code}: {resp.text[:400]}")
    if not resp.content:
        log.info("crustdata.screener.response path=%s status=%s empty_body", path, resp.status_code)
        return {}
    try:
        body = resp.json()
    except ValueError:
        log.warning("crustdata.screener.response path=%s non_json", path)
        return {}
    # Crustdata sometimes returns a top-level JSON array of posts (not wrapped).
    if isinstance(body, list):
        body = {"posts": body}
    if not isinstance(body, dict):
        return {}
    n_posts = len(_screener_extract_posts(body))
    log.info(
        "crustdata.screener.response path=%s status=%s posts_extracted=%d",
        path,
        resp.status_code,
        n_posts,
    )
    return body


def _screener_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    _check_circuit()
    url_hint = (params.get("person_linkedin_url") or "")[:100]
    log.info(
        "crustdata.screener.request method=GET base=%s path=%s person_url_prefix=%r",
        _BASE_URL,
        path,
        url_hint,
    )
    with httpx.Client(
        base_url=_BASE_URL,
        headers=_screener_headers(),
        timeout=_SCREENER_TIMEOUT,
    ) as client:
        try:
            resp = client.get(path, params=params)
        except httpx.RequestError as err:
            log.warning("crustdata.screener.transport_error path=%s err=%s", path, err)
            raise CrustdataError(f"crustdata screener transport error: {err}") from err
    if resp.status_code in (401, 402):
        _trip_circuit()
        log.warning(
            "crustdata.screener.response GET path=%s status=%s (quota/auth)",
            path,
            resp.status_code,
        )
        raise CrustdataQuotaExhausted(
            f"crustdata screener {resp.status_code}: {resp.text[:300]}"
        )
    if resp.status_code == 429:
        _trip_circuit()
        log.warning("crustdata.screener.response GET path=%s status=429", path)
        raise CrustdataError(f"crustdata screener 429: {resp.text[:300]}")
    if resp.status_code == 404:
        log.info("crustdata.screener.response GET path=%s status=404 empty", path)
        return {}
    if resp.status_code >= 400:
        log.warning(
            "crustdata.screener.response path=%s status=%s body_prefix=%r",
            path,
            resp.status_code,
            resp.text[:400],
        )
        raise CrustdataError(f"crustdata screener {resp.status_code}: {resp.text[:400]}")
    if not resp.content:
        log.info("crustdata.screener.response path=%s status=%s empty_body", path, resp.status_code)
        return {}
    try:
        body = resp.json()
    except ValueError:
        log.warning("crustdata.screener.response path=%s non_json", path)
        return {}
    if isinstance(body, list):
        body = {"posts": body}
    if not isinstance(body, dict):
        return {}
    n_posts = len(_screener_extract_posts(body))
    log.info(
        "crustdata.screener.response path=%s status=%s posts_extracted=%d",
        path,
        resp.status_code,
        n_posts,
    )
    return body


def _screener_extract_posts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("posts", "results", "data", "items"):
            block = payload.get(key)
            if isinstance(block, list):
                return [x for x in block if isinstance(x, dict)]
    return []


def author_profile_url_from_screener_post(raw: dict[str, Any]) -> str | None:
    """Best-effort author /in/ URL from a Crustdata screener post object."""
    for key in (
        "person_linkedin_flagship_profile_url",
        "author_linkedin_url",
        "linkedin_profile_url",
        "person_linkedin_url",
    ):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    hyp = raw.get("hyperlinks")
    if isinstance(hyp, dict):
        urls = hyp.get("person_linkedin_urls") or []
        if urls and isinstance(urls[0], str) and urls[0].strip():
            return urls[0].strip()
    actor = raw.get("actor")
    if isinstance(actor, dict):
        for key in (
            "linkedin_flagship_url",
            "flagship_profile_url",
            "profile_url",
            "url",
        ):
            v = actor.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def screener_keyword_search_posts(
    *,
    keyword: str,
    limit: int = 20,
    page: int | None = None,
    date_posted: str = "past-month",
    filters: list[dict[str, Any]] | None = None,
    exact_keyword_match: bool | None = None,
) -> list[dict[str, Any]]:
    """POST ``/screener/linkedin_posts/keyword_search/`` — realtime LinkedIn posts.

    When ``page`` is set, Crustdata caps ``limit`` at 5. For bulk retrieval omit
    ``page`` and pass ``limit`` up to 500."""
    kw = (keyword or "").strip()
    if not kw:
        return []
    body: dict[str, Any] = {"keyword": kw[:400], "date_posted": date_posted}
    if page is not None:
        body["page"] = int(page)
        body["limit"] = min(int(limit), 5)
    else:
        body["limit"] = min(max(int(limit), 1), 500)
    if filters:
        body["filters"] = filters
    if exact_keyword_match is not None:
        body["exact_keyword_match"] = bool(exact_keyword_match)
    payload = _screener_post("/screener/linkedin_posts/keyword_search/", body)
    return _screener_extract_posts(payload)


def screener_person_posts(
    *,
    person_linkedin_url: str,
    limit: int = 5,
    page: int | None = None,
) -> list[dict[str, Any]]:
    """GET ``/screener/linkedin_posts`` — recent posts for a single profile URL."""
    url = (person_linkedin_url or "").strip()
    if not url:
        return []
    params: dict[str, Any] = {"person_linkedin_url": url}
    if page is not None:
        params["page"] = int(page)
        params["limit"] = min(int(limit), 5)
    else:
        params["limit"] = min(max(int(limit), 1), 100)
    payload = _screener_get("/screener/linkedin_posts", params)
    return _screener_extract_posts(payload)


def screener_posts_for_members(
    *,
    member_profile_urls: list[str],
    keyword: str = "a",
    limit: int = 40,
    date_posted: str = "past-month",
    max_members: int = 12,
) -> list[dict[str, Any]]:
    """Keyword search with ``MEMBER`` filter — one call for several ``/in/`` URLs.

    Crustdata allows a placeholder keyword (e.g. ``\"a\"``) when filtering by
    specific members."""
    seen: set[str] = set()
    urls: list[str] = []
    for u in member_profile_urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        urls.append(u)
    if not urls:
        return []
    urls = urls[: max(1, int(max_members))]
    filters: list[dict[str, Any]] = [
        {"filter_type": "MEMBER", "type": "in", "value": urls},
    ]
    return screener_keyword_search_posts(
        keyword=keyword[:400] or "a",
        limit=limit,
        page=None,
        date_posted=date_posted,
        filters=filters,
    )


def build_keyword_expression(
    tier_1: list[str],
    tier_2: list[str] | None = None,
    *,
    max_boolean_operators: int | None = None,
) -> str:
    """Compose Crustdata's boolean keyword grammar from operator keyword tiers.

    Crustdata supports AND/OR/NOT (uppercase), parentheses, and quoted phrases.
    We OR all tier_1 + tier_2 keywords; phrases with spaces are quoted.
    Multi-word phrases are quoted; everything else passed bare.

    ``max_boolean_operators`` caps how many boolean operators appear in the
    expression. For an OR-only chain, ``n`` terms use ``n - 1`` OR operators;
    e.g. ``max_boolean_operators=5`` allows at most 6 terms. ``None`` means no cap.
    """
    pieces: list[str] = []
    seen: set[str] = set()
    max_terms: int | None = None
    if max_boolean_operators is not None:
        max_terms = max(1, max_boolean_operators + 1)
    for kw in (tier_1 or []) + (tier_2 or []):
        if max_terms is not None and len(pieces) >= max_terms:
            log.info(
                "crustdata: keyword OR-chain capped at %d terms (watcher max_boolean_operators=%d)",
                max_terms,
                max_boolean_operators,
            )
            break
        kw = (kw or "").strip()
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        pieces.append(f'"{kw}"' if " " in kw else kw)
    return " OR ".join(pieces)


def normalize_inbox_post(raw: dict[str, Any], *, cofounder_id: str) -> dict[str, Any]:
    """Map a Crustdata notification post object into our inbox row shape.

    Crustdata returns either a single object or a list; the receiver flattens
    so this function takes one post at a time. Schema lifted from event 3
    (`linkedin-post-with-keyword`) — both person- and company-actor variants.
    """
    actor_type = raw.get("actor_type") or ""
    is_person = actor_type == "person"
    is_company = actor_type == "company"

    author_name = (
        raw.get("person_name") if is_person else raw.get("company_name")
    ) or raw.get("actor_name")
    author_title = raw.get("person_title") if is_person else None

    # `current_employers` is the canonical title/company source for person actors
    employers = raw.get("current_employers") or []
    if is_person and employers:
        first = employers[0] or {}
        author_title = author_title or first.get("employee_title")
        author_company = first.get("employer_name")
        company_li_id = first.get("employer_linkedin_id")
    elif is_company:
        author_company = raw.get("company_name")
        company_li_id = raw.get("company_linkedin_id")
    else:
        author_company = None
        company_li_id = None

    return {
        "cofounder_id": cofounder_id,
        "post_uid": raw.get("uid"),
        "post_url": raw.get("share_url"),
        "share_urn": raw.get("share_urn"),
        "backend_urn": raw.get("backend_urn"),
        "actor_type": actor_type,
        "actor_name": raw.get("actor_name"),
        "author_name": author_name,
        "author_title": author_title,
        "author_company": author_company,
        "author_company_linkedin_id": company_li_id,
        "author_linkedin_url": raw.get("person_linkedin_flagship_profile_url")
        if is_person
        else raw.get("company_linkedin_url"),
        "author_linkedin_urn": raw.get("person_linkedin_urn"),
        "author_location": raw.get("person_location"),
        "post_text": raw.get("text") or "",
        "post_summary": raw.get("post_summary"),
        "post_category": raw.get("post_category"),
        "date_posted": raw.get("date_posted"),
        "total_reactions": raw.get("total_reactions"),
        "total_comments": raw.get("total_comments"),
        "num_shares": raw.get("num_shares"),
        "reactions_by_type": raw.get("reactions_by_type"),
        "current_employers": employers,
        "is_repost_without_thoughts": raw.get("is_repost_without_thoughts"),
        "raw": raw,
        "consumed": False,
        "consumed_at": None,
    }
