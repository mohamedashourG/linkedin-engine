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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote as _quote

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
# Transient 429 from Unipile/LinkedIn — exponential backoff then re-raise via _check_resp.
# Bumped base from 0.75s → 4s and attempts from 3 → 5 (max wait
# 4 + 8 + 16 + 32 = 60s) after Cardiowell run hit a sustained rate-limit
# window that the previous short backoff couldn't ride out. Unipile's
# limiter appears to reset on a multi-second window per account.
_UNIPILE_429_MAX_ATTEMPTS = 5
_UNIPILE_429_BACKOFF_BASE_S = 4.0

# ─── Human-cadence throttling (anti-automation-detection) ────────────────
#
# LinkedIn flags accounts for automation based on (a) volume per unit time
# and (b) the *regularity* of inter-action timing. Uniform jitter is still
# trivially detectable as machine-paced — automation classifiers fit a
# distribution and flag the tight variance. Real humans burst (multiple
# fast clicks while reading a thread) then pause (read, switch tabs, walk
# away). We mimic that with `_human_delay_seconds()` which mixes two
# distributions: the baseline jittered range plus a 10% chance of a much
# longer "thinking pause".
#
# Tightened all intervals on 2026-05-14 after three GB-proxied accounts
# (Michael Colivet + two Nicolas Vila) hit `status=CREDENTIALS` (forced
# re-auth from LinkedIn flagging suspicious activity). The previous
# defaults (1.4s users, 2.0s search, 0.0s default, 0.0s post_comment)
# were the floor of what could plausibly be human — combined with the
# GB-proxy + parallel-workers signal stack, it tipped over.

# Per-call-type throttle intervals (humanlike with heavy-tailed jitter).
# All locks + last-call timestamps are PER-ACCOUNT — see the dict-keyed
# structures below — so two accounts can fire in parallel without
# contending on the same lock. This is the unlock that makes the
# account-pool rotation actually deliver N× aggregate throughput while
# each individual account stays under LinkedIn's per-session radar.

# `/users/{slug}` — profile views (loudest signal — target sees a "X
# viewed your profile" notification on each call).
_UNIPILE_USERS_MIN_INTERVAL_S = 6.0
_UNIPILE_USERS_JITTER_MAX_S = 6.0

# `/linkedin/search` — keyword + people search.
_UNIPILE_SEARCH_MIN_INTERVAL_S = 4.0
_UNIPILE_SEARCH_JITTER_MAX_S = 4.0

# Default — every other Unipile call (post details, comments listing, etc.)
_UNIPILE_DEFAULT_MIN_INTERVAL_S = 3.0
_UNIPILE_DEFAULT_JITTER_MAX_S = 3.0

# POST `/posts/{id}/comments` — write actions. 90s baseline + 0–90s
# jitter, occasional multi-minute "read the thread" pause.
_UNIPILE_POST_COMMENT_MIN_INTERVAL_S = 90.0
_UNIPILE_POST_COMMENT_JITTER_MAX_S = 90.0

# POST `/users/invite` — connection requests with optional note.
# LinkedIn's anti-automation signal is *especially* sensitive to invite
# bursts (more than to comment bursts), and recipients see the invite
# immediately in their notifications. 5-minute baseline + up to 5-min
# jitter (so 5-10 min typical between invites from the same account)
# with a 10% chance of a 15-40 min "stepped away" pause via
# `_human_delay_seconds`. Combined with the per-account daily cap of
# 10 and the aggregate pool-gate, this keeps invite cadence well within
# manual-use patterns.
_UNIPILE_INVITE_MIN_INTERVAL_S = 300.0
_UNIPILE_INVITE_JITTER_MAX_S = 300.0

# LinkedIn caps invite-note text at 200 chars on the standard
# in-mail/network invite flow (Premium accounts get more, but we don't
# rely on Premium). Enforced server-side too; we validate client-side
# so we surface a clean error instead of a 4xx surprise.
LINKEDIN_INVITE_NOTE_MAX_CHARS = 200

# POST `/chats` — first-touch DM to a (typically just-accepted)
# 1st-degree connection. DMs are LOWER flag-risk than invites (recipient
# expects messages from connections) but burst-sending still trips
# LinkedIn's anti-automation classifier. 3-minute baseline + up to 3-min
# jitter (so 3-6 min typical between DMs from the same account) with a
# 10% chance of a 9-24 min long-pause draw via `_human_delay_seconds`.
# Combined with the per-account daily cap of 25 DMs and the aggregate
# pool-gate, this stays inside the published "safe daily" envelope.
_UNIPILE_DM_MIN_INTERVAL_S = 180.0
_UNIPILE_DM_JITTER_MAX_S = 180.0

# LinkedIn messaging has no documented hard char cap on direct messages
# but the practical operational ceiling is ~8000 chars (anything longer
# gets truncated or rejected by some clients). We cap at 1500 to keep
# DMs in normal cold-outreach range — anything longer is almost certainly
# a copy/paste error.
LINKEDIN_DM_TEXT_MAX_CHARS = 1500


# ── Per-account throttle state ──────────────────────────────────────────
#
# Each throttle category keeps a dict keyed by account_id → last-call
# timestamp + a per-key lock. ``defaultdict``-style auto-creation on
# first access via the ``_get_account_*`` helpers below.
#
# Note: an empty account_id ("" or None — e.g. legacy callers that
# haven't been migrated to pass it) falls back to a shared bucket
# keyed by "__global__". Eventually we want every call to pass an
# explicit account_id, but the fallback bucket keeps existing callers
# working during the migration.

_FALLBACK_ACCOUNT_KEY = "__global__"

_users_throttle_last_ts: dict[str, float] = {}
_users_throttle_locks: dict[str, threading.Lock] = {}
_users_throttle_state_lock = threading.Lock()  # guards the dict structures

_search_throttle_last_ts: dict[str, float] = {}
_search_throttle_locks: dict[str, threading.Lock] = {}
_search_throttle_state_lock = threading.Lock()

_default_throttle_last_ts: dict[str, float] = {}
_default_throttle_locks: dict[str, threading.Lock] = {}
_default_throttle_state_lock = threading.Lock()

_post_comment_throttle_last_ts: dict[str, float] = {}
_post_comment_throttle_locks: dict[str, threading.Lock] = {}
_post_comment_throttle_state_lock = threading.Lock()

_invite_throttle_last_ts: dict[str, float] = {}
_invite_throttle_locks: dict[str, threading.Lock] = {}
_invite_throttle_state_lock = threading.Lock()

_dm_throttle_last_ts: dict[str, float] = {}
_dm_throttle_locks: dict[str, threading.Lock] = {}
_dm_throttle_state_lock = threading.Lock()


