"""
Crustdata watcher management — register, list, simulate, delete watches per
cofounder. The simulation endpoint is the primary dev-loop entry point: it
fires the same notification payload as a real watch but instantly, so we can
integration-test the whole pipe (Crustdata → webhook → inbox → discovery)
without waiting an hour for a real watch tick.

The route also stores the returned `watch_id` on the cofounder doc as
`crustdata_watch_ids` so subsequent settings changes can update/delete them.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.config import settings
from app.database import get_db
from app.models.common import utcnow
from app.services.crustdata import (
    CrustdataError,
    CrustdataNotConfigured,
    CrustdataQuotaExhausted,
    CrustdataWatchSpec,
    build_keyword_expression,
    delete_watch as cd_delete_watch,
    find_reconciled_production_watch,
    list_watches as cd_list_watches,
    redact_notification_url,
    register_keyword_watch,
    webhook_token_for,
    webhook_url_for,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crustdata", tags=["crustdata"])


class RegisterWatchRequest(BaseModel):
    """Optional overrides — when omitted we derive everything from the operator
    config + cofounder.

    **Industries:** ICP rubric industry strings are *not* sent by default (Crustdata
    rejects most free-form labels). Pass ``industries`` with Crustdata-valid
    values, or ``[]`` to omit explicitly.
    """

    keyword_expression: str | None = None
    actor_types: list[str] | None = None
    author_titles: list[str] | None = None
    author_company_urls: list[str] | None = None
    author_location: str | None = None
    industries: list[str] | None = None
    post_intent: str | None = None
    post_categories: list[str] | None = None
    fetch_reactors: bool = False
    detailed_reactor_data: bool = False
    headcount_buckets: list[str] | None = None
    company_hq_country: str | None = None
    past_company: list[str] | None = None
    past_title: list[str] | None = None
    expiration_days: int | None = None
    simulation: bool = Field(
        default=False,
        description="If true, fires Crustdata simulation endpoint — instant payload.",
    )
    notification_endpoint_override: str | None = Field(
        default=None,
        description="Override the auto-derived webhook URL (use for ngrok in dev).",
    )


def _spec_from_operator(
    operator: dict[str, Any], override: RegisterWatchRequest
) -> CrustdataWatchSpec:
    """Derive the Crustdata filter spec from the operator's ICP rubric.

    Caller can override any field via RegisterWatchRequest; everything that
    remains None falls back to the operator config.
    """
    extracted = operator.get("product_extracted") or {}
    keywords = (extracted.get("suggested_keywords") or {}) if extracted else {}
    rubric = operator.get("icp_rubric") or {}

    def _matches(axis: str) -> list[str]:
        out: list[str] = []
        axis_dict = rubric.get(axis) or {}
        for tier in axis_dict.get("tiers") or []:
            out.extend(tier.get("matches") or [])
        return out

    tier_1_titles = []
    title_axis = (rubric.get("title") or {}).get("tiers") or []
    if title_axis:
        tier_1_titles = (title_axis[0] or {}).get("matches") or []

    geos_full = _matches("geography")
    geo_first = override.author_location or (geos_full[0] if geos_full else None)

    if override.industries is not None:
        industries_val = override.industries if override.industries else None
    else:
        # Crustdata INDUSTRY filter expects their taxonomy; ICP rubric strings are usually 400.
        industries_val = None

    keyword_expr = override.keyword_expression or build_keyword_expression(
        keywords.get("tier_1") or [],
        keywords.get("tier_2") or [],
        max_boolean_operators=settings.crustdata_watch_max_boolean_operators,
    )
    if not keyword_expr:
        raise HTTPException(
            400,
            "no keywords available — operator has no tier_1/tier_2 keywords and "
            "no keyword_expression override was provided",
        )

    product_desc = (
        operator.get("product_description")
        or (extracted.get("description") if isinstance(extracted, dict) else None)
        or ""
    )[:600] or None

    past_company = override.past_company
    past_title = override.past_title
    company_hq_country = override.company_hq_country
    headcount_buckets = (
        list(override.headcount_buckets) if override.headcount_buckets else None
    )
    has_lead = bool(past_company or past_title)
    has_account = bool(headcount_buckets or company_hq_country)
    if not has_lead and not has_account:
        headcount_buckets = list(settings.crustdata_watch_default_headcount_buckets)
        log.info(
            "crustdata watcher: applying default COMPANY_HEADCOUNT (event requires "
            "account or lead filters)"
        )

    return CrustdataWatchSpec(
        keyword_expression=keyword_expr,
        actor_types=override.actor_types or ["person"],
        author_titles=override.author_titles or (tier_1_titles or None),
        author_company_urls=override.author_company_urls or None,
        author_location=geo_first,
        industries=industries_val,
        post_intent=override.post_intent or product_desc,
        post_categories=override.post_categories,
        fetch_reactors=override.fetch_reactors,
        detailed_reactor_data=override.detailed_reactor_data,
        headcount_buckets=headcount_buckets,
        company_hq_country=company_hq_country,
        past_company=past_company,
        past_title=past_title,
    )


async def _load_cofounder(
    db: AsyncIOMotorDatabase, *, operator_id: ObjectId, cofounder_id: str
) -> dict[str, Any]:
    if not ObjectId.is_valid(cofounder_id):
        raise HTTPException(400, "invalid cofounder_id")
    cf = await db.cofounders.find_one(
        {"_id": ObjectId(cofounder_id), "operator_id": operator_id}
    )
    if not cf:
        raise HTTPException(404, "cofounder not found")
    return cf


def _coerce_crustdata(err: CrustdataError) -> HTTPException:
    if isinstance(err, CrustdataNotConfigured):
        return HTTPException(503, str(err))
    if isinstance(err, CrustdataQuotaExhausted):
        return HTTPException(402, str(err))
    return HTTPException(502, f"crustdata: {err}")


@router.post("/cofounders/{cofounder_id}/register")
async def register(
    cofounder_id: str,
    body: RegisterWatchRequest,
    current_user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, Any]:
    """Register (or simulate) a `linkedin-post-with-keyword` watch for one
    cofounder. The webhook URL is derived per-cofounder with an HMAC token so
    the receiver can verify origin."""
    operator_id: ObjectId = current_user["_id"]
    log.info(
        "crustdata.route.register start cofounder=%s simulation=%s operator=%s",
        cofounder_id,
        body.simulation,
        operator_id,
    )
    cf = await _load_cofounder(db, operator_id=operator_id, cofounder_id=cofounder_id)
    spec = _spec_from_operator(current_user, body)

    expiration = None
    if body.expiration_days is not None:
        expiration = date.today() + timedelta(days=body.expiration_days)

    notification_endpoint = body.notification_endpoint_override
    if notification_endpoint is None:
        try:
            notification_endpoint = webhook_url_for(cofounder_id)
        except CrustdataNotConfigured as err:
            if not body.simulation:
                raise HTTPException(503, str(err))
            # Simulation still needs a URL Crustdata can POST to — use the same
            # public base as production (`CRUSTDATA_WEBHOOK_BASE_URL` / .env).
            base = (settings.crustdata_webhook_base_url or "").rstrip("/")
            if not base:
                raise HTTPException(
                    503,
                    "Simulation requires CRUSTDATA_WEBHOOK_BASE_URL or "
                    "notification_endpoint_override. "
                    f"({err})",
                ) from err
            try:
                tok = webhook_token_for(cofounder_id)
            except CrustdataNotConfigured as e2:
                raise HTTPException(503, str(e2)) from e2
            notification_endpoint = (
                f"{base}/api/webhooks/crustdata"
                f"?{urlencode({'cofounder_id': cofounder_id, 'token': tok})}"
            )

    log.info(
        "crustdata.route.register notification_endpoint=%s (simulation=%s)",
        redact_notification_url(notification_endpoint or ""),
        body.simulation,
    )

    try:
        if not body.simulation:
            reconciled = find_reconciled_production_watch(cofounder_id)
            if reconciled:
                watch_id = reconciled["watch_id"]
                kw_stored = reconciled.get("keyword_expression") or spec.keyword_expression
                log.info(
                    "crustdata.route.register result=reconciled cofounder=%s watch_id=%s "
                    "(matched via GET /watcher/watches; skipped POST /watcher/watches)",
                    cofounder_id,
                    watch_id,
                )
                await db.cofounders.update_one(
                    {"_id": cf["_id"]},
                    {
                        "$addToSet": {"crustdata_watch_ids": str(watch_id)},
                        "$set": {
                            "crustdata_last_registered_at": utcnow(),
                            "crustdata_keyword_expression": kw_stored,
                        },
                    },
                )
                return {
                    "ok": True,
                    "reconciled": True,
                    "watch_id": watch_id,
                    "simulation": False,
                    "notification_endpoint": notification_endpoint,
                    "spec": {
                        "keyword_expression": spec.keyword_expression,
                        "actor_types": spec.actor_types,
                        "author_titles": spec.author_titles,
                        "industries": spec.industries,
                        "author_location": spec.author_location,
                        "post_intent_set": bool(spec.post_intent),
                    },
                    "raw": reconciled,
                }
        log.info(
            "crustdata.route.register calling Crustdata API register_keyword_watch "
            "cofounder=%s simulation=%s",
            cofounder_id,
            body.simulation,
        )
        resp = register_keyword_watch(
            cofounder_id=cofounder_id,
            spec=spec,
            notification_endpoint=notification_endpoint,
            expiration_date=expiration,
            simulation=body.simulation,
        )
    except CrustdataError as err:
        raise _coerce_crustdata(err)

    watch_id = resp.get("id") or resp.get("watch_id") or resp.get("uuid")
    log.info(
        "crustdata.route.register done cofounder=%s simulation=%s watch_id=%s",
        cofounder_id,
        body.simulation,
        watch_id,
    )
    if watch_id and not body.simulation:
        await db.cofounders.update_one(
            {"_id": cf["_id"]},
            {
                "$addToSet": {"crustdata_watch_ids": str(watch_id)},
                "$set": {
                    "crustdata_last_registered_at": utcnow(),
                    "crustdata_keyword_expression": spec.keyword_expression,
                },
            },
        )
    return {
        "ok": True,
        "watch_id": watch_id,
        "simulation": body.simulation,
        "notification_endpoint": notification_endpoint,
        "spec": {
            "keyword_expression": spec.keyword_expression,
            "actor_types": spec.actor_types,
            "author_titles": spec.author_titles,
            "industries": spec.industries,
            "author_location": spec.author_location,
            "post_intent_set": bool(spec.post_intent),
        },
        "raw": resp,
    }


@router.get("/watches")
async def list_all(current_user: CurrentUser) -> dict[str, Any]:
    """List every watch registered against the configured Crustdata account.
    Crustdata's list endpoint is account-wide, not per-cofounder."""
    _ = current_user  # require auth, no operator filter (account-wide)
    log.info("crustdata.route.list_watches GET /api/crustdata/watches (proxies Crustdata GET /watcher/watches)")
    try:
        watches = cd_list_watches()
    except CrustdataError as err:
        raise _coerce_crustdata(err)
    return {"count": len(watches), "watches": watches}


