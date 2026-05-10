"""
Profile-resolve stage: enrich each verified candidate's author with title /
employer (+ seniority) using Crustdata and/or PDL.

Runs between cheap gates and expensive gates. Resolved fields are persisted
on the candidate doc so the ICP scorer can use real evidence.

Source behavior:
  1. Crustdata `/screener/person/enrich` when enrich_with_crustdata=true
     (batched; title, employer_name, headline, location).
  2. PDL `/v5/person/enrich` when enrich_with_pdl=true:
     - If Crustdata ran: supplement pass — always attach job_title_levels
       (seniority); fill author_title / author_company / author_name /
       author_headline only where still missing.
     - If Crustdata off: primary pass — same fields from PDL (title/company
       overwrite when PDL returns them, plus levels).
  3. No paid calls when both flags are false.

Cost defense: a URL pre-filter drops obvious brand-handle slugs before any
paid lookup.
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
    is_likely_person_slug,
)
from app.services.pdl import PDLError, PDLNotConfigured, PDLProfile, enrich_by_linkedin_url

log = logging.getLogger(__name__)


def resolve_profiles(db: Database, slate_run_id: ObjectId) -> dict[str, int]:
    counts: dict[str, int] = {
        "resolved": 0,
        "no_match": 0,
        "no_url": 0,
        "skipped_filtered": 0,
        "skipped_unconfigured": 0,
        "pdl_supplemented": 0,
    }

    crust_done = False
    if settings.enrich_with_crustdata:
        _resolve_via_crustdata(db, slate_run_id, counts)
        crust_done = True

    if settings.enrich_with_pdl:
        _resolve_via_pdl(
            db,
            slate_run_id,
            counts,
            prefer_existing_titles=crust_done,
        )
    elif not crust_done:
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
) -> None:
    """Batched Crustdata person enrichment."""
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
        return

    urls = list(by_url.keys())
    log.info(
        "profile_resolve(crustdata): %d unique person URLs to enrich (%d credits estimated)",
        len(urls),
        len(urls) * 3,
    )

    try:
        results = crustdata_enrich_profiles(urls)
    except CrustdataEnrichNotConfigured:
        counts["skipped_unconfigured"] = counts.get("skipped_unconfigured", 0) + sum(
            len(ids) for ids in by_url.values()
        )
        log.warning("profile_resolve(crustdata): unconfigured, skipped %d", counts["skipped_unconfigured"])
        return
    except CrustdataEnrichQuotaExhausted as err:
        log.warning("profile_resolve(crustdata): quota exhausted: %s", err)
        return
    except CrustdataEnrichError as err:
        log.warning("profile_resolve(crustdata): error: %s", err)
        return

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


def _pdl_fields_for_update(
    c: dict[str, Any],
    profile: PDLProfile,
    *,
    prefer_existing: bool,
) -> dict[str, Any]:
    """Map PDL profile → candidate $set fields.

    prefer_existing=True (after Crustdata): add seniority levels; only fill
    title/company/name/headline/location where the candidate is still blank.

    prefer_existing=False (PDL-only path): same title/company behavior as legacy
    PDL (overwrite title/company when PDL returns them); name only if missing.
    """
    update: dict[str, Any] = {}

    if profile.job_title_levels:
        update["author_title_levels"] = list(profile.job_title_levels)

    if profile.job_title:
        if not prefer_existing or not (c.get("author_title") or "").strip():
            update["author_title"] = profile.job_title
    if profile.job_company_name:
        if not prefer_existing or not (c.get("author_company") or "").strip():
            update["author_company"] = profile.job_company_name
    if profile.full_name and not (c.get("author_name") or "").strip():
        update["author_name"] = profile.full_name
    if profile.headline and not (c.get("author_headline") or "").strip():
        update["author_headline"] = profile.headline
    if prefer_existing and profile.location_country:
        if not (c.get("author_location") or "").strip():
            update["author_location"] = profile.location_country

    return update


def _resolve_via_pdl(
    db: Database,
    slate_run_id: ObjectId,
    counts: dict[str, int],
    *,
    prefer_existing_titles: bool,
) -> None:
    """PDL enrich per candidate (cached by LinkedIn URL in pdl.enrich_by_linkedin_url)."""
    cursor = db.candidates.find(
        {"slate_run_id": slate_run_id, "status": "cheap_gate_passed"},
    )
    for c in cursor:
        author_url = c.get("author_linkedin_url")
        if not author_url:
            counts["no_url"] += 1
            continue
        if not is_likely_person_slug(author_url):
            counts["skipped_filtered"] += 1
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

        patch = _pdl_fields_for_update(c, profile, prefer_existing=prefer_existing_titles)
        if not patch:
            counts["no_match"] += 1
            continue

        patch["updated_at"] = utcnow()
        db.candidates.update_one({"_id": c["_id"]}, {"$set": patch})

        if prefer_existing_titles:
            counts["pdl_supplemented"] = counts.get("pdl_supplemented", 0) + 1
        else:
            counts["resolved"] += 1

    mode = "pdl_supplement" if prefer_existing_titles else "pdl_primary"
    log.info("profile_resolve(%s): slate=%s %s", mode, slate_run_id, counts)
