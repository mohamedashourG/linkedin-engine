"""
Profile-resolve stage: enrich each verified candidate's author with title /
company from PDL.

Runs between verification and the gates. PDL hits get persisted onto the
candidate document (author_title, author_company) so the ICP scorer can use
real evidence instead of inferring from snippet alone.

When PDL has no record OR PDL_API_KEY isn't configured, the stage skips the
candidate quietly — the ICP scorer will fall back to inference from the post
text. The stage never blocks the pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.models.common import utcnow
from app.services.pdl import PDLError, PDLNotConfigured, enrich_by_linkedin_url

log = logging.getLogger(__name__)


def resolve_profiles(db: Database, slate_run_id: ObjectId) -> dict[str, int]:
    counts = {"resolved": 0, "no_match": 0, "no_url": 0, "skipped_unconfigured": 0}
    cursor = db.candidates.find(
        {"slate_run_id": slate_run_id, "status": "verified"}
    )

    for c in cursor:
        author_url = c.get("author_linkedin_url")
        if not author_url:
            counts["no_url"] += 1
            continue
        try:
            profile = enrich_by_linkedin_url(author_url)
        except PDLNotConfigured:
            counts["skipped_unconfigured"] += 1
            continue
        except PDLError as err:
            log.warning("pdl error for %s: %s", author_url, err)
            counts["no_match"] += 1
            continue

        if not profile:
            counts["no_match"] += 1
            continue

        update: dict[str, Any] = {"updated_at": utcnow()}
        if profile.job_title:
            update["author_title"] = profile.job_title
        if profile.job_company_name:
            update["author_company"] = profile.job_company_name
        if profile.full_name and not c.get("author_name"):
            update["author_name"] = profile.full_name

        if len(update) > 1:
            db.candidates.update_one({"_id": c["_id"]}, {"$set": update})
            counts["resolved"] += 1
        else:
            counts["no_match"] += 1

    log.info("profile_resolve: slate=%s %s", slate_run_id, counts)
    return counts