@router.delete("/watches/{watch_id}")
async def delete_one(
    watch_id: str,
    current_user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, Any]:
    operator_id: ObjectId = current_user["_id"]
    log.info(
        "crustdata.route.delete_watch watch_id=%s operator=%s (Crustdata DELETE /watcher/watches/{id})",
        watch_id,
        operator_id,
    )
    try:
        deleted = cd_delete_watch(watch_id)
    except CrustdataError as err:
        raise _coerce_crustdata(err)
    # Pull this watch_id off any cofounder that recorded it
    await db.cofounders.update_many(
        {"operator_id": operator_id},
        {"$pull": {"crustdata_watch_ids": watch_id}},
    )
    return {"ok": True, "deleted": deleted, "watch_id": watch_id}


@router.get("/inbox")
async def list_inbox(
    current_user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    consumed: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Inspect the per-operator Crustdata inbox. Useful to verify webhook
    delivery + simulation behaviour."""
    operator_id: ObjectId = current_user["_id"]
    cursor = (
        db.crustdata_inbox.find(
            {"operator_id": operator_id, "consumed": consumed}
        )
        .sort("received_at", -1)
        .limit(max(1, min(limit, 200)))
    )
    rows: list[dict[str, Any]] = []
    async for r in cursor:
        rows.append(
            {
                "id": str(r["_id"]),
                "cofounder_id": r.get("cofounder_id"),
                "post_url": r.get("post_url"),
                "author_name": r.get("author_name"),
                "author_title": r.get("author_title"),
                "author_company": r.get("author_company"),
                "author_location": r.get("author_location"),
                "date_posted": r.get("date_posted"),
                "received_at": r.get("received_at"),
                "consumed": r.get("consumed"),
                "consumed_reason": r.get("consumed_reason"),
                "post_text_preview": (r.get("post_text") or "")[:200],
            }
        )
    total = await db.crustdata_inbox.count_documents(
        {"operator_id": operator_id, "consumed": consumed}
    )
    return {"count": len(rows), "total": total, "rows": rows}


@router.post("/inbox/synthesize")
async def synthesize_inbox(
    current_user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    cofounder_id: str,
    count: int = 3,
) -> dict[str, Any]:
    """Dev-only: drop N synthetic inbox rows directly into crustdata_inbox so
    you can exercise the discovery drain path without depending on the
    Crustdata service. Bypasses the webhook + HMAC check."""
    if settings.app_env != "dev":
        raise HTTPException(403, "synthesize is dev-only")
    operator_id: ObjectId = current_user["_id"]
    cf = await _load_cofounder(db, operator_id=operator_id, cofounder_id=cofounder_id)
    now = utcnow()
    docs: list[dict[str, Any]] = []
    for i in range(max(1, min(count, 20))):
        docs.append(
            {
                "operator_id": operator_id,
                "cofounder_id": str(cf["_id"]),
                "post_uid": f"synthetic_{now.isoformat()}_{i}",
                "post_url": f"https://www.linkedin.com/posts/synthetic-{now.timestamp():.0f}-{i}",
                "share_urn": None,
                "actor_type": "person",
                "actor_name": f"Synthetic Author {i + 1}",
                "author_name": f"Synthetic Author {i + 1}",
                "author_title": "VP of Commercial",
                "author_company": "Acme Biotech",
                "author_company_linkedin_id": None,
                "author_linkedin_url": f"https://www.linkedin.com/in/synthetic-{i + 1}",
                "author_location": "Boston, Massachusetts, United States",
                "post_text": (
                    "Just kicked off a Phase 2 trial readout review. "
                    "We saw a 28% absolute response rate in the treatment "
                    "arm, vs 11% in control. The biggest surprise wasn't "
                    "efficacy — it was that the patient-reported outcomes "
                    "tracked the radiographic data within 4 weeks. That's "
                    f"the cleanest signal I've seen in years. (test #{i + 1})"
                ),
                "date_posted": now.date().isoformat(),
                "total_reactions": 47 + i * 3,
                "total_comments": 12 + i,
                "consumed": False,
                "consumed_at": None,
                "received_at": now,
                "created_at": now,
                "updated_at": now,
                "synthetic": True,
            }
        )
    result = await db.crustdata_inbox.insert_many(docs)
    return {"ok": True, "inserted": len(result.inserted_ids)}
