"""
Onboarding wizard — 5 steps:
  1. /product   — extract + save ICP / keyword tiers
  2. /cofounders — add 1+ LinkedIn accounts to manage
  3. /cofounders/{id}/voice — build per-cofounder voice templates
  4. /calendly — connect Calendly URL (webhook registration is Phase 5)
  5. /schedule — set daily run time + target

Onboarding completes when all five flags pass; the dashboard layout reads
`onboarding_complete` and gates accordingly.
"""
from __future__ import annotations

import secrets
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth.deps import CurrentUser
from app.celery_app import register_operator_schedule
from app.database import get_db
from app.models.common import utcnow
from app.models.cofounder import (
    CofounderCreate,
    CofounderPublic,
    CofounderUpdate,
    VoicePayload,
    cofounder_to_public,
    new_cofounder_doc,
)
from app.models.onboarding import (
    CalendlyConnectRequest,
    OnboardingStatus,
    ProductExtractRequest,
    ProductExtractResponse,
    ProductSaveRequest,
    ScheduleRequest,
)
from app.services import icp_extractor, voice_profile
from app.services.openai_client import OpenAINotConfigured

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


# ---------------------------------------------------------------- helpers

def _object_id(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid id")
    return ObjectId(value)


async def _refresh_onboarding_complete(
    db: AsyncIOMotorDatabase, operator_id: ObjectId
) -> bool:
    user = await db.users.find_one({"_id": operator_id})
    cofounders = await db.cofounders.find(
        {"operator_id": operator_id, "active": True}
    ).to_list(length=None)
    has_product = bool(user and user.get("product_description"))
    cofounder_count = len(cofounders)
    cofounders_with_voice = sum(
        1 for cf in cofounders if cf.get("voice_profile")
    )
    has_calendly = bool(user and user.get("calendly_webhook_signing_key"))
    has_schedule = bool(user and user.get("run_time_local"))

    complete = bool(
        has_product
        and cofounder_count >= 1
        and cofounders_with_voice == cofounder_count
        and has_calendly
        and has_schedule
    )
    if complete != bool(user and user.get("onboarding_complete")):
        await db.users.update_one(
            {"_id": operator_id},
            {"$set": {"onboarding_complete": complete, "updated_at": utcnow()}},
        )
    return complete


# ---------------------------------------------------------------- status

@router.get("/status", response_model=OnboardingStatus)
async def status_endpoint(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> OnboardingStatus:
    operator_id = user["_id"]
    cofounders = await db.cofounders.find(
        {"operator_id": operator_id, "active": True}
    ).to_list(length=None)
    has_product = bool(user.get("product_description"))
    cofounder_count = len(cofounders)
    cofounders_with_voice = sum(1 for cf in cofounders if cf.get("voice_profile"))
    has_calendly = bool(user.get("calendly_webhook_signing_key"))
    has_schedule = bool(user.get("run_time_local")) and bool(user.get("daily_target"))
    complete = bool(
        has_product
        and cofounder_count >= 1
        and cofounders_with_voice == cofounder_count
        and has_calendly
        and has_schedule
    )
    return OnboardingStatus(
        has_product=has_product,
        cofounder_count=cofounder_count,
        cofounders_with_voice=cofounders_with_voice,
        has_calendly=has_calendly,
        has_schedule=has_schedule,
        onboarding_complete=complete,
    )


# ---------------------------------------------------------------- product

@router.post("/product/extract", response_model=ProductExtractResponse)
async def extract_product(
    payload: ProductExtractRequest,
    user: CurrentUser,
) -> ProductExtractResponse:
    try:
        result = await icp_extractor.extract(payload.free_text)
    except OpenAINotConfigured as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(err)
        )
    return ProductExtractResponse(**result)


@router.put("/product")
async def save_product(
    payload: ProductSaveRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "product_description": payload.product_description,
                "product_extracted": payload.product_extracted,
                "icp_rubric": payload.icp_rubric,
                "updated_at": utcnow(),
            }
        },
    )
    complete = await _refresh_onboarding_complete(db, user["_id"])
    return {"ok": True, "onboarding_complete": complete}


# ---------------------------------------------------------------- cofounders

