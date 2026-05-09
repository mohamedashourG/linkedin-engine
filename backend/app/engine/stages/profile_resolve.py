"""
Profile-resolve stage: enrich each verified candidate's author with title /
employer using Crustdata (preferred) or PDL (fallback).

Runs between verification and the gates. Resolved title/company are persisted
on the candidate doc so the ICP scorer can use real evidence instead of
inferring from snippet alone.

Source priority:
  1. Crustdata `/screener/person/enrich` — batched 25/call, returns title +
     employer_name + headline. Gated by ENRICH_WITH_CRUSTDATA.
  2. PDL `/v5/person/enrich` — per-call, in practice returns name only.
     Gated by ENRICH_WITH_PDL. Used only when Crustdata is disabled.
  3. No-op when both flags are false.

Cost defense: a URL pre-filter drops obvious brand-handle slugs
(linkedin.com/in/<x>-com|-app|-ai|-jobs etc.) before any paid lookup.
apidirect-derived URLs are ~35% non-people, so this is non-trivial savings.
"""
from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.models.common import utcnow
from app.services.crustdata_enrich import (
    CrustdataEnrichError,
    CrustdataEnrichNotConfigured,
    CrustdataEnrichQuotaExhausted,
    enrich_profiles as crustdata_enrich_profiles,
    filter_likely_person_urls,
    is_likely_person_slug,
)
from app.services.pdl import PDLError, PDLNotConfigured, enrich_by_linkedin_url

log = logging.getLogger(__name__)


def resolve_profiles(db: Database, slate_run_id: ObjectId) -> dict[str, int]:
    counts = {
        "resolved": 0,
        "no_match": 0,
        "no_url": 0,
        "skipped_filtered": 0,
        "skipped_unconfigured": 0,
    }

    if settings.enrich_with_crustdata:
        return _resolve_via_crustdata(db, slate_run_id, counts)
    if settings.enrich_with_pdl:
        return _resolve_via_pdl(db, slate_run_id, counts)

    skipped = db.candidates.count_documents(
        {"slate_run_id": slate_run_id, "status": "cheap_gate_passed"}
    )
    counts["skipped_unconfigured"] = skipped
    log.info(
        "profile_resolve: enrichment disabled (crustdata=false, pdl=false), skipped %d",
        skipped,
    )
    return counts


def _resolve_via_crustdata(
    db: Database, slate_run_id: ObjectId, counts: dict[str, int]
) -> dict[str, int]:
    """Batched Crustdata person enrichment. Pre-filters obvious brand-handle
    URLs to avoid paying credits on guaranteed no-matches."""
    cursor = db.candidates.find(
        {"slate_run_id": slate_run_id, "status": "cheap_gate_passed"},
        {"author_linkedin_url": 1},
    )
    by_url: dict[str, list[ObjectId]] = {}
    for c in cursor:
        url = c.get("author_linkedin_url")
        if not url:
            counts["no_url"] += 1
            continue
        if not is_likely_person_slug(url):
            counts["skipped_filtered"] += 1
            continue
        by_url.setdefault(url, []).append(c["_id"])

    if not by_url:
        log.info("profile_resolve(crustdata): nothing to enrich after URL filter %s", counts)
        return counts

    urls = list(by_url.keys())
    log.info(
        "profile_resolve(crustdata): %d unique person URLs to enrich (%d credits estimated)",
        len(urls),
        len(urls) * 3,
    )

    try:
        results = crustdata_enrich_profiles(urls)
    except CrustdataEnrichNotConfigured:
        counts["skipped_unconfigured"] = sum(len(ids) for ids in by_url.values())
        log.warning("profile_resolve(crustdata): unconfigured, skipped %d", counts["skipped_unconfigured"])
        return counts
    except CrustdataEnrichQuotaExhausted as err:
        log.warning("profile_resolve(crustdata): quota exhausted: %s", err)
        return counts
    except CrustdataEnrichError as err:
        log.warning("profile_resolve(crustdata): error: %s", err)
        return counts

    now = utcnow()
    for url, candidate_ids in by_url.items():
        profile = results.get(url)
        if not profile:
            counts["no_match"] += 1
            continue
        update: dict[str, Any] = {"updated_at": now}
        if profile.title:
            update["author_title"] = profile.title
        if profile.employer_name:
            update["author_company"] = profile.employer_name
        if profile.name:
            update["author_name_resolved"] = profile.name
        if profile.headline:
            update["author_headline"] = profile.headline
        if profile.location:
            update["author_location"] = profile.location
        if len(update) > 1:
            db.candidates.update_many(
                {"_id": {"$in": candidate_ids}},
                {"$set": update},
            )
            counts["resolved"] += len(candidate_ids)
        else:
            counts["no_match"] += len(candidate_ids)

    log.info("profile_resolve(crustdata): slate=%s %s", slate_run_id, counts)
    return counts


def _resolve_via_pdl(
    db: Database, slate_run_id: ObjectId, counts: dict[str, int]
) -> dict[str, int]:
    cursor = db.candidates.find(
        {"slate_run_id": slate_run_id, "status": "cheap_gate_passed"}
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

    log.info("profile_resolve(pdl): slate=%s %s", slate_run_id, counts)
    return counts
