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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


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
        resp = client.get("/accounts", params={"limit": 50})
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
    return UnipilePost(
        id=str(raw.get("id") or raw.get("social_id") or raw.get("urn") or ""),
        url=url or "",
        text=text or "",
        author_name=author.get("name")
        or " ".join(filter(None, [author.get("first_name"), author.get("last_name")])).strip()
        or None,
        author_title=author.get("headline") or author.get("title") or author.get("occupation"),
        author_company=author.get("company") or author.get("company_name"),
        author_profile_url=author.get("public_profile_url")
        or author.get("profile_url")
        or author.get("url"),
        published_at=_parse_iso(raw.get("date") or raw.get("created_at") or raw.get("published_at")),
    )


def search_posts(*, account_id: str, query: str, limit: int = 20) -> list[UnipilePost]:
    """
    LinkedIn keyword post search via Unipile. Uses the connected cofounder's
    LinkedIn account (account_id) to run the search.

    Endpoint shape from Unipile docs: POST /linkedin/search with
    {api: 'classic', category: 'posts', keywords: ...}.
    """
    if settings.unipile_mock or not query.strip():
        return []
    body = {
        "api": "classic",
        "category": "posts",
        "keywords": query[:300],
    }
    with _client() as client:
        resp = client.post(
            "/linkedin/search",
            params={"account_id": account_id, "limit": limit},
            json=body,
        )
    payload = _check_resp(resp, "search_posts")
    items = payload.get("items") or payload.get("results") or []
    out: list[UnipilePost] = []
    for raw in items:
        post = _parse_unipile_post(raw)
        if post:
            out.append(post)
    return out


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
        network_distance=str(
            raw.get("network_distance")
            or raw.get("distance")
            or raw.get("connection_degree")
            or ""
        ).strip("Dd")  # tolerate "DISTANCE_2" / "2nd" — strip trailing letters
        or None,
    )


def search_people(
    *,
    account_id: str,
    query: str,
    limit: int = 10,
    geo_urns: tuple[str, ...] = (LINKEDIN_GEO_URN_US,),
    network_distances: tuple[str, ...] = ("S",),  # "S" = 2nd-degree per LinkedIn convention
) -> list[UnipilePerson]:
    """RULE 24 — LinkedIn people search via Unipile. Default filter is US +
    2nd-degree, matching the audit's URL.

    Endpoint shape mirrors search_posts: POST /linkedin/search with
    {api: 'classic', category: 'people', keywords, geo_urns, network_distance}.
    The network_distance + geo_urns filter shape may need a tweak per Unipile
    tenant; this function is the single chokepoint to adjust if so."""
    if settings.unipile_mock or not query.strip():
        return []
    body: dict[str, Any] = {
        "api": "classic",
        "category": "people",
        "keywords": query[:300],
    }
    if geo_urns:
        body["geo_urns"] = list(geo_urns)
    if network_distances:
        body["network_distance"] = list(network_distances)
    with _client() as client:
        resp = client.post(
            "/linkedin/search",
            params={"account_id": account_id, "limit": limit},
            json=body,
        )
    payload = _check_resp(resp, "search_people")
    items = payload.get("items") or payload.get("results") or []
    out: list[UnipilePerson] = []
    for raw in items:
        person = _parse_unipile_person(raw)
        if person:
            out.append(person)
    return out


def get_user_posts(
    *, account_id: str, public_identifier_or_url: str, limit: int = 10
) -> list[UnipilePost]:
    """Fetch a specific LinkedIn user's recent posts."""
    if settings.unipile_mock:
        return []
    slug = public_identifier_or_url
    if "/in/" in slug:
        slug = slug.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if not slug:
        return []
    with _client() as client:
        resp = client.get(
            f"/users/{slug}/posts",
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
        resp = client.get(
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
            )
        )
    return out


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
        resp = client.post("/users/invite", json=body)
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
        resp = client.post("/hosted/accounts/link", json=body)
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
        resp = client.get("/accounts", params={"name": name, "limit": 5})
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
        resp = client.get(f"/users/{slug}", params={"account_id": account_id})
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
