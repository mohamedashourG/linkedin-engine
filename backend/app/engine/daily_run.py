"""
Daily run orchestrator. Runs sync inside a Celery worker.

Pipeline (per spec):
  1. Discovery
  2. Verification
  3. 4-gate filter (sequential, drop on first fail)
  4. Allocation (per-cofounder + comment type)
  5. Drafting + validator
  6. RULE 23 atomic gate
  7. Slate email

Each stage emits an audit_record. Force-aborts halt the pipeline immediately.
"""
from __future__ import annotations

import logging
from datetime import date as date_type, datetime, time, timezone
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.engine.email_delivery import send_slate_email
from app.engine.stages import (
    allocator,
    discovery,
    drafter,
    profile_resolve,
    validator,
    verification,
)
from app.engine.stages.gates import (
    analyst_reportage,
    icp_scoring,
    non_buyer,
    post_quality,
)
from app.engine.stages.rule_23 import Rule23ForceAbort, seal_slate
from app.models.common import utcnow
from app.services.email import EmailNotConfigured

log = logging.getLogger(__name__)


def run_for_operator(db: Database, operator_id: ObjectId) -> dict[str, Any]:
    operator = db.users.find_one({"_id": operator_id})
    if not operator:
        return {"status": "operator_not_found"}
    if operator.get("paused"):
        return {"status": "paused"}
    if not operator.get("onboarding_complete"):
        return {"status": "onboarding_incomplete"}

    cofounders = list(
        db.cofounders.find({"operator_id": operator_id, "active": True})
    )
    if not cofounders:
        return {"status": "no_active_cofounders"}

    slate_run_id = _open_slate_run(db, operator_id)
    log.info("daily_run started: operator=%s slate=%s", operator_id, slate_run_id)

    try:
        # 1. Discovery
        discovered = discovery.discover_for_operator(
            db,
            operator=operator,
            cofounders=cofounders,
            slate_run_id=slate_run_id,
        )
        _audit(db, operator_id, slate_run_id, "stage_complete", "discovery", {"count": discovered})

        # 2. Verification
        verified, rejected = verification.verify_candidates(db, slate_run_id)
        _audit(db, operator_id, slate_run_id, "stage_complete", "verification", {"verified": verified, "rejected": rejected})

        # 2b. Profile resolve (PDL) — fill author_title / author_company so the
        # ICP scorer has explicit evidence. Best-effort: missing PDL data falls
        # through to the LLM-inference fallback inside icp_scoring.
        resolve_counts = profile_resolve.resolve_profiles(db, slate_run_id)
        _audit(db, operator_id, slate_run_id, "stage_complete", "profile_resolve", resolve_counts)

        # 3. Gates (sequential)
        gate_counts = _run_gates(
            db,
            slate_run_id=slate_run_id,
            operator=operator,
        )
        _audit(db, operator_id, slate_run_id, "stage_complete", "gates", gate_counts)

        # 4. Allocation
        per_cf = allocator.allocate(
            db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
        )
        _audit(db, operator_id, slate_run_id, "stage_complete", "allocator", {"summary": per_cf})

        # 5. Drafting + validation
        drafted_count = _run_drafter(db, slate_run_id=slate_run_id, cofounders=cofounders)
        _audit(db, operator_id, slate_run_id, "stage_complete", "drafter", {"drafted": drafted_count})

        # 6. RULE 23
        seal = seal_slate(
            db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
        )

        # 7. Email
        try:
            msg_id = send_slate_email(
                db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
            )
            _audit(db, operator_id, slate_run_id, "stage_complete", "email_delivery", {"message_id": msg_id})
        except EmailNotConfigured as err:
            log.warning("email skipped: %s", err)
            _audit(db, operator_id, slate_run_id, "stage_error", "email_delivery", {"error": str(err)}, severity="warn")

        log.info("daily_run sealed: slate=%s slated=%d", slate_run_id, seal["slated"])
        return {"status": "sealed", "slate_run_id": str(slate_run_id), **seal}

    except Rule23ForceAbort as err:
        log.error("RULE 23 force-aborted: layer=%s reason=%s details=%s", err.layer, err.reason, err.details)
        return {
            "status": "force_aborted",
            "reason": err.reason,
            "layer": err.layer,
            "details": err.details,
        }
    except Exception as err:
        log.exception("daily_run unexpected failure")
        _audit(db, operator_id, slate_run_id, "stage_error", "pipeline", {"error": str(err)}, severity="error")
        db.slate_runs.update_one(
            {"_id": slate_run_id},
            {"$set": {"status": "force_aborted", "force_abort_reason": "internal_error", "updated_at": utcnow()}},
        )
        return {"status": "error", "error": str(err)}


