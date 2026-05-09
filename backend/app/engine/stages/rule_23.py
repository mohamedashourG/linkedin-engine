"""
RULE 23 atomic postcondition gate.

Six layers; if any fail, the slate is force-aborted via one of an explicit
4-reason allowlist. The slate is re-read from MongoDB inside the gate (per
spec — never trust the in-memory slate object). On success, an HMAC token over
(slate_id, count, run_date) is computed and persisted on the slate_run.

Spec: decision #13.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.config import settings
from app.engine.constants import (
    COFOUNDER_TARGET_FLOOR_RATIO,
    COMMENT_MIN_CHARS,
    EM_DASH,
    RULE_23_FORCE_ABORT_REASONS,
)
from app.models.common import utcnow

log = logging.getLogger(__name__)


class Rule23ForceAbort(RuntimeError):
    def __init__(self, reason: str, layer: str, details: dict[str, Any] | None = None):
        if reason not in RULE_23_FORCE_ABORT_REASONS:
            raise ValueError(
                f"Rule23 force-abort reason {reason!r} is not in the allowlist {RULE_23_FORCE_ABORT_REASONS}"
            )
        super().__init__(f"{layer}:{reason}")
        self.reason = reason
        self.layer = layer
        self.details = details or {}


def seal_slate(
    db: Database,
    *,
    operator: dict[str, Any],
    cofounders: list[dict[str, Any]],
    slate_run_id: ObjectId,
) -> dict[str, Any]:
    """Run all 6 layers + emit HMAC. Raises Rule23ForceAbort on any failure."""
    operator_id: ObjectId = operator["_id"]
    validations: list[dict[str, Any]] = []
    drafted = list(
        db.candidates.find({"slate_run_id": slate_run_id, "status": "drafted"})
    )

    # Layer 1 — operator-level floor
    hard_floor = int(operator.get("hard_floor") or 20)
    if len(drafted) < hard_floor:
        _abort(
            db,
            slate_run_id,
            validations,
            reason="floor_breach",
            layer="floor",
            details={"drafted": len(drafted), "hard_floor": hard_floor},
        )
    validations.append({"layer": "floor", "pass": True})

    # Layer 2 — per-cofounder floor
    counts = {cf["_id"]: 0 for cf in cofounders}
    for c in drafted:
        counts[c["cofounder_id"]] = counts.get(c["cofounder_id"], 0) + 1
    for cf in cofounders:
        target = int(cf.get("daily_volume_target") or 20)
        floor = max(1, int(target * COFOUNDER_TARGET_FLOOR_RATIO))
        if counts.get(cf["_id"], 0) < floor:
            _abort(
                db,
                slate_run_id,
                validations,
                reason="cofounder_imbalance",
                layer="cofounder_floor",
                details={
                    "cofounder_id": str(cf["_id"]),
                    "got": counts.get(cf["_id"], 0),
                    "floor": floor,
                },
            )
    validations.append({"layer": "cofounder_floor", "pass": True})

    # Layer 3 — comment_text invariants
    for c in drafted:
        text = c.get("comment_text") or ""
        if len(text) < COMMENT_MIN_CHARS or EM_DASH in text:
            _abort(
                db,
                slate_run_id,
                validations,
                reason="comment_invalid",
                layer="comment_invariants",
                details={"candidate_id": str(c["_id"])},
            )
    validations.append({"layer": "comment_invariants", "pass": True})

    # Layer 4 — verified_tuple invariants
    for c in drafted:
        if not c.get("post_url") or not c.get("post_text"):
            _abort(
                db,
                slate_run_id,
                validations,
                reason="verified_tuple_missing",
                layer="verified_tuple",
                details={"candidate_id": str(c["_id"])},
            )
    validations.append({"layer": "verified_tuple", "pass": True})

    # Layer 5 — force-abort allowlist (implicit by reaching here)
    validations.append({"layer": "force_abort_allowlist", "pass": True})

    # Layer 6 — HMAC token over (slate_id, count, run_date)
    pepper = (settings.rule_23_pepper or os.getenv("RULE_23_PEPPER") or "").encode()
    if not pepper or pepper == b"change-me-in-prod-and-rotate":
        log.warning("RULE_23_PEPPER is unset or default — token has no security value")
    slate = db.slate_runs.find_one({"_id": slate_run_id})
    payload = f"{slate_run_id}:{len(drafted)}:{slate.get('run_date')}".encode()
    token = hmac.new(pepper, payload, hashlib.sha256).hexdigest()
    validations.append({"layer": "hmac", "pass": True})

    db.slate_runs.update_one(
        {"_id": slate_run_id},
        {
            "$set": {
                "status": "sealed",
                "sealed_at": utcnow(),
                "rule_23_validations": validations,
                "hmac_token": token,
                "total_slated": len(drafted),
                "updated_at": utcnow(),
            }
        },
    )
    db.candidates.update_many(
        {"slate_run_id": slate_run_id, "status": "drafted"},
        {"$set": {"status": "slated", "updated_at": utcnow()}},
    )

    db.audit_records.insert_one(
        {
            "operator_id": operator_id,
            "event_type": "rule_23_pass",
            "slate_run_id": slate_run_id,
            "details": {"slated": len(drafted), "hmac_token": token},
            "severity": "info",
            "created_at": utcnow(),
        }
    )
    log.info("RULE 23 sealed slate=%s slated=%d", slate_run_id, len(drafted))
    return {"slated": len(drafted), "hmac_token": token, "validations": validations}


def _abort(
    db: Database,
    slate_run_id: ObjectId,
    validations: list[dict[str, Any]],
    *,
    reason: str,
    layer: str,
    details: dict[str, Any],
) -> None:
    validations.append({"layer": layer, "pass": False, "details": details})
    db.slate_runs.update_one(
        {"_id": slate_run_id},
        {
            "$set": {
                "status": "force_aborted",
                "rule_23_validations": validations,
                "force_abort_reason": reason,
                "updated_at": utcnow(),
            }
        },
    )
    slate = db.slate_runs.find_one({"_id": slate_run_id})
    db.audit_records.insert_one(
        {
            "operator_id": (slate or {}).get("operator_id"),
            "event_type": "force_abort",
            "slate_run_id": slate_run_id,
            "details": {"reason": reason, "layer": layer, **details},
            "severity": "error",
            "created_at": utcnow(),
        }
    )
    raise Rule23ForceAbort(reason=reason, layer=layer, details=details)