@router.get("/cofounders", response_model=list[CofounderPublic])
async def list_cofounders(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> list[CofounderPublic]:
    docs = await db.cofounders.find(
        {"operator_id": user["_id"]}
    ).sort("created_at", 1).to_list(length=None)
    return [cofounder_to_public(d) for d in docs]


@router.post(
    "/cofounders",
    response_model=CofounderPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_cofounder(
    payload: CofounderCreate,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CofounderPublic:
    doc = new_cofounder_doc(user["_id"], payload)
    result = await db.cofounders.insert_one(doc)
    doc["_id"] = result.inserted_id
    await _refresh_onboarding_complete(db, user["_id"])
    return cofounder_to_public(doc)


@router.put("/cofounders/{cofounder_id}", response_model=CofounderPublic)
async def update_cofounder(
    cofounder_id: Annotated[str, Path()],
    payload: CofounderUpdate,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CofounderPublic:
    cf_id = _object_id(cofounder_id)
    updates: dict[str, Any] = {"updated_at": utcnow()}
    raw = payload.model_dump(exclude_unset=True)
    for key, value in raw.items():
        if key in ("linkedin_url", "calendly_url") and value is not None:
            updates[key] = str(value)
        else:
            updates[key] = value

    result = await db.cofounders.find_one_and_update(
        {"_id": cf_id, "operator_id": user["_id"]},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cofounder not found")
    await _refresh_onboarding_complete(db, user["_id"])
    return cofounder_to_public(result)


@router.delete("/cofounders/{cofounder_id}")
async def delete_cofounder(
    cofounder_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    cf_id = _object_id(cofounder_id)
    result = await db.cofounders.delete_one(
        {"_id": cf_id, "operator_id": user["_id"]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cofounder not found")
    complete = await _refresh_onboarding_complete(db, user["_id"])
    return {"ok": True, "onboarding_complete": complete}


# ---------------------------------------------------------------- voice

@router.put("/cofounders/{cofounder_id}/voice", response_model=CofounderPublic)
async def save_voice(
    cofounder_id: Annotated[str, Path()],
    payload: VoicePayload,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CofounderPublic:
    cf_id = _object_id(cofounder_id)
    cofounder = await db.cofounders.find_one(
        {"_id": cf_id, "operator_id": user["_id"]}
    )
    if not cofounder:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cofounder not found")

    try:
        templates = await voice_profile.build_templates(
            tone_description=payload.tone_description,
            examples=[ex for ex in payload.examples],
        )
    except OpenAINotConfigured as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(err)
        )

    voice_doc = {
        "tone_description": payload.tone_description,
        "examples": [ex.model_dump() for ex in payload.examples],
        "source_a_template": templates["source_a_template"],
        "source_b_template": templates["source_b_template"],
    }
    result = await db.cofounders.find_one_and_update(
        {"_id": cf_id, "operator_id": user["_id"]},
        {"$set": {"voice_profile": voice_doc, "updated_at": utcnow()}},
        return_document=True,
    )
    await _refresh_onboarding_complete(db, user["_id"])
    return cofounder_to_public(result)


# ---------------------------------------------------------------- calendly

@router.post("/calendly")
async def connect_calendly(
    payload: CalendlyConnectRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    """
    Phase 2: store the Calendly URL + a generated signing key. The actual webhook
    registration happens in Phase 5 (services/calendly.py + Calendly v2 API).
    """
    signing_key = secrets.token_urlsafe(32)
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "calendly_url": str(payload.calendly_url),
                "calendly_webhook_signing_key": signing_key,
                "updated_at": utcnow(),
            }
        },
    )
    complete = await _refresh_onboarding_complete(db, user["_id"])
    return {"ok": True, "onboarding_complete": complete}


# ---------------------------------------------------------------- schedule

@router.put("/schedule")
async def set_schedule(
    payload: ScheduleRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "run_time_local": payload.run_time_local,
                "daily_target": payload.daily_target,
                "updated_at": utcnow(),
            }
        },
    )
    register_operator_schedule(str(user["_id"]))
    complete = await _refresh_onboarding_complete(db, user["_id"])
    return {"ok": True, "onboarding_complete": complete}
