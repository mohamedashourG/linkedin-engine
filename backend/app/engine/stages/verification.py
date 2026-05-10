"""
Verification stage. Spec calls for a Post Details fetch (URL hallucination
defense), but apidirect.io's LinkedIn endpoint doesn't expose a per-post details
call — its search response is canonical. So verification here:

  1. Asserts a non-empty URL + post_text + author exist.
  2. Drops posts older than DISCOVERY_MAX_AGE_DAYS (recency filter).
  3. Promotes status raw → verified, copies snippet onto post_text.
  4. Drops candidates whose snippet is empty or trivially short.

When apidirect (or a future provider) gains a real details endpoint, this is the
single chokepoint to swap in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any  # noqa: F401  (used by inline annotations below)

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.models.common import utcnow

log = logging.getLogger(__name__)

_MIN_SNIPPET_CHARS = 80
_MIN_SNIPPET_CHARS_CONTACT_SEED = 32


def _operator_geo_terms(operator: dict[str, Any]) -> list[str]:
    """Same geography sources as discovery `_compose_discovery_query` (kept local to avoid import cycles)."""
    ext = operator.get("product_extracted") or {}
    raw = ext.get("target_geographies") or []
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if len(s) >= 2:
            out.append(s.lower())
    rub = operator.get("icp_rubric") or {}
    geo = rub.get("geography") or {}
    for tier in geo.get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        for m in tier.get("matches") or []:
            s = str(m).strip()
            if len(s) >= 2:
                out.append(s.lower())
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:15]


def _haystack_matches_geo(haystack: str, geos: list[str]) -> bool:
    if not geos:
        return True
    h = haystack.lower()
    return any(g in h for g in geos)


def _coerce_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            v = value
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            dt = datetime.fromisoformat(v)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return None


def verify_candidates(
    db: Database,
    slate_run_id: ObjectId,
    *,
    operator: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Returns (verified_count, rejected_count) for this slate run."""
    raw = db.candidates.find({"slate_run_id": slate_run_id, "status": "raw"})
    verified, rejected = 0, 0

    max_age_days = settings.discovery_max_age_days
    cutoff: datetime | None = (
        utcnow() - timedelta(days=max_age_days) if max_age_days > 0 else None
    )

    geos: list[str] = []
    if operator and settings.discovery_require_geo_in_post:
        geos = _operator_geo_terms(operator)

    for c in raw:
        snippet: str = (c.get("post_text") or "").strip()
        url: str = c.get("post_url") or ""
        min_len = (
            _MIN_SNIPPET_CHARS_CONTACT_SEED
            if c.get("source") == "contact_seed"
            else _MIN_SNIPPET_CHARS
        )
        if not url or len(snippet) < min_len:
            log.info(
                "│  [DROP/verify] %s  ←  empty_or_thin_snippet",
                (url or "<no-url>")[:90],
            )
            db.candidates.update_one(
                {"_id": c["_id"]},
                {
                    "$set": {
                        "status": "rejected_url_mismatch",
                        "drop_reason": "empty_or_thin_snippet",
                        "updated_at": utcnow(),
                    }
                },
            )
            rejected += 1
            continue

        if geos:
            hay = " ".join(
                str(x or "")
                for x in (
                    snippet,
                    c.get("author_title"),
                    c.get("author_company"),
                    c.get("author_name"),
                )
            )
            if not _haystack_matches_geo(hay, geos):
                log.info(
                    "│  [DROP/verify] %s  ←  geo_not_in_post_or_author",
                    url[:90],
                )
                db.candidates.update_one(
                    {"_id": c["_id"]},
                    {
                        "$set": {
                            "status": "rejected_url_mismatch",
                            "drop_reason": "geo_not_in_post_or_author",
                            "updated_at": utcnow(),
                        }
                    },
                )
                rejected += 1
                continue

        # Recency gate: drop posts older than max_age_days. Candidates with
        # no parseable published_at are KEPT (don't penalize missing data —
        # apidirect omits dates for ~30% of posts).
        if cutoff is not None:
            published = _coerce_datetime(c.get("post_published_at"))
            if published is not None and published < cutoff:
                log.info(
                    "│  [DROP/verify] %s  ←  too_old (>%dd)",
                    url[:90],
                    max_age_days,
                )
                db.candidates.update_one(
                    {"_id": c["_id"]},
                    {
                        "$set": {
                            "status": "rejected_url_mismatch",
                            "drop_reason": f"too_old (>{max_age_days}d)",
                            "updated_at": utcnow(),
                        }
                    },
                )
                rejected += 1
                continue

        update: dict[str, Any] = {
            "status": "verified",
            "post_text": snippet,
            "updated_at": utcnow(),
        }
        # Only derive author_linkedin_url from the post URL if we don't already
        # have one from Unipile (Unipile returns the author profile URL inline).
        if not c.get("author_linkedin_url"):
            update["author_linkedin_url"] = _author_url_from_post_url(url)
        db.candidates.update_one({"_id": c["_id"]}, {"$set": update})
        verified += 1

    log.info(
        "verification: slate=%s verified=%d rejected=%d",
        slate_run_id,
        verified,
        rejected,
    )
    return verified, rejected


def _author_url_from_post_url(post_url: str) -> str | None:
    """
    Best-effort author-URL extraction from a LinkedIn post URL.

    Recognised forms (most common first):
      /posts/<slug>_<rest>-activity-<id>-<hash>     → slug = `<slug>`
      /posts/<slug>-activity-<id>-<hash>            → slug = `<slug>` (no underscore)

    Returns None for /pulse/ articles and /feed/update/ URN forms — those
    don't carry an author handle in the URL.
    """
    if not post_url or "/posts/" not in post_url:
        return None
    try:
        tail = post_url.split("/posts/", 1)[1]
        # Drop query/fragment if any leaked through
        tail = tail.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        # Two separator conventions: `<slug>_<rest>` or `<slug>-activity-<id>`.
        # Prefer `_` since LinkedIn always uses it when present.
        if "_" in tail:
            slug = tail.split("_", 1)[0]
        elif "-activity-" in tail:
            slug = tail.split("-activity-", 1)[0]
        else:
            slug = tail
        if slug:
            return f"https://www.linkedin.com/in/{slug}"
    except (IndexError, ValueError):
        pass
    return None
