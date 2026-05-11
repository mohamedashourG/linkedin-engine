"""
Daily run orchestrator. Runs sync inside a Celery worker.

Pipeline (per spec):
  1. Discovery
  2. Verification
  3. 4-gate filter (cheap then expensive; sequential, drop on first fail)
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

from app.engine.constants import REFRAME_OVER_REPRESENTATION_THRESHOLD
from app.engine.email_delivery import send_slate_email
from app.engine.stages import (
    allocator,
    discovery,
    drafter,
    validator,
    verification,
)
from app.engine.stages.gates import (
    analyst_reportage,
    icp_scoring,
    non_buyer,
    post_quality,
)
from app.config import settings
from app.engine.stages.rule_23 import Rule23ForceAbort, seal_slate, seal_slate_skip_checks
from app.models.common import utcnow
from app.services import client_config
from app.services.email import EmailNotConfigured

log = logging.getLogger(__name__)


_STAGE_ETA_SECONDS = {
    # Per-candidate cost estimates from observed runs. Used to project ETA the
    # moment a stage starts. The gates stage dominates because each candidate
    # runs ~4 LLM calls.
    "discovery": 0.05,           # ~6s for 13 keyword searches
    "verification": 0.005,
    "gates": 1.20,                # ~5 min for 250 candidates × 4 gates
    "allocator": 0.02,
    "drafter": 8.0,               # per-survivor LLM call
    "rebalance": 6.0,
    "rule_23": 0.5,
    "email_delivery": 1.0,
}

_STAGE_ORDER = (
    "discovery",
    "verification",
    "gates",
    "allocator",
    "drafter",
    "rule_23",
    "email_delivery",
)


def _stage_eta_seconds(
    stage: str, processed: int, total: int, started_at: datetime
) -> int:
    """Estimated seconds remaining. Uses observed throughput once we have at
    least 5 processed; else falls back to the table above."""
    if total <= 0 or processed >= total:
        return 0
    elapsed = (utcnow() - started_at).total_seconds()
    if processed >= 5:
        per = elapsed / processed
    else:
        per = _STAGE_ETA_SECONDS.get(stage, 1.0)
    remaining = (total - processed) * per
    return max(1, int(remaining))


def _set_stage(
    db: Database,
    slate_run_id: ObjectId,
    stage: str,
    *,
    processed: int = 0,
    total: int = 0,
    started_at: datetime | None = None,
    note: str | None = None,
) -> None:
    """Write the current stage + progress + ETA so the frontend can show a
    real-time progress bar."""
    started_at = started_at or utcnow()
    eta = _stage_eta_seconds(stage, processed, total, started_at)
    update: dict[str, Any] = {
        "current_stage": stage,
        "stage_progress": {"processed": processed, "total": total},
        "stage_started_at": started_at,
        "stage_eta_seconds": eta,
        "updated_at": utcnow(),
    }
    if note:
        update["stage_note"] = note
    db.slate_runs.update_one({"_id": slate_run_id}, {"$set": update})


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
    pipeline_t0 = utcnow()
    log.info("┌── daily_run start  operator=%s  slate=%s", operator_id, slate_run_id)

    try:
        # 1-3. Discovery → verification → gates (cheap then expensive), with up to
        # _MAX_TOP_UP_ROUNDS top-up passes targeting cofounders below their
        # RULE 23 per-cofounder floor. Each round only fetches/processes for
        # the deficit cofounders, so cost scales with how short we are, not
        # how many cofounders the operator has.
        cof_ids = [cf["_id"] for cf in cofounders]
        gate_counts = {"non_buyer": 0, "analyst": 0, "icp_low": 0, "post_quality": 0, "passed": 0}
        verified_total = 0

        for round_num in range(_MAX_TOP_UP_ROUNDS + 1):
            round_label = "discovery" if round_num == 0 else f"discovery_topup_{round_num}"
            round_cofounders = (
                cofounders
                if round_num == 0
                else _cofounders_below_floor(
                    cofounders,
                    _per_cofounder_passed_counts(db, slate_run_id=slate_run_id, cofounder_ids=cof_ids),
                )
            )
            if round_num > 0 and not round_cofounders:
                log.info("│  [topup]       all cofounders meet floor — no top-up needed")
                break
            if round_num > 0:
                short_names = [cf.get("display_name", "?") for cf in round_cofounders]
                log.info("│  [topup]       round %d for %s", round_num, short_names)

            # 1. Discovery (initial OR top-up for deficit cofounders)
            t = utcnow()
            _set_stage(db, slate_run_id, round_label, started_at=t, note="searching LinkedIn")
            discovered = discovery.discover_for_operator(
                db,
                operator=operator,
                cofounders=round_cofounders,
                slate_run_id=slate_run_id,
                crustdata_simulation_ping=(round_num == 0),
            )
            log.info("│  [%-13s] %d candidates  (%.1fs)", round_label, discovered, (utcnow() - t).total_seconds())
            _audit(db, operator_id, slate_run_id, "stage_complete", round_label, {"count": discovered})

            # 2. Verification (only newly-raw rows)
            t = utcnow()
            _set_stage(db, slate_run_id, "verification", started_at=t, total=discovered, note="checking URLs + text")
            verified, rejected = verification.verify_candidates(
                db, slate_run_id, operator=operator
            )
            verified_total += verified
            log.info("│  [verify]      %d verified, %d rejected  (%.1fs)", verified, rejected, (utcnow() - t).total_seconds())
            _audit(db, operator_id, slate_run_id, "stage_complete", "verification", {"verified": verified, "rejected": rejected, "round": round_num})

            # 2b. CHEAP gates (non_buyer + post_quality) — title-free, drops ~70%
            t = utcnow()
            _set_stage(db, slate_run_id, "gates", started_at=t, total=verified, note=f"cheap gates (round {round_num})")
            cheap_counts = _run_gates(
                db,
                slate_run_id=slate_run_id,
                operator=operator,
                stage_started_at=t,
                phase="cheap",
            )
            cheap_passed = cheap_counts.get("passed", 0)
            log.info(
                "│  [cheap_gates] %d passed / %d (drops: nb=%d q=%d)  (%.1fs)",
                cheap_passed,
                verified,
                cheap_counts.get("non_buyer", 0),
                cheap_counts.get("post_quality", 0),
                (utcnow() - t).total_seconds(),
            )
            _audit(db, operator_id, slate_run_id, "stage_complete", f"cheap_gates_round_{round_num}", cheap_counts)

            # 2c. EXPENSIVE gates (analyst + icp_scoring) — author fields come from
            # discovery (e.g. Unipile inline / post payload), not a separate enrich stage.
            t = utcnow()
            _set_stage(db, slate_run_id, "gates", started_at=t, total=cheap_passed, note=f"expensive gates (round {round_num})")
            expensive_counts = _run_gates(
                db,
                slate_run_id=slate_run_id,
                operator=operator,
                stage_started_at=t,
                phase="expensive",
            )
            log.info(
                "│  [exp_gates r%d] %d passed / %d (drops: an=%d icp=%d)  (%.1fs)",
                round_num,
                expensive_counts.get("passed", 0),
                cheap_passed,
                expensive_counts.get("analyst", 0),
                expensive_counts.get("icp_low", 0),
                (utcnow() - t).total_seconds(),
            )
            _audit(db, operator_id, slate_run_id, "stage_complete", f"expensive_gates_round_{round_num}", expensive_counts)

            # Merge into the cumulative gate_counts for downstream reporting.
            round_gate_counts = {
                "non_buyer": cheap_counts.get("non_buyer", 0),
                "post_quality": cheap_counts.get("post_quality", 0),
                "analyst": expensive_counts.get("analyst", 0),
                "icp_low": expensive_counts.get("icp_low", 0),
                "passed": expensive_counts.get("passed", 0),
            }
            for k, v in round_gate_counts.items():
                gate_counts[k] = gate_counts.get(k, 0) + v

        _audit(db, operator_id, slate_run_id, "stage_complete", "gates", {**gate_counts, "verified_total": verified_total})

        # 4. Allocation
        t = utcnow()
        _set_stage(db, slate_run_id, "allocator", started_at=t, note="picking top survivors per cofounder")
        per_cf = allocator.allocate(
            db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
        )
        allocated_total = sum(v.get("allocated", 0) for v in per_cf.values())
        log.info("│  [allocator]   %d allocated across %d cofounders  (%.1fs)", allocated_total, len(per_cf), (utcnow() - t).total_seconds())
        _audit(db, operator_id, slate_run_id, "stage_complete", "allocator", {"summary": per_cf})

        # 5. Drafter + validator + slate-level rebalance
        t = utcnow()
        _set_stage(db, slate_run_id, "drafter", started_at=t, total=allocated_total, note="drafting voice-matched comments")
        drafted_count = _run_drafter(db, slate_run_id=slate_run_id, cofounders=cofounders)
        log.info("│  [drafter]     %d drafted, validators all pass  (%.1fs)", drafted_count, (utcnow() - t).total_seconds())
        _audit(db, operator_id, slate_run_id, "stage_complete", "drafter", {"drafted": drafted_count})

        # 6. RULE 23 (optional bypass: settings.skip_rule_23 / SKIP_RULE_23)
        t = utcnow()
        if settings.skip_rule_23:
            _set_stage(
                db,
                slate_run_id,
                "rule_23",
                started_at=t,
                note="skipped (skip_rule_23)",
            )
            seal = seal_slate_skip_checks(
                db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
            )
            log.info(
                "│  [rule_23]     skipped floors; sealed slated=%d  hmac=%s…  (%.1fs)",
                seal["slated"],
                (seal.get("hmac_token") or "")[:12],
                (utcnow() - t).total_seconds(),
            )
        else:
            _set_stage(db, slate_run_id, "rule_23", started_at=t, note="atomic 6-layer gate")
            seal = seal_slate(
                db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
            )
            log.info(
                "│  [rule_23]     sealed slated=%d  hmac=%s…  (%.1fs)",
                seal["slated"],
                (seal.get("hmac_token") or "")[:12],
                (utcnow() - t).total_seconds(),
            )

        # 7. Email
        t = utcnow()
        _set_stage(db, slate_run_id, "email_delivery", started_at=t, note="sending slate email")
        try:
            msg_id = send_slate_email(
                db, operator=operator, cofounders=cofounders, slate_run_id=slate_run_id
            )
            log.info("│  [email]       sent  message_id=%s  (%.1fs)", msg_id, (utcnow() - t).total_seconds())
            _audit(db, operator_id, slate_run_id, "stage_complete", "email_delivery", {"message_id": msg_id})
        except EmailNotConfigured as err:
            log.warning("│  [email]       skipped: %s", err)
            _audit(db, operator_id, slate_run_id, "stage_error", "email_delivery", {"error": str(err)}, severity="warn")

        # Mark complete.
        _set_stage(db, slate_run_id, "complete", note="sealed")
        total_secs = (utcnow() - pipeline_t0).total_seconds()
        log.info("└── daily_run sealed  slate=%s  slated=%d  total=%.1fs", slate_run_id, seal["slated"], total_secs)
        return {"status": "sealed", "slate_run_id": str(slate_run_id), **seal}

    except Rule23ForceAbort as err:
        log.error("└── RULE 23 force-aborted  layer=%s  reason=%s  %s", err.layer, err.reason, err.details)
        _set_stage(db, slate_run_id, "aborted", note=f"{err.layer}:{err.reason}")
        return {
            "status": "force_aborted",
            "reason": err.reason,
            "layer": err.layer,
            "details": err.details,
        }
    except Exception as err:
        log.exception("└── daily_run unexpected failure")
        _audit(db, operator_id, slate_run_id, "stage_error", "pipeline", {"error": str(err)}, severity="error")
        db.slate_runs.update_one(
            {"_id": slate_run_id},
            {"$set": {"status": "force_aborted", "force_abort_reason": "internal_error", "current_stage": "aborted", "updated_at": utcnow()}},
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


_GATES_MAX_WORKERS = 10
_MAX_TOP_UP_ROUNDS = 2  # initial round + up to 2 top-ups (3 total max per slate)


def _per_cofounder_passed_counts(
    db: Database,
    *,
    slate_run_id: ObjectId,
    cofounder_ids: list[ObjectId],
) -> dict[ObjectId, int]:
    """Count gate-passed candidates per cofounder for this slate run."""
    counts = {cid: 0 for cid in cofounder_ids}
    cursor = db.candidates.find(
        {"slate_run_id": slate_run_id, "status": "gate_passed"},
        {"cofounder_id": 1},
    )
    for d in cursor:
        cid = d.get("cofounder_id")
        if cid in counts:
            counts[cid] += 1
    return counts


def _cofounders_below_floor(
    cofounders: list[dict[str, Any]],
    counts: dict[ObjectId, int],
) -> list[dict[str, Any]]:
    """Return the subset of cofounders whose gate-passed count is below their
    RULE 23 per-cofounder floor (`max(1, target * COFOUNDER_TARGET_FLOOR_RATIO)`).
    Used to decide whether to run a discovery top-up round."""
    from app.engine.constants import COFOUNDER_TARGET_FLOOR_RATIO

    short: list[dict[str, Any]] = []
    for cf in cofounders:
        target = int(cf.get("daily_volume_target") or 20)
        floor = max(1, int(target * COFOUNDER_TARGET_FLOOR_RATIO))
        if counts.get(cf["_id"], 0) < floor:
            short.append(cf)
    return short


def _evaluate_cheap_gates(
    c: dict[str, Any], *, product_summary: str
) -> tuple[str, dict[str, Any]]:
    """Phase A: cheap gates that don't need author title.

    Runs non_buyer first (highest drop rate, ~65%), then post_quality. Both
    operate on post_text + author_name only — no enrichment data required.

    verdict ∈ {"non_buyer", "post_quality", "cheap_passed", "error_<gate>"}
    """
    post_text = c.get("post_text") or ""
    author = c.get("author_name")

    try:
        nb = non_buyer.evaluate(
            post_text=post_text, author_name=author, product_summary=product_summary
        )
    except Exception as err:
        return "error_non_buyer", {"err": str(err)}
    if nb.drop:
        return "non_buyer", {"reason": nb.reason, "gate_results": {"non_buyer": nb.model_dump()}}

    try:
        pq = post_quality.evaluate(post_text=post_text)
    except Exception as err:
        return "error_quality", {"err": str(err), "gate_results": {"non_buyer": nb.model_dump()}}
    if pq.drop:
        return "post_quality", {
            "reason": pq.reason,
            "gate_results": {"non_buyer": nb.model_dump(), "post_quality": pq.model_dump()},
        }

    return "cheap_passed", {
        "gate_results": {"non_buyer": nb.model_dump(), "post_quality": pq.model_dump()}
    }


def _evaluate_expensive_gates(
    c: dict[str, Any],
    *,
    rubric: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Phase C: expensive gates (analyst + ICP).

    ICP uses author_title, author_company, and optional author_title_levels
    from discovery (e.g. Unipile post + inline profile) when present. Existing
    gate_results from the cheap phase are merged in.

    verdict ∈ {"analyst", "icp_low", "passed", "error_<gate>"}
    """
    post_text = c.get("post_text") or ""
    author = c.get("author_name")
    prior = c.get("gate_results") or {}
    nb_dump = prior.get("non_buyer")
    pq_dump = prior.get("post_quality")

    try:
        ar = analyst_reportage.evaluate(post_text=post_text, author_name=author)
    except Exception as err:
        return "error_analyst", {"err": str(err)}
    if ar.drop:
        return "analyst", {
            "reason": ar.reason,
            "gate_results": {**prior, "analyst": ar.model_dump()},
        }

    lv_raw = c.get("author_title_levels")
    title_levels = lv_raw if isinstance(lv_raw, list) else None

    try:
        icp = icp_scoring.evaluate(
            post_text=post_text,
            author_name=author,
            author_title=c.get("author_title"),
            author_company=c.get("author_company"),
            author_location=c.get("author_location"),
            author_title_levels=title_levels,
            icp_rubric=rubric,
            # When the candidate came from a source that filtered geo or
            # industry server-side (RULE 24 people-search with geoUrn /
            # INDUSTRY filter), the LLM auto-credits those axes at the
            # rubric's top tier — no re-evaluation that could wrongly drop
            # verified candidates whose location string is region-shaped.
            geo_verified_at_source=bool(c.get("geo_verified_at_source")),
            industry_verified_at_source=bool(c.get("industry_verified_at_source")),
        )
    except Exception as err:
        return "error_icp", {"err": str(err)}
    threshold = int(rubric.get("threshold", 6))
    icp_dump = icp.model_dump()
    # RULE 14: project the raw axis-sum onto a 0-10 score and gate on that.
    # `threshold` (default 6) means "drop if score_0_10 < 6", i.e., drop ≤5.
    icp_dump["score_0_10"] = icp_scoring.compute_score_0_10(icp.total, rubric)
    merged = {**prior, "analyst": ar.model_dump(), "icp": icp_dump}
    if icp_dump["score_0_10"] < threshold:
        return "icp_low", {
            "reason": f"icp: score_0_10={icp_dump['score_0_10']} < {threshold}",
            "gate_results": merged,
        }

    return "passed", {"gate_results": merged}