def _account_key(account_id: str | None) -> str:
    aid = (account_id or "").strip()
    return aid or _FALLBACK_ACCOUNT_KEY


def _get_throttle_lock(
    locks_dict: dict[str, threading.Lock],
    state_lock: threading.Lock,
    key: str,
) -> threading.Lock:
    """Get-or-create the per-account lock atomically."""
    lock = locks_dict.get(key)
    if lock is not None:
        return lock
    with state_lock:
        lock = locks_dict.get(key)
        if lock is None:
            lock = threading.Lock()
            locks_dict[key] = lock
        return lock


def _human_delay_seconds(base_s: float, jitter_s: float) -> float:
    """Return a wait duration that resists automation pattern-matching.

    Standard mode (90% of calls): base + uniform(0, jitter) — same shape
    as the old uniform jitter, gives most calls a predictable-ish gap.

    Occasional long pause (10% of calls): uniform(base*3, base*8) — a
    much longer "task switch / read break" that scrambles the inter-call
    distribution so the classifier sees a heavy-tailed pattern instead
    of a tight band of values. Empirically this is what real human
    browsing looks like — short bursts of activity separated by long
    irregular pauses (composing a reply, reading a thread, walking away
    to grab coffee, etc.).
    """
    import random as _rand
    if _rand.random() < 0.10:
        return _rand.uniform(base_s * 3.0, base_s * 8.0)
    return base_s + _rand.uniform(0, jitter_s)


