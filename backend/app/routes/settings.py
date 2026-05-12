"""
Editable operator-level settings: keyword tiers, ICP rubric, comment quotas,
plus daily target / hard floor / run time.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr, Field

from app.auth.deps import CurrentUser
from app.database import get_db
from app.models.common import utcnow

router = APIRouter(prefix="/api/settings", tags=["settings"])


class KeywordTiers(BaseModel):
    tier_1: list[str] = Field(default_factory=list)
    tier_2: list[str] = Field(default_factory=list)
    tier_3: list[str] = Field(default_factory=list)


class IcpRubricTier(BaseModel):
    matches: list[str] = Field(default_factory=list)
    score: int = 0


class IcpRubricAxis(BaseModel):
    tiers: list[IcpRubricTier] = Field(default_factory=list)


class IcpRubric(BaseModel):
    title: IcpRubricAxis
    industry: IcpRubricAxis
    geography: IcpRubricAxis
    stage: IcpRubricAxis
    threshold: int = 6


class CommentQuotas(BaseModel):
    A: list[float] = Field(min_length=2, max_length=2)
    B: list[float] = Field(min_length=2, max_length=2)
    C: list[float] = Field(min_length=2, max_length=2)
    D: list[float] = Field(min_length=2, max_length=2)
    E: list[float] = Field(min_length=2, max_length=2)
    F: list[float] = Field(min_length=2, max_length=2)


class ProductExtractedPublic(BaseModel):
    target_industries: list[str] = Field(default_factory=list)
    target_titles: list[str] = Field(default_factory=list)
    target_geographies: list[str] = Field(default_factory=list)
    target_pain_points: list[str] = Field(default_factory=list)


class SettingsResponse(BaseModel):
    keywords: KeywordTiers
    icp_rubric: IcpRubric | None = None
    product_extracted: ProductExtractedPublic = Field(
        default_factory=ProductExtractedPublic
    )
    comment_quotas: dict[str, list[float]]
    daily_target: int
    hard_floor: int
    run_time_local: str
    paused: bool
    operator_email: EmailStr
    slate_recipients: list[EmailStr] = Field(default_factory=list)


class SettingsPatch(BaseModel):
    keywords: KeywordTiers | None = None
    icp_rubric: IcpRubric | None = None
    product_extracted: ProductExtractedPublic | None = None
    comment_quotas: CommentQuotas | None = None
    daily_target: int | None = Field(default=None, ge=1, le=200)
    hard_floor: int | None = Field(default=None, ge=1, le=200)
    run_time_local: str | None = Field(
        default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    paused: bool | None = None
    slate_recipients: list[EmailStr] | None = Field(default=None, max_length=20)


def _to_response(user: dict[str, Any]) -> SettingsResponse:
    extracted = user.get("product_extracted") or {}
    keywords_raw = extracted.get("suggested_keywords") or {}
    return SettingsResponse(
        keywords=KeywordTiers(
            tier_1=keywords_raw.get("tier_1") or [],
            tier_2=keywords_raw.get("tier_2") or [],
            tier_3=keywords_raw.get("tier_3") or [],
        ),
        icp_rubric=IcpRubric(**user["icp_rubric"]) if user.get("icp_rubric") else None,
        product_extracted=ProductExtractedPublic(
            target_industries=extracted.get("target_industries") or [],
            target_titles=extracted.get("target_titles") or [],
            target_geographies=extracted.get("target_geographies") or [],
            target_pain_points=extracted.get("target_pain_points") or [],
        ),
        comment_quotas=user.get("comment_quotas") or {},
        daily_target=int(user.get("daily_target") or 30),
        hard_floor=int(user.get("hard_floor") or 20),
        run_time_local=user.get("run_time_local") or "09:00",
        paused=bool(user.get("paused", False)),
        operator_email=user["email"],
        slate_recipients=user.get("slate_recipients") or [],
    )


@router.get("/", response_model=SettingsResponse)
async def get_settings(user: CurrentUser) -> SettingsResponse:
    return _to_response(user)


@router.put("/", response_model=SettingsResponse)
async def update_settings(
    payload: SettingsPatch,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SettingsResponse:
    update: dict[str, Any] = {"updated_at": utcnow()}

    # product_extracted is a nested doc with multiple sub-fields. Merge the
    # patch fields into the existing doc so a partial update (e.g. keywords
    # only, or target_titles only) doesn't blow away the others.
    if payload.keywords is not None or payload.product_extracted is not None:
        merged_extracted = (user.get("product_extracted") or {}).copy()
        if payload.keywords is not None:
            merged_extracted["suggested_keywords"] = payload.keywords.model_dump()
        if payload.product_extracted is not None:
            pe = payload.product_extracted.model_dump()
            for k, v in pe.items():
                merged_extracted[k] = v
        update["product_extracted"] = merged_extracted

    if payload.icp_rubric is not None:
        update["icp_rubric"] = payload.icp_rubric.model_dump()

    if payload.comment_quotas is not None:
        update["comment_quotas"] = payload.comment_quotas.model_dump()

    if payload.daily_target is not None:
        update["daily_target"] = payload.daily_target
    if payload.hard_floor is not None:
        update["hard_floor"] = payload.hard_floor
    if payload.run_time_local is not None:
        update["run_time_local"] = payload.run_time_local
    if payload.paused is not None:
        update["paused"] = payload.paused
    if payload.slate_recipients is not None:
        # Dedupe + lowercase normalize.
        seen: set[str] = set()
        clean: list[str] = []
        for raw in payload.slate_recipients:
            e = str(raw).strip().lower()
            if e and e not in seen:
                seen.add(e)
                clean.append(e)
        update["slate_recipients"] = clean

    if len(update) == 1:
        # only updated_at changed → no-op
        return _to_response(user)

    result = await db.users.find_one_and_update(
        {"_id": user["_id"]},
        {"$set": update},
        return_document=True,
    )
    if not result:
        raise HTTPException(404, "User not found")
    return _to_response(result)