def _run_gates(
    db: Database,
    *,
    slate_run_id: ObjectId,
    operator: dict[str, Any],
    stage_started_at: datetime | None = None,
    phase: str,  # "cheap" or "expensive"
) -> dict[str, int]:
    """Run one phase of gates, parallelized across candidates.

    phase="cheap"     → reads status="verified", writes "cheap_gate_passed" or drops
    phase="expensive" → reads status="cheap_gate_passed", writes "gate_passed" or drops

    Verified candidates are sorted newest-first (by post_published_at desc,
    None last) so freshest posts are processed first — matters under any
    cap (top-up failures, time limits, etc.)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if phase not in ("cheap", "expensive"):
        raise ValueError(f"unknown phase: {phase}")

    counts = {"non_buyer": 0, "analyst": 0, "icp_low": 0, "post_quality": 0, "passed": 0}
    product_summary = (operator.get("product_description") or "")[:1500]
    # Per-user rubric overlays the client baseline (RULE 20). On axes the user
    # left blank, the client config's tier list is used.
    cfg = client_config.for_operator(operator)
    rubric = client_config.merge_icp_rubric(operator.get("icp_rubric"), cfg.icp_rubric)

    source_status = "verified" if phase == "cheap" else "cheap_gate_passed"
    target_status = "cheap_gate_passed" if phase == "cheap" else "gate_passed"
    candidates = list(db.candidates.find({"slate_run_id": slate_run_id, "status": source_status}))

    # Newest first — None dates sorted last so they don't crowd out fresh posts.
    def _sort_key(c: dict[str, Any]) -> Any:
        pub = c.get("post_published_at")
        if pub is None:
            return (1, 0)  # bucket: missing dates last
        if isinstance(pub, str):
            try:
                v = pub
                if v.endswith("Z"):
                    v = v[:-1] + "+00:00"
                pub = datetime.fromisoformat(v)
            except (TypeError, ValueError):
                return (1, 0)
        if hasattr(pub, "tzinfo") and pub.tzinfo is None:
            from datetime import timezone as _tz
            pub = pub.replace(tzinfo=_tz.utc)
        return (0, -pub.timestamp())  # bucket: dated posts first, newest first

    candidates.sort(key=_sort_key)

    total = len(candidates)
    if total == 0:
        return counts
    started = stage_started_at or utcnow()
    PROGRESS_EVERY = 5

    def _eval(c: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if phase == "cheap":
            return _evaluate_cheap_gates(c, product_summary=product_summary)
        return _evaluate_expensive_gates(c, rubric=rubric)

    processed = 0
    with ThreadPoolExecutor(max_workers=_GATES_MAX_WORKERS) as pool:
        future_to_candidate = {pool.submit(_eval, c): c for c in candidates}
        for fut in as_completed(future_to_candidate):
            c = future_to_candidate[fut]
            try:
                verdict, payload = fut.result()
            except Exception as err:
                _drop(db, c, f"gate_error_unexpected: {err}")
                processed += 1
                continue

            if verdict in ("passed", "cheap_passed"):
                db.candidates.update_one(
                    {"_id": c["_id"]},
                    {
                        "$set": {
                            "status": target_status,
                            "gate_results": payload["gate_results"],
                            "updated_at": utcnow(),
                        }
                    },
                )
                counts["passed"] += 1
            elif verdict.startswith("error_"):
                _drop(db, c, f"gate_{verdict}: {payload.get('err')}", gate_results=payload.get("gate_results"))
            else:
                _drop(
                    db,
                    c,
                    f"{verdict}: {payload.get('reason', '')}",
                    gate_results=payload.get("gate_results"),
                )
                counts[verdict] = counts.get(verdict, 0) + 1

            processed += 1
            if processed % PROGRESS_EVERY == 0 or processed == total:
                _set_stage(
                    db,
                    slate_run_id,
                    "gates",
                    processed=processed,
                    total=total,
                    started_at=started,
                    note=f"{phase} gates: {processed}/{total} ({counts['passed']} survived)",
                )

    return counts


def _drop(
    db: Database,
    candidate: dict[str, Any],
    reason: str,
    *,
    gate_results: dict[str, Any] | None = None,
) -> None:
    # Surface the drop inline so operators reviewing worker logs can see
    # which posts failed at each gate without grepping the candidates
    # collection. URL is truncated to keep lines readable.
    log.info(
        "│  [DROP] %s  ←  %s",
        (candidate.get("post_url") or "<no-url>")[:90],
        reason[:140],
    )
    update: dict[str, Any] = {
        "status": "gate_dropped",
        "drop_reason": reason,
        "updated_at": utcnow(),
    }
    if gate_results:
        update["gate_results"] = gate_results
    db.candidates.update_one({"_id": candidate["_id"]}, {"$set": update})


def _draft_one(
    db: Database,
    c: dict[str, Any],
    cofounder: dict[str, Any],
    *,
    exclude_formulas: list[str] | None = None,
) -> dict[str, Any] | None:
    """Draft + validate one candidate. Returns the persisted update dict on
    success, None on failure (and writes the gate_dropped state)."""
    voice = cofounder["voice_profile"]
    examples = voice.get("examples") or []
    tone = voice.get("tone_description") or ""
    icp_score = int(((c.get("gate_results") or {}).get("icp") or {}).get("total") or 0)
    comment_type = c.get("comment_type") or "A"

    formula = drafter.pick_reframe_formula(comment_type, exclude=exclude_formulas)
    try:
        comment_text, formula_used = drafter.draft_comment(
            cofounder_name=cofounder.get("display_name") or "",
            cofounder_tone=tone,
            cofounder_examples=examples,
            post_text=c.get("post_text") or "",
            author_name=c.get("author_name"),
            author_title=c.get("author_title"),
            author_company=c.get("author_company"),
            icp_score=icp_score,
            comment_type=comment_type,
            source_classification=c.get("source_classification") or "A",
            reframe_formula=formula,
        )
    except Exception as err:
        log.warning("drafter failed for candidate=%s: %s", c["_id"], err)
        _drop(db, c, f"drafter_error: {err}")
        return None

    result = validator.validate_comment(comment_text, comment_type)
    if not result.ok:
        _drop(db, c, f"validator: {result.reason}")
        return None

    db.candidates.update_one(
        {"_id": c["_id"]},
        {
            "$set": {
                "status": "drafted",
                "comment_text": comment_text,
                "reframe_formula": formula_used,
                "updated_at": utcnow(),
            }
        },
    )
    return {"comment_text": comment_text, "reframe_formula": formula_used}


def _rebalance_reframe_overrep(
    db: Database, *, slate_run_id: ObjectId, cofounders: list[dict[str, Any]]
) -> int:
    """If any single reframe formula > 40% of slate, redraft those candidates
    with a different formula. Caps at 1 retry per candidate."""
    by_id = {cf["_id"]: cf for cf in cofounders}
    drafted = list(db.candidates.find({"slate_run_id": slate_run_id, "status": "drafted"}))
    if not drafted:
        return 0
    counts: dict[str, int] = {}
    for c in drafted:
        f = c.get("reframe_formula") or ""
        counts[f] = counts.get(f, 0) + 1
    threshold = REFRAME_OVER_REPRESENTATION_THRESHOLD * len(drafted)
    over = [f for f, n in counts.items() if n > threshold and len(drafted) >= 3]
    if not over:
        return 0

    log.info(
        "rebalance: over-represented formulas=%s (counts=%s, slate=%d) — redrafting",
        over,
        counts,
        len(drafted),
    )
    redrafted = 0
    for c in drafted:
        if c.get("reframe_formula") not in over:
            continue
        cofounder = by_id.get(c["cofounder_id"])
        if not cofounder:
            continue
        # Reset status to allocated so _draft_one's gate_dropped path is consistent.
        db.candidates.update_one(
            {"_id": c["_id"]}, {"$set": {"status": "allocated"}}
        )
        if _draft_one(db, c, cofounder, exclude_formulas=over):
            redrafted += 1
    return redrafted


def _run_drafter(
    db: Database, *, slate_run_id: ObjectId, cofounders: list[dict[str, Any]]
) -> int:
    by_id = {cf["_id"]: cf for cf in cofounders}
    drafted = 0
    allocated = list(db.candidates.find({"slate_run_id": slate_run_id, "status": "allocated"}))
    for c in allocated:
        cofounder = by_id.get(c["cofounder_id"])
        if not cofounder:
            _drop(db, c, "drafter_no_cofounder")
            continue
        # Fallback: if the cofounder hasn't completed voice onboarding yet,
        # use the engine's default operator-tone profile so we still draft
        # a comment instead of silently dropping. The cofounder dict is
        # mutated in-place so _draft_one sees the populated voice profile,
        # and a one-time warning per slate flags that voice onboarding is
        # incomplete (operator can finish it without re-running discovery).
        if not cofounder.get("voice_profile"):
            cofounder["voice_profile"] = drafter.DEFAULT_VOICE_PROFILE
            log.warning(
                "drafter: cofounder=%s missing voice_profile — using DEFAULT_VOICE_PROFILE fallback. Complete voice onboarding to silence this.",
                cofounder.get("_id"),
            )
        if _draft_one(db, c, cofounder):
            drafted += 1
    # Slate-level rebalancer: kick over-represented formulas off the slate.
    redrafted = _rebalance_reframe_overrep(
        db, slate_run_id=slate_run_id, cofounders=cofounders
    )
    if redrafted:
        log.info("rebalance: %d candidates redrafted", redrafted)
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
