"""
Derive per-step pass/fail post lists for the /today dashboard from candidate
documents (status + drop_reason). Keeps logic in one place for the API response.
"""
from __future__ import annotations

from typing import Any

_PIPELINE_POST_LIMIT = 250


def _preview(post_text: str | None, limit: int = 140) -> str:
    t = (post_text or "").strip().replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def pipeline_post_ref(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(c["_id"]),
        "post_url": c.get("post_url") or "",
        "author_name": c.get("author_name"),
        "post_preview": _preview(c.get("post_text")),
        "status": c.get("status") or "",
        "drop_reason": c.get("drop_reason"),
    }


def _is_drafter_failure(dr: str | None) -> bool:
    if not dr:
        return False
    return (
        dr.startswith("drafter_error")
        or dr.startswith("validator:")
        or dr.startswith("drafter_no_cofounder")
    )


def _is_gate_failure(dr: str | None) -> bool:
    """gate_dropped rows that are not drafter-stage failures."""
    if not dr:
        return False
    if _is_drafter_failure(dr):
        return False
    return True


def _cheap_gate_reason(dr: str | None) -> bool:
    if not dr:
        return False
    return (
        dr.startswith("non_buyer")
        or dr.startswith("post_quality")
        or dr.startswith("error_non_buyer")
        or dr.startswith("error_quality")
    )


def _finalize_lists(
    passed: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> dict[str, Any]:
    def trunc(xs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, bool]:
        total = len(xs)
        if total <= _PIPELINE_POST_LIMIT:
            return xs, total, False
        return xs[:_PIPELINE_POST_LIMIT], total, True

    p, pt, pc = trunc(passed)
    f, ft, fc = trunc(failed)
    pend, pendt, pendc = trunc(pending)
    return {
        "passed": p,
        "failed": f,
        "pending": pend,
        "passed_total": pt,
        "failed_total": ft,
        "pending_total": pendt,
        "truncated": pc or fc or pendc,
    }


def compute_pipeline_breakdown(
    candidates: list[dict[str, Any]],
    *,
    slate_status: str,
    email_sent: bool,
) -> dict[str, Any]:
    """Return keyed breakdown matching the dashboard pipeline steps."""
    building = slate_status == "building"

    discovery_pass = list(candidates)

    # Inline rubric (runs INSIDE discovery — geo + title/industry + post-keyword
    # tier checks via Path A / Path B). Drops here happen before the candidate
    # ever reaches verification, so they get their own step.
    inline_pass: list[dict[str, Any]] = []
    inline_fail: list[dict[str, Any]] = []
    for c in candidates:
        st = c.get("status") or ""
        if st == "rejected_inline":
            inline_fail.append(c)
        else:
            inline_pass.append(c)

    ver_pass: list[dict[str, Any]] = []
    ver_fail: list[dict[str, Any]] = []
    ver_pending: list[dict[str, Any]] = []
    for c in candidates:
        st = c.get("status") or ""
        if st == "rejected_url_mismatch":
            ver_fail.append(c)
        elif st == "raw" and building:
            ver_pending.append(c)
        elif st == "rejected_inline":
            # Stopped before verification ran — don't credit as ver_pass.
            continue
        else:
            ver_pass.append(c)

    gate_pass: list[dict[str, Any]] = []
    gate_fail: list[dict[str, Any]] = []
    gate_pending: list[dict[str, Any]] = []
    for c in candidates:
        st = c.get("status") or ""
        dr = c.get("drop_reason")
        if st in ("gate_passed", "allocated", "drafted", "slated", "shipped", "dropped_by_user"):
            gate_pass.append(c)
        elif st == "gate_dropped" and dr and _is_gate_failure(dr):
            gate_fail.append(c)
        elif building and st in ("verified", "cheap_gate_passed"):
            # In streaming mode (and any in-flight legacy run) a candidate
            # sitting at verified or cheap_gate_passed is mid-flight through
            # the gates pipeline — show it as waiting, not failed.
            gate_pending.append(c)

    alloc_pass: list[dict[str, Any]] = []
    alloc_fail: list[dict[str, Any]] = []
    alloc_pending: list[dict[str, Any]] = []
    for c in candidates:
        st = c.get("status") or ""
        if st in ("allocated", "drafted", "slated", "shipped", "dropped_by_user"):
            alloc_pass.append(c)
        elif st == "gate_passed":
            # While the run is still building, a gate_passed candidate is
            # buffered waiting for the next allocator wave (streaming) or
            # for the allocator to run at all (legacy) — that's a "wait",
            # not a failure. Only after the slate seals is a leftover
            # gate_passed truly not-selected.
            if building:
                alloc_pending.append(c)
            else:
                alloc_fail.append(c)

    draft_pass: list[dict[str, Any]] = []
    draft_fail: list[dict[str, Any]] = []
    draft_pending: list[dict[str, Any]] = []
    for c in candidates:
        st = c.get("status") or ""
        dr = c.get("drop_reason")
        if st in ("drafted", "slated", "shipped", "dropped_by_user"):
            draft_pass.append(c)
        elif st == "gate_dropped" and dr and _is_drafter_failure(dr):
            draft_fail.append(c)
        elif building and st == "allocated":
            draft_pending.append(c)

    r23_pass: list[dict[str, Any]] = []
    r23_fail: list[dict[str, Any]] = []
    for c in candidates:
        st = c.get("status") or ""
        if st in ("slated", "shipped", "dropped_by_user"):
            r23_pass.append(c)
        elif st == "drafted" and slate_status == "force_aborted":
            r23_fail.append(c)

    email_pass: list[dict[str, Any]] = []
    email_fail: list[dict[str, Any]] = []
    if email_sent:
        for c in candidates:
            st = c.get("status") or ""
            if st in ("slated", "shipped", "dropped_by_user"):
                email_pass.append(c)
    elif slate_status == "sealed":
        for c in candidates:
            st = c.get("status") or ""
            if st in ("slated", "shipped", "dropped_by_user"):
                email_fail.append(c)

    out: dict[str, Any] = {
        "discovery": _finalize_lists(
            [pipeline_post_ref(c) for c in discovery_pass],
            [],
            [],
        ),
        "inline_rubric": _finalize_lists(
            [pipeline_post_ref(c) for c in inline_pass],
            [pipeline_post_ref(c) for c in inline_fail],
            [],
        ),
        "verification": _finalize_lists(
            [pipeline_post_ref(c) for c in ver_pass],
            [pipeline_post_ref(c) for c in ver_fail],
            [pipeline_post_ref(c) for c in ver_pending],
        ),
        "gates": _finalize_lists(
            [pipeline_post_ref(c) for c in gate_pass],
            [pipeline_post_ref(c) for c in gate_fail],
            [pipeline_post_ref(c) for c in gate_pending],
        ),
        "allocator": _finalize_lists(
            [pipeline_post_ref(c) for c in alloc_pass],
            [pipeline_post_ref(c) for c in alloc_fail],
            [pipeline_post_ref(c) for c in alloc_pending],
        ),
        "drafter": _finalize_lists(
            [pipeline_post_ref(c) for c in draft_pass],
            [pipeline_post_ref(c) for c in draft_fail],
            [pipeline_post_ref(c) for c in draft_pending],
        ),
        "rule_23": _finalize_lists(
            [pipeline_post_ref(c) for c in r23_pass],
            [pipeline_post_ref(c) for c in r23_fail],
            [],
        ),
        "email_delivery": _finalize_lists(
            [pipeline_post_ref(c) for c in email_pass],
            [pipeline_post_ref(c) for c in email_fail],
            [],
        ),
    }
    return out
