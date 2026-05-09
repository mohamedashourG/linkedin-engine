"""
Allocator: from the gate-passed candidates, choose which ones make today's
slate, and assign each one a comment type A-F respecting the operator's quotas.

Two passes:
  1. Per-cofounder allocation — each cofounder gets up to their daily_volume_target,
     pulled in ICP-score-descending order from candidates assigned to them.
  2. Comment-type assignment (RULE 7 floor-then-fill) — within each cofounder's
     bucket, every type gets at least its floor share, then remaining slots
     fill the type with the most headroom up to its cap. The audit's locked
     defaults (A cap 25, B cap 10, C floor 25, D 10-15, E floor 20, F 5-10)
     ensure the worst performers (A, B) can't dominate while the best (E)
     and the workhorse (C) get guaranteed presence.
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

    quotas = operator.get("comment_quotas") or COMMENT_TYPE_QUOTAS_DEFAULT

    allocated_total = 0
    per_cf_summary: dict[str, dict[str, int]] = {}

    for cofounder in cofounders:
        cf_id = cofounder["_id"]
        target = int(cofounder.get("daily_volume_target", 20))
        bucket = sorted(by_cofounder.get(cf_id, []), key=_candidate_score, reverse=True)
        chosen = bucket[:target]

        type_assignments = _assign_types(len(chosen), quotas)
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


_ORDERED_TYPES = ("A", "B", "C", "D", "E", "F")


def _normalize_quota(value: Any) -> dict[str, float]:
    """Coerce the user's per-type quota record into {floor, cap} as fractions
    of 1.0.

    Accepts:
      - {"floor": pct, "cap": pct}     ← new shape (RULE 7 audit 2026-05-05)
      - [lo, hi] / (lo, hi)            ← legacy shape; lo→floor, hi→cap

    Percentages > 1 are interpreted as 0-100 (so 25 → 0.25); values ≤ 1 as
    fractions of 1 already. Missing or malformed entries default to {0, 1}
    (no constraint), which is the safe-rather-than-strict choice."""
    def _pct(v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return f / 100.0 if f > 1 else f

    if isinstance(value, dict):
        return {"floor": _pct(value.get("floor", 0)), "cap": _pct(value.get("cap", 1))}
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lo, hi = value
        return {"floor": _pct(lo), "cap": _pct(hi)}
    return {"floor": 0.0, "cap": 1.0}


def _assign_types(n: int, quotas: dict[str, Any]) -> list[str]:
    """RULE 7 floor-then-fill allocation.

    Step 1: each type gets round(n * floor) slots.
    Step 2: distribute remaining slots to types with positive headroom
            (cap - current), greedy by descending headroom.
    Step 3: trim if rounding overshot, by removing slack-most-from-floor
            types one at a time.

    Final padding (if quotas are degenerate and we still don't have N slots)
    spills to the highest-cap type rather than always to type A."""
    if n <= 0:
        return []

    norm = {t: _normalize_quota(quotas.get(t)) for t in _ORDERED_TYPES}
    floor_count = {t: int(round(n * norm[t]["floor"])) for t in _ORDERED_TYPES}
    cap_count = {t: int(n * norm[t]["cap"]) for t in _ORDERED_TYPES}

    # Floor must not exceed cap. If they cross (degenerate quota), respect cap.
    counts = {t: min(floor_count[t], cap_count[t]) for t in _ORDERED_TYPES}

    total = sum(counts.values())
    if total < n:
        # Fill remaining by descending headroom (cap - current); ties broken
        # by ordered_types position (favors A first only when equal).
        remaining = n - total
        for _ in range(remaining):
            headroom = {t: cap_count[t] - counts[t] for t in _ORDERED_TYPES}
            best: str | None = None
            best_room = 0
            for t in _ORDERED_TYPES:
                if headroom[t] > best_room:
                    best = t
                    best_room = headroom[t]
            if best is None:
                break  # all caps full; degenerate quotas — stop adding
            counts[best] += 1
    elif total > n:
        # Trim. Pick types with the most slack ABOVE floor, never going below.
        excess = total - n
        for _ in range(excess):
            slack = {t: counts[t] - floor_count[t] for t in _ORDERED_TYPES}
            worst: str | None = None
            worst_slack = 0
            for t in _ORDERED_TYPES:
                if slack[t] > worst_slack:
                    worst = t
                    worst_slack = slack[t]
            if worst is None:
                break  # everyone is at floor — stop trimming
            counts[worst] -= 1

    out: list[str] = []
    for t in _ORDERED_TYPES:
        out.extend([t] * counts[t])

    # Defensive padding: if degenerate quotas left us short, fall back to the
    # type with the highest cap (rather than always type A which the audit
    # caps at 25%).
    if len(out) < n:
        spill = max(_ORDERED_TYPES, key=lambda t: cap_count[t])
        out.extend([spill] * (n - len(out)))
    return out[:n]