def _open_slate_run(db: Database, operator_id: ObjectId) -> ObjectId:
    today = datetime.now(timezone.utc).date()
    now = utcnow()
    doc = {
        "operator_id": operator_id,
        "run_date": datetime.combine(today, time.min, tzinfo=timezone.utc),
        "status": "building",
        "total_discovered": 0,
        "total_verified": 0,
        "total_gated": 0,
        "total_drafted": 0,
        "total_slated": 0,
        "per_cofounder_counts": {},
        "rule_23_validations": [],
        "hmac_token": None,
        "email_sent": False,
        "email_message_id": None,
        "sealed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    return db.slate_runs.insert_one(doc).inserted_id


def _run_gates(
    db: Database, *, slate_run_id: ObjectId, operator: dict[str, Any]
) -> dict[str, int]:
    counts = {"non_buyer": 0, "analyst": 0, "icp_low": 0, "post_quality": 0, "passed": 0}
    product_summary = (operator.get("product_description") or "")[:1500]
    rubric = operator.get("icp_rubric") or {}

    verified = list(db.candidates.find({"slate_run_id": slate_run_id, "status": "verified"}))
    for c in verified:
        post_text = c.get("post_text") or ""
        author = c.get("author_name")

        try:
            nb = non_buyer.evaluate(
                post_text=post_text, author_name=author, product_summary=product_summary
            )
        except Exception as err:
            _drop(db, c, f"gate_error_non_buyer: {err}")
            continue
        if nb.drop:
            _drop(db, c, f"non_buyer: {nb.reason}", gate_results={"non_buyer": nb.model_dump()})
            counts["non_buyer"] += 1
            continue

        try:
            ar = analyst_reportage.evaluate(post_text=post_text, author_name=author)
        except Exception as err:
            _drop(db, c, f"gate_error_analyst: {err}")
            continue
        if ar.drop:
            _drop(db, c, f"analyst: {ar.reason}", gate_results={"non_buyer": nb.model_dump(), "analyst": ar.model_dump()})
            counts["analyst"] += 1
            continue

        try:
            icp = icp_scoring.evaluate(
                post_text=post_text,
                author_name=author,
                author_title=c.get("author_title"),
                author_company=c.get("author_company"),
                icp_rubric=rubric,
            )
        except Exception as err:
            _drop(db, c, f"gate_error_icp: {err}")
            continue
        threshold = int(rubric.get("threshold", 6))
        if icp.total < threshold:
            _drop(
                db,
                c,
                f"icp: {icp.total} < {threshold}",
                gate_results={
                    "non_buyer": nb.model_dump(),
                    "analyst": ar.model_dump(),
                    "icp": icp.model_dump(),
                },
            )
            counts["icp_low"] += 1
            continue

        try:
            pq = post_quality.evaluate(post_text=post_text)
        except Exception as err:
            _drop(db, c, f"gate_error_quality: {err}")
            continue
        if pq.drop:
            _drop(
                db,
                c,
                f"quality: {pq.reason}",
                gate_results={
                    "non_buyer": nb.model_dump(),
                    "analyst": ar.model_dump(),
                    "icp": icp.model_dump(),
                    "post_quality": pq.model_dump(),
                },
            )
            counts["post_quality"] += 1
            continue

        # All four gates passed.
        db.candidates.update_one(
            {"_id": c["_id"]},
            {
                "$set": {
                    "status": "gate_passed",
                    "gate_results": {
                        "non_buyer": nb.model_dump(),
                        "analyst": ar.model_dump(),
                        "icp": icp.model_dump(),
                        "post_quality": pq.model_dump(),
                    },
                    "updated_at": utcnow(),
                }
            },
        )
        counts["passed"] += 1

    return counts


def _drop(
    db: Database,
    candidate: dict[str, Any],
    reason: str,
    *,
    gate_results: dict[str, Any] | None = None,
) -> None:
    update: dict[str, Any] = {
        "status": "gate_dropped",
        "drop_reason": reason,
        "updated_at": utcnow(),
    }
    if gate_results:
        update["gate_results"] = gate_results
    db.candidates.update_one({"_id": candidate["_id"]}, {"$set": update})


def _run_drafter(
    db: Database, *, slate_run_id: ObjectId, cofounders: list[dict[str, Any]]
) -> int:
    by_id = {cf["_id"]: cf for cf in cofounders}
    drafted = 0
    allocated = list(db.candidates.find({"slate_run_id": slate_run_id, "status": "allocated"}))
    for c in allocated:
        cofounder = by_id.get(c["cofounder_id"])
        if not cofounder or not cofounder.get("voice_profile"):
            _drop(db, c, "drafter_no_voice")
            continue
        voice = cofounder["voice_profile"]
        template = (
            voice.get("source_a_template")
            if c.get("source_classification") == "A"
            else voice.get("source_b_template")
        )
        if not template:
            _drop(db, c, "drafter_missing_template")
            continue

        try:
            comment_text = drafter.draft_comment(
                post_text=c.get("post_text") or "",
                author_name=c.get("author_name"),
                voice_template=template,
                comment_type=c.get("comment_type") or "A",
                source_classification=c.get("source_classification") or "A",
            )
        except Exception as err:
            log.warning("drafter failed for candidate=%s: %s", c["_id"], err)
            _drop(db, c, f"drafter_error: {err}")
            continue

        result = validator.validate(comment_text, c.get("comment_type") or "A")
        if not result.ok:
            _drop(db, c, f"validator: {result.reason}")
            continue

        db.candidates.update_one(
            {"_id": c["_id"]},
            {
                "$set": {
                    "status": "drafted",
                    "comment_text": comment_text,
                    "updated_at": utcnow(),
                }
            },
        )
        drafted += 1
    return drafted


def _audit(
    db: Database,
    operator_id: ObjectId,
    slate_run_id: ObjectId,
    event_type: str,
    stage: str,
    details: dict[str, Any],
    *,
    severity: str = "info",
) -> None:
    db.audit_records.insert_one(
        {
            "operator_id": operator_id,
            "event_type": event_type,
            "stage": stage,
            "slate_run_id": slate_run_id,
            "details": details,
            "severity": severity,
            "created_at": utcnow(),
        }
    )