def _throttle_users_call(account_id: str | None = None) -> None:
    """Space `/users/{slug}` calls with humanlike timing — PER ACCOUNT.

    Each LinkedIn-session profile view triggers a "X viewed your profile"
    notification on the target, so this is the loudest signal we make.
    Per-account state means two accounts in the pool can fire in parallel
    without contending on the same lock; each individual account still
    stays at humanlike cadence."""
    key = _account_key(account_id)
    lock = _get_throttle_lock(
        _users_throttle_locks, _users_throttle_state_lock, key,
    )
    with lock:
        last_ts = _users_throttle_last_ts.get(key, 0.0)
        elapsed = time.monotonic() - last_ts
        target_gap = _human_delay_seconds(
            _UNIPILE_USERS_MIN_INTERVAL_S,
            _UNIPILE_USERS_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            time.sleep(wait)
        _users_throttle_last_ts[key] = time.monotonic()
    try:
        from app.services import cost_tracker
        cost_tracker.record_unipile_call("profile_view")
    except Exception:  # noqa: BLE001
        pass


def _throttle_search_call(account_id: str | None = None) -> None:
    """Space `/linkedin/search` calls with humanlike timing — PER ACCOUNT."""
    key = _account_key(account_id)
    lock = _get_throttle_lock(
        _search_throttle_locks, _search_throttle_state_lock, key,
    )
    with lock:
        last_ts = _search_throttle_last_ts.get(key, 0.0)
        elapsed = time.monotonic() - last_ts
        target_gap = _human_delay_seconds(
            _UNIPILE_SEARCH_MIN_INTERVAL_S,
            _UNIPILE_SEARCH_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            time.sleep(wait)
        _search_throttle_last_ts[key] = time.monotonic()
    try:
        from app.services import cost_tracker
        cost_tracker.record_unipile_call("search")
    except Exception:  # noqa: BLE001
        pass


def _throttle_post_comment_call(account_id: str | None = None) -> None:
    """Space write actions (POST /posts/{id}/comments) — PER ACCOUNT.

    90s baseline + 0–90s jitter (1.5–3 min typical) with 10% chance of
    a 4.5–12 min "read the thread" pause. Sequential user clicks on
    "Send reply" on the SAME account queue behind this lock; clicks
    using different accounts (e.g. multi-operator deployments) fire
    in parallel."""
    key = _account_key(account_id)
    lock = _get_throttle_lock(
        _post_comment_throttle_locks, _post_comment_throttle_state_lock, key,
    )
    with lock:
        last_ts = _post_comment_throttle_last_ts.get(key, 0.0)
        elapsed = time.monotonic() - last_ts
        target_gap = _human_delay_seconds(
            _UNIPILE_POST_COMMENT_MIN_INTERVAL_S,
            _UNIPILE_POST_COMMENT_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            log.info(
                "post_comment throttle (account=%s): sleeping %.1fs",
                key[:18], wait,
            )
            time.sleep(wait)
        _post_comment_throttle_last_ts[key] = time.monotonic()


def _throttle_invite_call(account_id: str | None = None) -> None:
    """Space LinkedIn invite calls — PER ACCOUNT.

    5min baseline + 0–5min jitter (5–10 min typical) with 10% chance of
    a 15–40 min "stepped away" pause via ``_human_delay_seconds``. Invites
    are the highest-risk anti-automation signal on LinkedIn (recipients
    see them in real-time notifications), so the gap is materially
    longer than post_comment's 90s.

    Concurrent ``send_invitation`` calls on the SAME account_id queue
    behind this lock; calls using different accounts (pool rotation)
    fire in parallel. The aggregate pool-gate (``unipile_pool``) layers
    on top to prevent burst across accounts.
    """
    key = _account_key(account_id)
    lock = _get_throttle_lock(
        _invite_throttle_locks, _invite_throttle_state_lock, key,
    )
    with lock:
        last_ts = _invite_throttle_last_ts.get(key, 0.0)
        elapsed = time.monotonic() - last_ts
        target_gap = _human_delay_seconds(
            _UNIPILE_INVITE_MIN_INTERVAL_S,
            _UNIPILE_INVITE_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            log.info(
                "invite throttle (account=%s): sleeping %.1fs",
                key[:18], wait,
            )
            time.sleep(wait)
        _invite_throttle_last_ts[key] = time.monotonic()


def _throttle_dm_call(account_id: str | None = None) -> None:
    """Space LinkedIn DM (chat-message) calls — PER ACCOUNT.

    3min baseline + 0-3min jitter (3-6 min typical) with 10% chance of a
    9-24 min long-pause via ``_human_delay_seconds``. DMs are lower
    flag-risk than invites since the recipient expects messages from
    1st-degree connections, but burst-sending still trips LinkedIn's
    anti-automation classifier so the gap is tighter than invite but
    looser than post_comment.

    Same lock pattern as the other per-account throttles. The aggregate
    pool-gate (``unipile_pool``) layers on top to prevent cross-account
    bursts when multiple accounts run concurrent DM batches.
    """
    key = _account_key(account_id)
    lock = _get_throttle_lock(
        _dm_throttle_locks, _dm_throttle_state_lock, key,
    )
    with lock:
        last_ts = _dm_throttle_last_ts.get(key, 0.0)
        elapsed = time.monotonic() - last_ts
        target_gap = _human_delay_seconds(
            _UNIPILE_DM_MIN_INTERVAL_S,
            _UNIPILE_DM_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            log.info(
                "dm throttle (account=%s): sleeping %.1fs",
                key[:18], wait,
            )
            time.sleep(wait)
        _dm_throttle_last_ts[key] = time.monotonic()


def _throttle_default_call(account_id: str | None = None) -> None:
    """Universal pre-call gate fired before *every* Unipile HTTP request
    — PER ACCOUNT.

    Spaces consecutive calls per LinkedIn-session with humanlike timing
    (3–6s typical, ~10% chance of a 9–24s pause). Stacks on top of the
    more aggressive search/users/post_comment throttles for those
    specific endpoints.
    """
    key = _account_key(account_id)
    lock = _get_throttle_lock(
        _default_throttle_locks, _default_throttle_state_lock, key,
    )
    with lock:
        last_ts = _default_throttle_last_ts.get(key, 0.0)
        elapsed = time.monotonic() - last_ts
        target_gap = _human_delay_seconds(
            _UNIPILE_DEFAULT_MIN_INTERVAL_S,
            _UNIPILE_DEFAULT_JITTER_MAX_S,
        )
        wait = target_gap - elapsed
        if wait > 0:
            time.sleep(wait)
        _default_throttle_last_ts[key] = time.monotonic()


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
class UnipileCommentReply:
    """A reply nested under one of our top-level comments.

    Intentionally narrower than ``UnipileComment``: we store only the data
    needed to render the reply thread in the manual-comments UI (author
    name + text + time), explicitly NOT the author's profile URL or
    public identifier — the product decision is to render replies as a
    flat list without linking out to author profiles.

    ``author_provider_id`` IS captured (as ACoAAA-form member URN) so the
    reply-to-reply flow can post a proper @-mention back via Unipile's
    ``mentions`` body field — the alternative (plain-text name) renders as
    raw text on LinkedIn instead of a clickable tag."""
    comment_id: str
    text: str
    author_name: str | None
    author_provider_id: str | None
    published_at: datetime | None


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
    # provider_id IS already returned by Unipile's people-search response
    # under the `id` key (ACoAA…-form member URN). Capturing it here lets
    # `_run_unipile_title_search` skip the otherwise-redundant /users/{slug}
    # resolve call inside `get_user_posts` — that resolve was previously
    # consuming the whole `profile_view` daily cap AND firing a "X viewed
    # your profile" notification on every stranger in the people-search
    # roster (loudest automation signal on the platform). 2026-05-14 fix.
    provider_id: str | None = None


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


def _request_with_429_retry(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    account_id: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Run one HTTP call; on 429 retry with exponential backoff (in-process).

    Every call passes through ``_throttle_default_call(account_id)`` first —
    per-account pacing so two accounts in the pool can fire in parallel
    while each individual session stays humanlike.

    Callers that don't pass ``account_id`` get the shared ``__global__``
    bucket (legacy compat). New call sites should always pass the
    account_id of the LinkedIn session they're using.

    Wraps httpx/httpcore transport errors (connect timeout, read timeout,
    pool full, DNS, etc.) as ``UnipileError`` so callers' existing
    ``except UnipileError`` handlers catch them consistently."""
    _throttle_default_call(account_id)
    m = method.upper()
    last: httpx.Response | None = None
    for attempt in range(_UNIPILE_429_MAX_ATTEMPTS):
        try:
            last = client.request(m, path, **kwargs)
        except httpx.HTTPError as err:
            # Includes ConnectTimeout, ReadTimeout, PoolTimeout, ConnectError,
            # NetworkError, RemoteProtocolError, etc. Re-raise as the
            # vendor-typed error so per-call try/except in discovery works.
            raise UnipileError(
                f"unipile transport error on {m} {path[:120]}: {err}"
            ) from err
        if last.status_code != 429:
            return last

        # Distinguish Unipile-side 429 (their gateway limit; retrying helps)
        # from LinkedIn-side 429 (the "provider" — Yair's account itself is
        # being throttled by LinkedIn; retrying makes it WORSE because each
        # retry burns more of his per-account budget and prolongs the cooldown).
        #
        # Unipile's response body for the LinkedIn-side variant looks like:
        #   {"status":429,"type":"errors/too_many_requests",
        #    "title":"Too many requests",
        #    "detail":"The provider cannot accept any more requests at the
        #             moment. Please try again later."}
        # We sniff the "provider" keyword in the body to decide. On a
        # provider-429 we fail FAST (no retry) and let the caller surface the
        # condition; callers can then back off at the application layer
        # (e.g. skip remaining jobs in the refresh loop) rather than us
        # silently hammering LinkedIn harder.
        body_lower = ""
        try:
            body_lower = (last.text or "").lower()
        except Exception:
            body_lower = ""
        is_provider_429 = "provider" in body_lower
        if is_provider_429:
            log.warning(
                "unipile 429 PROVIDER-side %s %s — failing fast (retry would "
                "extend LinkedIn cooldown on this account)",
                m,
                path[:160],
            )
            return last  # caller will see 429 and raise UnipileError

        if attempt < _UNIPILE_429_MAX_ATTEMPTS - 1:
            delay = _UNIPILE_429_BACKOFF_BASE_S * (2**attempt)
            log.warning(
                "unipile 429 UNIPILE-side %s %s attempt %d/%d, sleeping %.2fs",
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
    # Fallback — derive author profile URL from the canonical LinkedIn POST
    # URL shape `linkedin.com/posts/<author-slug>_<title-slug>-activity-<id>`.
    # Unipile's classic post-search response doesn't reliably populate the
    # author URL, but the slug is embedded in `url` here. Without this,
    # the Crustdata-first enrichment path in discovery has no slug to look
    # up and routes every author to Unipile's `/users/{slug}` fallback —
    # defeating the whole point of off-loading enrichment to Crustdata.
    if not prof_url and url and "/posts/" in url:
        tail = url.rsplit("/posts/", 1)[1]
        # Stop at the first underscore (delimiter between author slug and
        # post-title slug), or at the first /, ?, # if those come first.
        slug_part = (
            tail.split("_", 1)[0]
            .split("/", 1)[0]
            .split("?", 1)[0]
            .split("#", 1)[0]
        )
        # Slug-shape validation mirrors crustdata_enrich.is_likely_person_slug
        # so we only synthesize URLs Crustdata would accept downstream.
        if slug_part and re.match(r"^[a-z0-9][a-z0-9_-]{0,99}$", slug_part, re.I):
            prof_url = f"https://www.linkedin.com/in/{slug_part}"
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
    _throttle_search_call(account_id)
    with _client() as client:
        resp = _request_with_429_retry(
            client, "POST", "/linkedin/search",
            account_id=account_id, params=params, json=body,
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
            client, "GET", "/linkedin/search/parameters",
            account_id=account_id, params=params,
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
    # Capture provider_id (ACoAA…-form URN). Unipile returns it inline on
    # people-search; keeping it lets get_user_posts skip the resolve call.
    raw_id = (raw.get("id") or raw.get("provider_id") or "").strip()
    provider_id = raw_id if raw_id.startswith("AC") and len(raw_id) >= 30 else None
    return UnipilePerson(
        name=name or "(unknown)",
        public_identifier=public_id,
        profile_url=profile_url,
        title=raw.get("headline") or raw.get("title") or raw.get("occupation"),
        company=raw.get("company") or raw.get("company_name"),
        location=raw.get("location"),
        provider_id=provider_id,
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
    _throttle_search_call(account_id)
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "POST",
            "/linkedin/search",
            account_id=account_id,
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
    Throttled to ~1.4s+jitter between calls to mimic human cadence and
    avoid LinkedIn's automation-detection heuristics on /users/.
    """
    _throttle_users_call(account_id)
    try:
        resp = _request_with_429_retry(
            client, "GET", f"/users/{slug}",
            account_id=account_id, params={"account_id": account_id},
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
            account_id=account_id,
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
            account_id=account_id,
            params={"account_id": account_id},
        )
    if resp.status_code == 404:
        return None
    payload = _check_resp(resp, "get_post")
    return _parse_unipile_post(payload)


# ---------------------------------------------------------------- comments

def _post_urn_path(post_url_or_id: str) -> str | None:
    """Convert a post URL or bare numeric id to the URN-encoded path segment
    Unipile requires for nested ``/posts/{post_id}/comments`` calls.

    Unipile's nested endpoint rejects bare numerics (400 'invalid post_id')
    and only accepts ``urn:li:activity:<id>`` (URL-encoded). The flat
    ``/posts/comments?post_url=...`` form was easier to call but is more
    aggressively rate-limited by LinkedIn — we standardize on the nested
    form for both stats reads (verified working against Account B even when
    Yair's account is in cooldown)."""
    pid = extract_post_id_from_url(post_url_or_id) if "linkedin.com" in (post_url_or_id or "") else (post_url_or_id or "").strip()
    if not pid:
        return None
    # If we got a bare numeric activity id, wrap it as a URN. If it's already
    # a urn:li:activity:/ugcPost:/share: form, keep it as-is.
    if pid.startswith("urn:li:"):
        urn = pid
    elif pid.isdigit():
        urn = f"urn:li:activity:{pid}"
    else:
        # Some Unipile post_ids come back as bare ugcPost/share strings
        # without the urn prefix — leave alone for the API to handle.
        urn = pid
    # urllib.parse.quote with safe='' so the colons get percent-encoded
    # exactly as Unipile expects (verified via curl 2026-05-14).
    return _quote(urn, safe="")


def get_post_comments(*, account_id: str, post_url: str) -> list[UnipileComment]:
    """Fetch top-level comments on a LinkedIn post.

    Uses Unipile's nested ``GET /posts/{post_urn}/comments`` endpoint (the
    REST-canonical shape). Confirmed shape via direct curl 2026-05-14:
    bare numeric post_id → 400 'invalid post_id'; URN-encoded path → 200.

    The ``account_id`` here is the LinkedIn session whose perspective we
    use to read — typically NOT the same account that posted the comment
    we're tracking. Reading is a public-graph operation so any healthy
    LinkedIn session works, which lets the manual-comments refresh route
    use a dedicated stats account (``settings.unipile_stats_account_id``)
    decoupled from Yair's posting account."""
    if settings.unipile_mock:
        return _MOCK_COMMENTS.get(post_url, [])

    if not account_id:
        raise UnipileError("get_post_comments: account_id required")
    if not post_url:
        return []
    urn_path = _post_urn_path(post_url)
    if not urn_path:
        return []
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "GET",
            f"/posts/{urn_path}/comments",
            account_id=account_id,
            params={"account_id": account_id, "limit": 100},
        )
    if resp.status_code == 404:
        return []
    payload = _check_resp(resp, "get_post_comments")
    items = payload.get("items") or []
    out: list[UnipileComment] = []
    for raw in items:
        # Unipile returns rich nested ``author_details`` plus a flat
        # ``author`` (display name). Older shape had ``author`` as a dict;
        # we accept both for forward-compat.
        author_raw = raw.get("author")
        if isinstance(author_raw, dict):
            author = author_raw
            author_name = author.get("name")
        else:
            author = raw.get("author_details") or {}
            author_name = author_raw if isinstance(author_raw, str) else author.get("name")
        out.append(
            UnipileComment(
                comment_id=str(raw.get("id") or ""),
                text=raw.get("text") or "",
                author_name=author_name,
                author_provider_id=author.get("id") or author.get("provider_id") or author.get("urn"),
                author_public_identifier=author.get("public_identifier"),
                author_profile_url=author.get("profile_url") or author.get("public_profile_url"),
                published_at=_parse_iso(raw.get("date") or raw.get("published_at")),
                reaction_count=_safe_int(raw.get("reaction_counter") or raw.get("num_reactions")),
                reply_count=_safe_int(raw.get("reply_counter") or raw.get("num_replies")),
            )
        )
    return out


def get_comment_replies(
    *,
    account_id: str,
    post_url: str,
    comment_id: str,
) -> list[UnipileCommentReply]:
    """Fetch the replies nested under one of our previously-posted comments.

    Uses ``GET /posts/{post_urn}/comments?comment_id=<parent_id>`` — the
    same nested endpoint as ``get_post_comments``, but with ``comment_id``
    set as a query param to scope to the replies under that thread.
    Confirmed shape via direct curl 2026-05-14: response items have
    ``thread_id`` matching the parent comment_id, ``reply_counter`` 0
    (replies are leaves in our flat model).

    Returns ``[]`` on any of: empty account_id/post_url/comment_id, 404,
    or no items. ``UnipileError`` propagates so the caller's
    rate-limit / circuit-breaker logic still applies (refresh-engagement
    short-circuits the loop on the first provider-side 429)."""
    if settings.unipile_mock:
        return []
    if not account_id or not post_url or not comment_id:
        return []
    urn_path = _post_urn_path(post_url)
    if not urn_path:
        return []
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "GET",
            f"/posts/{urn_path}/comments",
            account_id=account_id,
            params={
                "account_id": account_id,
                "comment_id": comment_id,
                "limit": 100,
            },
        )
    if resp.status_code == 404:
        return []
    payload = _check_resp(resp, "get_comment_replies")
    items = payload.get("items") or []
    out: list[UnipileCommentReply] = []
    for raw in items:
        # Tolerate both shapes: ``author`` as flat string (current Unipile
        # response) or ``author`` as dict with ``name`` (older shape).
        author_raw = raw.get("author")
        author_details = raw.get("author_details") or {}
        if isinstance(author_raw, dict):
            author_name = author_raw.get("name")
            author_pid = author_raw.get("id") or author_raw.get("provider_id") or author_raw.get("urn")
        else:
            author_name = author_raw if isinstance(author_raw, str) else None
            # `author_details.id` is the ACoAAA-form member URN we need for
            # @-mentions when replying back to this reply.
            author_pid = author_details.get("id") or author_details.get("provider_id") or author_details.get("urn")
        out.append(
            UnipileCommentReply(
                comment_id=str(raw.get("id") or ""),
                text=raw.get("text") or "",
                author_name=author_name,
                author_provider_id=(str(author_pid).strip() if author_pid else None),
                published_at=_parse_iso(raw.get("date") or raw.get("published_at")),
            )
        )
    return out


def post_comment(
    *,
    account_id: str,
    post_url: str,
    text: str,
    parent_comment_id: str | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> UnipileCommentPostResult:
    """POST a top-level or threaded comment on a LinkedIn post via Unipile.

    Mentions
    --------
    Pass ``mentions=[{"name": "Tarpan Patel", "profile_id": "ACoAAA17..."}]``
    to render @-mentions in the resulting LinkedIn comment. The ``text``
    field must reference each mention by index via the ``{{N}}`` placeholder
    (Unipile substitutes the placeholder with a real LinkedIn @-tag at post
    time). Example::

        post_comment(
            ...,
            text="{{0}} thanks for the thoughtful reply…",
            mentions=[{"name": "Tarpan Patel",
                       "profile_id": "ACoAAA17lcwBbmp21rltGYgcFamfKdZfci9oojw"}],
        )

    Without ``mentions``, names are sent as plain text and LinkedIn shows
    them un-tagged (the operator reported this on 2026-05-14 — "Michael
    Colivet Not bad take" rendered as raw text, not a clickable mention).
    """
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

    # **POST endpoint requires the real social_id URN, NOT the activity id.**
    # LinkedIn URLs end with `activity-<N>` for ANY post — but the post's
    # actual social_id is typically `urn:li:ugcPost:<M>` (different N vs M).
    # Unipile's GET /posts auto-resolves activity→post; POST /posts/.../comments
    # does NOT and returns 422 "invalid_post / Post cannot be found" on a
    # bare or URN-wrapped activity ID. Verified live 2026-05-14 against
    # Jason Yarbrough's `nycdoe-nychealth` post — bare and URN-activity
    # forms both 422'd, URN-ugcPost succeeded.
    #
    # Resolution path:
    #   1. extract_post_id_from_url returns the activity numeric (URL fmt).
    #   2. GET /posts/{activity} → returns {social_id: "urn:li:ugcPost:..."}
    #   3. POST /posts/{social_id_url_encoded}/comments → success.
    #
    # We use the social_id for the path. If the post was originally a
    # ugcPost/share URL, extract_post_id_from_url already returned the
    # URN form and we skip the resolve step.
    resolved_social_id: str = post_id
    needs_resolve = post_id.isdigit()  # bare numeric = activity form
    if needs_resolve:
        try:
            with _client() as resolve_client:
                resolve_resp = _request_with_429_retry(
                    resolve_client,
                    "GET",
                    f"/posts/{post_id}",
                    account_id=account_id,
                    params={"account_id": account_id},
                )
            if resolve_resp.status_code == 200:
                payload = resolve_resp.json() or {}
                resolved = (payload.get("social_id") or "").strip()
                if resolved:
                    resolved_social_id = resolved
                    log.info(
                        "post_comment: resolved activity %s → %s",
                        post_id, resolved,
                    )
                else:
                    # Unipile returned 200 but no social_id — fall through
                    # with the original post_id; the POST will surface the
                    # underlying error (404/422) for the caller to handle.
                    log.warning(
                        "post_comment: GET /posts/%s returned no social_id; "
                        "trying POST with activity id (likely will 422)",
                        post_id,
                    )
            elif resolve_resp.status_code == 404:
                raise UnipileError(
                    f"post_comment: post not found ({post_id}) — "
                    "LinkedIn URL may be private, deleted, or blocked for "
                    "this account"
                )
        except UnipileError:
            raise
        except Exception as err:  # noqa: BLE001
            log.warning(
                "post_comment: resolve step failed (%s) — falling through "
                "with activity id; POST may 422 if the post is a ugcPost",
                err,
            )

    # URL-encode the resolved path component (URNs contain colons which
    # need percent-encoding). Use safe='' so the colons get escaped.
    post_path = _quote(resolved_social_id, safe="")

    body: dict[str, Any] = {"account_id": account_id, "text": text[:8000]}
    if parent_comment_id:
        body["comment_id"] = parent_comment_id
    if mentions:
        # Filter to entries with both `name` and `profile_id` — Unipile rejects
        # entries missing either. Silent skip so a malformed mention doesn't
        # block the rest of the post.
        cleaned = [
            {"name": str(m["name"]), "profile_id": str(m["profile_id"])}
            for m in mentions
            if isinstance(m, dict) and m.get("name") and m.get("profile_id")
        ]
        if cleaned:
            body["mentions"] = cleaned

    # Humanlike pacing for write actions — 90s+ between comments PER account.
    # Sequential user clicks queue here and drip out at a believable rate;
    # standalone first-of-day clicks fire immediately (no gap to enforce).
    _throttle_post_comment_call(account_id)

    # Audit log — capture exactly what's about to hit the wire so we can
    # forensically reconstruct what happened on every write. Mentions
    # field is the one most likely to silently fail (Unipile may drop it
    # without erroring, LinkedIn may render it as plain text). Logging
    # the body shape here means any future "mention didn't tag" report
    # has hard evidence within ~1 minute of the click.
    _mentions_summary = (
        f"mentions={len(body['mentions'])}({[m['name'] for m in body['mentions']]})"
        if body.get("mentions") else "mentions=NONE"
    )
    log.warning(
        "post_comment WIRE → path=/posts/%s/comments  account=%s  "
        "parent=%s  text_starts=%r  text_has_placeholder=%s  %s",
        resolved_social_id[:60],
        account_id[:18],
        (parent_comment_id or "")[:24],
        body["text"][:80],
        "{{0}}" in body["text"],
        _mentions_summary,
    )

    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "POST",
            f"/posts/{post_path}/comments",
            account_id=account_id,
            params={"account_id": account_id},
            json=body,
        )
    payload = _check_resp(resp, "post_comment")
    # Log Unipile's response so we can see if mentions echoed back or
    # were silently dropped.
    log.warning(
        "post_comment RESPONSE ← status=%d  body=%s",
        resp.status_code,
        (resp.text or "")[:300],
    )
    cid = str(payload.get("id") or payload.get("comment_id") or payload.get("urn") or "")
    return UnipileCommentPostResult(
        comment_id=cid,
        posted_at=_parse_iso(payload.get("date") or payload.get("created_at")),
        raw=payload,
    )


# ── LinkedIn invitations (connection request with optional note) ──────────


@dataclass(frozen=True)
class UnipileInvitationResult:
    """Result of POST /users/invite. Unipile returns a server-side
    invitation id we persist for later status polling, plus the raw
    payload for forensic audit."""
    invitation_id: str | None
    sent_at: datetime | None
    raw: dict[str, Any]


def send_invitation(
    *,
    account_id: str,
    provider_id: str,
    message: str | None = None,
) -> UnipileInvitationResult:
    """Send a LinkedIn connection request, optionally with a note.

    Unipile endpoint: ``POST /api/v1/users/invite`` with body
    ``{account_id, provider_id, message?}``. ``provider_id`` is the
    target's ACoAA… member URN (the same form Unipile returns from
    people-search and ``resolve_profile``).

    Args:
        account_id: pool account that owns the LinkedIn session. The
            caller is responsible for ensuring this is the SAME account
            that posted the original comment the recipient replied to,
            so the invite looks like a natural follow-up rather than a
            stranger reaching out.
        provider_id: LinkedIn URN of the invite target (ACoAA…).
        message: optional invite note, ≤200 chars. LinkedIn truncates
            silently above the cap; we validate up front so callers see
            a clean ``ValueError`` instead.

    Returns:
        UnipileInvitationResult with the Unipile invitation_id (used for
        status polling) and the raw response payload.

    Raises:
        ValueError: message too long or required params missing.
        UnipileError: Unipile responded 4xx/5xx (most commonly 422 if a
            connection / pending invite already exists, or 429 if the
            account has hit LinkedIn's invite rate limit).
        UnipileNotConfigured: ``UNIPILE_MOCK`` is set or credentials
            missing.

    Throttle stack applied:
      • Layer 1 — per-account ``_throttle_invite_call`` (5–10 min gap)
      • Layer 2 — universal ``_throttle_default_call`` (3–6 s gap, via
        ``_request_with_429_retry``)
      • Layer 3 — pool aggregate gate (only applies when called via
        ``_pool_acquire("invite", …)`` — direct callers bypass it)
    """
    if settings.unipile_mock:
        raise UnipileNotConfigured(
            "send_invitation: UNIPILE_MOCK=true — refusing to issue a real "
            "LinkedIn invite. Set UNIPILE_MOCK=false to enable."
        )
    if not account_id:
        raise ValueError("send_invitation: account_id is required")
    if not provider_id:
        raise ValueError("send_invitation: provider_id is required")
    pid = provider_id.strip()
    if not (pid.startswith("AC") and len(pid) >= 30):
        raise ValueError(
            f"send_invitation: provider_id must be the ACoAA… member URN form "
            f"returned by people-search / resolve_profile. Got {pid[:24]!r}"
        )
    note = (message or "").strip()
    if len(note) > LINKEDIN_INVITE_NOTE_MAX_CHARS:
        raise ValueError(
            f"send_invitation: note length {len(note)} exceeds LinkedIn's "
            f"{LINKEDIN_INVITE_NOTE_MAX_CHARS}-char cap. Truncate before calling."
        )

    body: dict[str, Any] = {
        "provider_id": pid,
        "account_id": account_id,
    }
    if note:
        body["message"] = note

    # Per-account invite cadence: 5-10 min typical (15-40 min on the
    # 10% long-pause draw). Stacks under the aggregate pool gate.
    _throttle_invite_call(account_id)

    # Forensic audit — log the wire payload before issuing. Invitations
    # are visible to the recipient immediately so any "this invite came
    # from the wrong account" or "this note shows up garbled" reports
    # have hard evidence within ~1 minute of the click. Note text is
    # truncated to 80 chars to keep log lines readable; full text is
    # already in Mongo at `linkedin_invitations.note_text`.
    log.warning(
        "send_invitation WIRE → path=/users/invite  account=%s  "
        "target=%s  has_note=%s  note_len=%d  note_starts=%r",
        account_id[:18],
        pid[:24],
        bool(note),
        len(note),
        note[:80],
    )

    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "POST",
            "/users/invite",
            account_id=account_id,
            params={"account_id": account_id},
            json=body,
        )
    payload = _check_resp(resp, "send_invitation")
    log.warning(
        "send_invitation RESPONSE ← status=%d  body=%s",
        resp.status_code, (resp.text or "")[:300],
    )
    inv_id = str(
        payload.get("invitation_id")
        or payload.get("id")
        or payload.get("urn")
        or ""
    ) or None
    sent_at = _parse_iso(
        payload.get("sent_at") or payload.get("date") or payload.get("created_at")
    )
    # Cost tracker: invites are read-side-free + write-side-free under
    # Unipile's subscription pricing (we pay the seat, not the call).
    # Still emit a count so the cost dashboard's "calls" total reflects
    # all paid + unpaid Unipile actions.
    try:
        from app.services import cost_tracker as _ct
        _ct.record_unipile_call("invite")
    except Exception:  # noqa: BLE001
        pass  # never let a cost-recording failure swallow a real result

    return UnipileInvitationResult(
        invitation_id=inv_id,
        sent_at=sent_at,
        raw=payload,
    )


def get_invitation_status(
    *,
    account_id: str,
    provider_id: str,
) -> dict[str, Any]:
    """Read the current relationship state between ``account_id`` and
    ``provider_id``. Used by the background poller to flip
    ``linkedin_invitations.status`` from ``sent`` to ``accepted`` /
    ``declined`` / ``withdrawn``.

    Unipile exposes relationship state via
    ``GET /api/v1/users/relations/{provider_id}?account_id=...`` —
    response shape varies but includes a ``status``/``connection_status``
    field with values like ``CONNECTED``, ``INVITATION_SENT``,
    ``INVITATION_RECEIVED``, ``NOT_CONNECTED``. Callers must map those
    to our internal ``sent | accepted | declined | withdrawn`` vocabulary.

    Read-only call; bypasses the heavy invite throttle and rides only
    the default 3-6s gate via ``_request_with_429_retry``.
    """
    if settings.unipile_mock:
        raise UnipileNotConfigured("get_invitation_status: UNIPILE_MOCK=true")
    if not account_id or not provider_id:
        raise ValueError("get_invitation_status: account_id + provider_id required")
    pid = provider_id.strip()
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "GET",
            f"/users/relations/{_quote(pid, safe='')}",
            account_id=account_id,
            params={"account_id": account_id},
        )
    payload = _check_resp(resp, "get_invitation_status")
    return payload if isinstance(payload, dict) else {}


# ── First-touch DM (chat-start with initial message) ──────────────────────
#
# After an invite is accepted, the operator typically wants to send a
# first-touch DM that references the comment-thread context. Unipile's
# `POST /api/v1/chats` creates a new chat between the sending account and
# a list of attendees (passed as `attendees_ids`) and posts the supplied
# `text` as the first message in that chat. If a chat already exists with
# this attendee on this account, Unipile returns the existing chat_id
# rather than creating a duplicate — so this endpoint is idempotent
# enough for first-touch (we still dedup at the route layer to avoid
# burning a Unipile call).
#
# Throttle stack matches the invite path:
#   • per-account `_throttle_dm_call` (3-6 min between same-account DMs,
#     10% long-pause draw)
#   • universal `_throttle_default_call` (3-6s gap via _request_with_429_retry)
#   • pool aggregate gate (only when called via _pool_acquire("dm", …))
#
@dataclass(frozen=True)
class UnipileChatMessageResult:
    """Result of POST /chats. Unipile returns a chat_id (used for any
    follow-up messages in the same thread) and a message_id (the first
    message we just posted as part of chat creation). Both are kept on
    the linkedin_dms record for forensic audit + downstream reply polling."""
    chat_id: str | None
    message_id: str | None
    sent_at: datetime | None
    raw: dict[str, Any]


def send_dm(
    *,
    account_id: str,
    provider_id: str,
    text: str,
) -> UnipileChatMessageResult:
    """Send a first-touch DM to a LinkedIn 1st-degree connection.

    Unipile endpoint: ``POST /api/v1/chats`` with body
    ``{attendees_ids: [provider_id], text}`` and the sending account
    passed in the ``account_id`` query param. Unipile creates the chat
    if it doesn't exist OR posts to the existing chat with that attendee.

    Args:
        account_id: pool account that owns the LinkedIn session. The
            caller is responsible for ensuring this is the SAME account
            the recipient connected with (typically the one that posted
            the original comment and sent the invite).
        provider_id: LinkedIn URN of the DM recipient (ACoAA…).
        text: message body, ≤``LINKEDIN_DM_TEXT_MAX_CHARS`` chars (1500).

    Returns:
        UnipileChatMessageResult with chat_id + message_id from Unipile.

    Raises:
        ValueError: text too long / empty, or provider_id malformed.
        UnipileError: Unipile responded 4xx/5xx (most commonly 403 if the
            account-target pair is not yet 1st-degree connected, or 429 if
            the account has hit LinkedIn's daily messaging cap).
        UnipileNotConfigured: ``UNIPILE_MOCK`` is set or credentials missing.

    Safety: refuses to fire if ``UNIPILE_MOCK=true`` — mirrors the same
    safety contract as ``send_invitation``. The route layer adds dry-run
    default + per-target dedup + atomic claim on top.
    """
    if settings.unipile_mock:
        raise UnipileNotConfigured(
            "send_dm: UNIPILE_MOCK=true — refusing to issue a real DM. "
            "Set UNIPILE_MOCK=false to enable."
        )
    if not account_id:
        raise ValueError("send_dm: account_id is required")
    if not provider_id:
        raise ValueError("send_dm: provider_id is required")
    pid = provider_id.strip()
    if not (pid.startswith("AC") and len(pid) >= 30):
        raise ValueError(
            f"send_dm: provider_id must be the ACoAA… member URN form. "
            f"Got {pid[:24]!r}"
        )
    body_text = (text or "").strip()
    if not body_text:
        raise ValueError("send_dm: text is required (non-empty)")
    if len(body_text) > LINKEDIN_DM_TEXT_MAX_CHARS:
        raise ValueError(
            f"send_dm: text length {len(body_text)} exceeds "
            f"{LINKEDIN_DM_TEXT_MAX_CHARS}-char cap. Truncate before calling."
        )

    body: dict[str, Any] = {
        "attendees_ids": [pid],
        "text": body_text,
    }

    # Per-account DM cadence: 3-6 min typical, occasional 9-24 min long
    # pause. Stacks under the aggregate pool gate.
    _throttle_dm_call(account_id)

    # Forensic audit — log the wire payload before issuing. DMs are
    # visible to the recipient immediately so any "this DM went to the
    # wrong person" or "the text shows up garbled" report has hard
    # evidence within ~1 minute of the click. Text truncated to 80 chars
    # in the log for readability; full text is in Mongo at
    # `linkedin_dms.message_text`.
    log.warning(
        "send_dm WIRE → path=/chats  account=%s  target=%s  text_len=%d  text_starts=%r",
        account_id[:18],
        pid[:24],
        len(body_text),
        body_text[:80],
    )

    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "POST",
            "/chats",
            account_id=account_id,
            params={"account_id": account_id},
            json=body,
        )
    payload = _check_resp(resp, "send_dm")
    log.warning(
        "send_dm RESPONSE ← status=%d  body=%s",
        resp.status_code, (resp.text or "")[:300],
    )

    chat_id = str(
        payload.get("chat_id")
        or payload.get("id")
        or payload.get("urn")
        or ""
    ) or None
    message_id = str(
        payload.get("message_id")
        or payload.get("message", {}).get("id") if isinstance(payload.get("message"), dict) else ""
        or ""
    ) or None
    sent_at = _parse_iso(
        payload.get("sent_at") or payload.get("date") or payload.get("created_at")
    )

    # Cost tracker: same convention as invite — count the call so the
    # cost dashboard's total reflects all Unipile actions even though
    # DMs are seat-priced (no per-call fee).
    try:
        from app.services import cost_tracker as _ct
        _ct.record_unipile_call("dm")
    except Exception:  # noqa: BLE001
        pass

    return UnipileChatMessageResult(
        chat_id=chat_id,
        message_id=message_id,
        sent_at=sent_at,
        raw=payload,
    )


def _normalize_post_social_id(post_url_or_id: str) -> str:
    """Return a LinkedIn post URN suitable for Unipile's ``post_social_id``
    path parameter. Unipile's DELETE route validates the path against a
    URN pattern; bare numeric IDs return 404 (route-not-matched).

    Accepts any of:
      - ``urn:li:activity:7432779696146112512`` → returned as-is
      - ``7432779696146112512``                → wrapped to ``urn:li:activity:...``
      - ``https://www.linkedin.com/feed/update/urn:li:activity:7432...``
                                               → extracted + wrapped
      - ``https://www.linkedin.com/posts/...activity-7432...``
                                               → extracted + wrapped
    """
    raw = (post_url_or_id or "").strip()
    if raw.startswith("urn:li:"):
        return raw
    pid = extract_post_id_from_url(raw) or raw
    # If the URL embedded a urn: form, extract may have returned only the
    # numeric ID. Re-check raw for any URN substring; otherwise wrap.
    if pid.startswith("urn:li:"):
        return pid
    return f"urn:li:activity:{pid}"


def _normalize_comment_social_id(
    comment_id_or_urn: str, *, post_id_numeric: str | None = None
) -> str:
    """Return a LinkedIn comment URN suitable for Unipile's
    ``comment_social_id`` path parameter.

    Accepts:
      - ``urn:li:comment:(activity:X,Y)`` → returned as-is
      - bare numeric comment id (``7460373062606159872``) → wrapped to the
        full ``urn:li:comment:(activity:{post},{comment})`` form. Requires
        the parent post's numeric id (stored on the job at post-time).
    """
    raw = (comment_id_or_urn or "").strip()
    if raw.startswith("urn:li:comment:"):
        return raw
    if not post_id_numeric:
        # No way to construct a valid comment URN without the parent post.
        # Return as-is so the caller can decide whether to surface the
        # ambiguity. Unipile will likely 404 with a clearer message.
        return raw
    return f"urn:li:comment:(activity:{post_id_numeric},{raw})"


class UnipileFeatureNotSupported(UnipileError):
    """Raised when a Unipile endpoint we'd need is not exposed by their
    public API. Distinct from a transient ``UnipileError`` (network /
    rate-limit / auth) so callers can surface a different message and
    fall back to a manual workflow instead of retrying."""


def delete_comment(
    *,
    account_id: str,
    post_url_or_id: str,
    comment_id: str,
) -> dict[str, Any]:
    """**Not supported by Unipile.**

    Probed against Unipile's API on 2026-05-13 with 11 different URL +
    method combinations — every single one returned an HTTP-router 404
    ("Cannot DELETE/PATCH/PUT/POST ...") meaning no controller is
    registered for any comment-mutation route. The endpoints Unipile
    actually exposes for comments are:

      ✅ POST  /api/v1/posts/{post}/comments        (create)
      ✅ GET   /api/v1/posts/{post}/comments        (list)
      ❌ DELETE/PATCH/PUT — not registered, any shape

    The OPTIONS response advertises all methods in
    ``Access-Control-Allow-Methods`` but that's the CORS gateway's
    blanket allowlist, not actual route handlers — confirmed because
    each method 404s identically.

    To delete a comment posted via Unipile, the operator must go to
    LinkedIn directly (the comment's URL works in any browser) and use
    the native "Delete" affordance there. The `manual_comments` UI
    surfaces this via a two-step "Open on LinkedIn / Mark as deleted"
    affordance instead of a single Delete button.

    Raises ``UnipileFeatureNotSupported`` so the caller can render a
    distinct UX rather than treat this as a transient failure.
    """
    raise UnipileFeatureNotSupported(
        "Unipile does not expose a comment-delete endpoint. "
        "Delete the comment directly on LinkedIn (open the post URL in "
        "a browser), then mark the row as deleted in the manual-comments "
        "UI for audit. Confirmed across 11 URL + method shapes on "
        "2026-05-13 — see app/services/unipile.py:delete_comment docstring."
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
        resp = _request_with_429_retry(
            client, "POST", "/users/invite",
            account_id=account_id, json=body,
        )
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
    """Find the most recent account on this tenant whose `name` correlates.

    Unipile's hosted-auth flow creates a *new* account on each connect
    attempt (type:create), so when a cofounder has reconnected after a
    stale session, the tenant ends up with multiple accounts that share
    the same `cof_<id>` correlation name. Pick the NEWEST by created_at
    so reconnect actually replaces the dead one.
    """
    if settings.unipile_mock:
        return None
    with _client() as client:
        resp = _request_with_429_retry(
            client, "GET", "/accounts", params={"name": name, "limit": 25}
        )
    payload = _check_resp(resp, "find_account_by_name")
    items = payload.get("items") or []
    if not items:
        return None

    def _created_key(raw: dict[str, Any]) -> str:
        # Sort lexicographically on ISO-8601 created_at; falls back to "" so
        # accounts without the field land last.
        return str(raw.get("created_at") or "")

    items_sorted = sorted(items, key=_created_key, reverse=True)
    raw = items_sorted[0]
    return UnipileAccount(
        id=str(raw.get("id") or ""),
        name=raw.get("name") or "(unnamed)",
        profile_url=raw.get("profile_url") or (raw.get("params") or {}).get("profile_url"),
        avatar_url=raw.get("avatar_url"),
        account_type=raw.get("type") or raw.get("account_type"),
    )


# ---------------------------------------------------------------- profile lookup

def resolve_profile(*, account_id: str, public_identifier_or_url: str) -> dict[str, Any]:
    """Resolve a LinkedIn slug or URL to provider_id + member_urn + a
    minimal-but-useful work-experience snapshot.

    Sends ``linkedin_sections=experience`` so the response includes the
    ``work_experience`` array (each entry has ``company_id`` =
    LinkedIn company id, ``company`` name, ``position``, ``location``,
    ``description``). Without that param, Unipile returns only
    headline + location + names (verified via curl 2026-05-14) — which
    silently broke the discovery code that read ``raw.get("work_experience")``
    expecting populated entries.

    With the experience section we can derive ``employer_linkedin_id``
    from ``first_job.company_id`` and chain into APIDirect
    ``/v1/linkedin/company`` for structured industry/size/description,
    closing the "Unipile fallback authors have no industry" gap.

    Throttled with ~1.4s + 0..0.6s jitter between consecutive calls — this
    endpoint hits LinkedIn's `/users/{slug}` surface, which LinkedIn's
    automation-detection heuristics flag if hit too fast from the same
    session. Spacing mimics human browsing cadence."""
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
    _throttle_users_call(account_id)
    with _client() as client:
        resp = _request_with_429_retry(
            client,
            "GET",
            f"/users/{slug}",
            account_id=account_id,
            params={
                "account_id": account_id,
                # See docstring — without this, work_experience is absent.
                "linkedin_sections": "experience",
            },
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
