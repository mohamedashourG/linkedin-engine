"""
Embedded ICP harvester. Re-reads today's gate-dropped posts (non_buyer +
analyst-reportage) and asks gpt-4.1-mini to extract ICP-relevant names mentioned
*inside* the posts, e.g. "we just hired Jane Rivera as VP Eng at Stripe…" →
seed `Jane Rivera @ Stripe` for tomorrow's discovery.

Six recognized patterns per spec:
  - name_title_of_company   ("Jane Rivera, VP Eng of Stripe")
  - conference_speaker      ("Jane Rivera spoke at...")
  - title_name_company      ("VP Eng Jane Rivera at Stripe")
  - promotion_active        ("we promoted Jane to VP")
  - promotion_passive       ("Jane was promoted to VP")
  - noun_appointment        ("Jane Rivera, our new VP Eng")
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pydantic import BaseModel, Field
from pymongo.database import Database

from app.models.common import utcnow
from app.services.openai_client import OpenAINotConfigured, parse_structured_sync

log = logging.getLogger(__name__)

_SEED_TTL_DAYS = 7

_PATTERNS = (
    "name_title_of_company",
    "conference_speaker",
    "title_name_company",
    "promotion_active",
    "promotion_passive",
    "noun_appointment",
)


class _Mention(BaseModel):
    name: str = Field(description="Full name of the person mentioned.")
    title: str = Field(default="", description="Their title, if stated.")
    company: str = Field(default="", description="Their company, if stated.")
    pattern: str = Field(
        description=(
            "One of: name_title_of_company, conference_speaker, "
            "title_name_company, promotion_active, promotion_passive, "
            "noun_appointment."
        )
    )


class _Extraction(BaseModel):
    mentions: list[_Mention] = Field(
        description=(
            "All third-party people mentioned in the post who could plausibly "
            "be ICP buyers or champions. Exclude the author themselves."
        )
    )


_SYSTEM = """You are a B2B GTM scraper. Given a LinkedIn post, extract every named third-party person mentioned in the body who could plausibly be an ICP buyer or champion for an outreach engine. Skip the post author themselves; we only want OTHER people they reference.

For each mention, classify the structural pattern of how they were mentioned (one of: name_title_of_company, conference_speaker, title_name_company, promotion_active, promotion_passive, noun_appointment).

Skip mentions that are obviously not buyers (kids, family, celebrities unrelated to B2B, etc.). Skip mentions where you can't pin down a real name. When in doubt, omit."""


def harvest_from_dropped(db: Database, operator_id: ObjectId) -> dict[str, int]:
    """
    Read today's gate-dropped non_buyer/analyst posts; call LLM to extract
    ICP names; insert each as a `discovery_seeds` row with status=pending,
    expires_at=now+7d. Idempotent: existing seeds with matching name+company
    aren't duplicated.
    """
    cutoff = utcnow() - timedelta(days=1)
    cursor = db.candidates.find(
        {
            "operator_id": operator_id,
            "status": "gate_dropped",
            "drop_reason": {"$regex": "^(non_buyer|analyst)"},
            "created_at": {"$gte": cutoff},
        }
    )
    candidates = list(cursor)
    if not candidates:
        return {"posts_scanned": 0, "extracted": 0, "inserted": 0, "duplicates": 0}

    inserted = 0
    duplicates = 0
    extracted_total = 0

    for c in candidates:
        post_text = (c.get("post_text") or "").strip()
        if len(post_text) < 80:
            continue
        try:
            result = parse_structured_sync(
                model_tier="cheap",
                system=_SYSTEM,
                user=f"Post:\n{post_text}",
                schema=_Extraction,
            )
        except OpenAINotConfigured as err:
            log.warning("harvester: openai not configured: %s", err)
            return {
                "posts_scanned": len(candidates),
                "extracted": extracted_total,
                "inserted": inserted,
                "duplicates": duplicates,
                "skipped_unconfigured": True,
            }
        except Exception as err:
            log.warning("harvester: extraction failed for candidate %s: %s", c["_id"], err)
            continue

        for m in result.mentions:
            extracted_total += 1
            name = (m.name or "").strip()
            if not name:
                continue
            company = (m.company or "").strip() or None
            title = (m.title or "").strip() or None
            pattern = m.pattern if m.pattern in _PATTERNS else "name_title_of_company"

            # Idempotency: skip if a pending seed already exists with same
            # operator + name + company.
            exists = db.discovery_seeds.find_one(
                {
                    "operator_id": operator_id,
                    "extracted_name": name,
                    "extracted_company": company,
                    "status": "pending",
                }
            )
            if exists:
                duplicates += 1
                continue

            now = utcnow()
            db.discovery_seeds.insert_one(
                {
                    "operator_id": operator_id,
                    "source": "embedded_harvester",
                    "source_candidate_id": c["_id"],
                    "extracted_name": name,
                    "extracted_title": title,
                    "extracted_company": company,
                    "linkedin_url": None,
                    "pattern": pattern,
                    "status": "pending",
                    "expires_at": now + timedelta(days=_SEED_TTL_DAYS),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            inserted += 1

    db.audit_records.insert_one(
        {
            "operator_id": operator_id,
            "event_type": "harvester_seeds_added",
            "details": {
                "posts_scanned": len(candidates),
                "extracted": extracted_total,
                "inserted": inserted,
                "duplicates": duplicates,
            },
            "severity": "info",
            "created_at": utcnow(),
        }
    )
    log.info(
        "harvester: scanned=%d extracted=%d inserted=%d dup=%d",
        len(candidates),
        extracted_total,
        inserted,
        duplicates,
    )
    return {
        "posts_scanned": len(candidates),
        "extracted": extracted_total,
        "inserted": inserted,
        "duplicates": duplicates,
    }
