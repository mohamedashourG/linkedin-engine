"""
Allocator: from the gate-passed candidates, choose which ones make today's
slate, and assign each one a comment type A-F respecting the operator's quotas.

Two passes:
  1. Per-cofounder allocation — each cofounder gets up to their daily_volume_target,
     pulled in ICP-score-descending order from candidates assigned to them.
  2. Comment-type assignment — within each cofounder's bucket, distribute types
     A-F by their quota percentages, biasing higher-effort types (A, B) toward
     higher-scoring posts.
"""
from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.engine.constants import COMMENT_TYPE_QUOTAS_DEFAULT
from app.models.common import utcnow

log = logging.getLogger(__name__)


def _candidate_score(c: dict[str, Any]) -> int:
    icp = (c.get("gate_results") or {}).get("icp") or {}
    return int(icp.get("total", 0) or 0)


def allocate(
    db: Database,
    *,
    operator: dict[str, Any],
    cofounders: list[dict[str, Any]],
    slate_run_id: ObjectId,
) -> dict[str, Any]:
    survivors_cursor = db.candidates.find(
        {"slate_run_id": slate_run_id, "status": "gate_passed"}
    )
    survivors = list(survivors_cursor)
    by_cofounder: dict[ObjectId, list[dict[str, Any]]] = {
        cf["_id"]: [] for cf in cofounders
    }
    for c in survivors:
        by_cofounder.setdefault(c["cofounder_id"], []).append(c)

    quotas = operator.get("comment_quotas") or {
        k: list(v) for k, v in {k: list(rng) for k, rng in COMMENT_TYPE_QUOTAS_DEFAULT.items()}.items()
    }
    quotas_pct = {k: tuple(v) for k, v in quotas.items()}

    allocated_total = 0
    per_cf_summary: dict[str, dict[str, int]] = {}

    for cofounder in cofounders:
        cf_id = cofounder["_id"]
        target = int(cofounder.get("daily_volume_target", 20))
        bucket = sorted(by_cofounder.get(cf_id, []), key=_candidate_score, reverse=True)
        chosen = bucket[:target]

        type_assignments = _assign_types(len(chosen), quotas_pct)
        for c, comment_type in zip(chosen, type_assignments):
            db.candidates.update_one(
                {"_id": c["_id"]},
                {
                    "$set": {
                        "comment_type": comment_type,
                        "status": "allocated",
                        "updated_at": utcnow(),
                    }
                },
            )

        per_cf_summary[str(cf_id)] = {
            "floor": int(target * 0.7),
            "drafted": 0,
            "shipped": 0,
            "allocated": len(chosen),
            "available": len(bucket),
        }
        allocated_total += len(chosen)

    db.slate_runs.update_one(
        {"_id": slate_run_id},
        {
            "$set": {
                "per_cofounder_counts": per_cf_summary,
                "total_allocated": allocated_total,
                "updated_at": utcnow(),
            }
        },
    )
    log.info(
        "allocator: slate=%s allocated=%d across %d cofounders",
        slate_run_id,
        allocated_total,
        len(cofounders),
    )
    return per_cf_summary


def _assign_types(
    n: int, quotas_pct: dict[str, tuple[float | int, float | int]]
) -> list[str]:
    """
    Use the midpoint of each quota range as the target percentage. Round to
    integer counts; pad/trim to exactly N. Order: highest-effort types first
    (A, B, C, D, E, F) so top-scoring posts get heavier comments.
    """
    if n <= 0:
        return []
    ordered_types = ["A", "B", "C", "D", "E", "F"]

    def midpoint(rng: tuple[float | int, float | int]) -> float:
        lo, hi = rng
        lo_v = float(lo) / 100.0 if float(lo) > 1 else float(lo)
        hi_v = float(hi) / 100.0 if float(hi) > 1 else float(hi)
        return (lo_v + hi_v) / 2.0

    target_counts = {t: round(n * midpoint(quotas_pct.get(t, (0, 0)))) for t in ordered_types}
    total = sum(target_counts.values())
    diff = n - total
    if diff != 0:
        target_counts["A"] = max(0, target_counts.get("A", 0) + diff)

    out: list[str] = []
    for t in ordered_types:
        out.extend([t] * target_counts.get(t, 0))
    return out[:n] + ["A"] * max(0, n - len(out[:n]))
