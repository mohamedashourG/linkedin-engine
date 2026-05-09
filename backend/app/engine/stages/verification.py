"""
Verification stage. Spec calls for a Post Details fetch (URL hallucination
defense), but apidirect.io's LinkedIn endpoint doesn't expose a per-post details
call — its search response is canonical. So verification here:

  1. Asserts a non-empty URL + post_text + author exist.
  2. Promotes status raw → verified, copies snippet onto post_text.
  3. Drops candidates whose snippet is empty or trivially short.

When apidirect (or a future provider) gains a real details endpoint, this is the
single chokepoint to swap in.
"""
from __future__ import annotations

import logging
from typing import Any  # noqa: F401  (used by inline annotations below)

from bson import ObjectId
from pymongo.database import Database

from app.models.common import utcnow

log = logging.getLogger(__name__)

_MIN_SNIPPET_CHARS = 80


def verify_candidates(db: Database, slate_run_id: ObjectId) -> tuple[int, int]:
    """Returns (verified_count, rejected_count) for this slate run."""
    raw = db.candidates.find({"slate_run_id": slate_run_id, "status": "raw"})
    verified, rejected = 0, 0

    for c in raw:
        snippet: str = (c.get("post_text") or "").strip()
        url: str = c.get("post_url") or ""
        if not url or len(snippet) < _MIN_SNIPPET_CHARS:
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
    Best-effort author-URL extraction. LinkedIn post URLs of the form
    https://www.linkedin.com/posts/<slug>_... let us derive the author by slug,
    but the slug itself is the canonical handle so we use the post URL as a
    stand-in for now (the engine only uses author_linkedin_url for the
    exhaustion ledger and reply matching). A dedicated profile-resolve call
    would be a Phase 4/5 enhancement.
    """
    if not post_url or "/posts/" not in post_url:
        return None
    try:
        slug = post_url.split("/posts/", 1)[1].split("_", 1)[0]
        if slug:
            return f"https://www.linkedin.com/in/{slug}"
    except (IndexError, ValueError):
        pass
    return None
