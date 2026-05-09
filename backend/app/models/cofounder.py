from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl

from app.models.common import utcnow


class VoiceExamplePayload(BaseModel):
    post: str = Field(min_length=1, max_length=5000)
    comment: str = Field(min_length=1, max_length=2000)


class CofounderCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    linkedin_url: HttpUrl
    calendly_url: HttpUrl | None = None
    email: EmailStr
    daily_volume_target: int = Field(default=20, ge=1, le=200)
    # RULE 13 / RULE 1: lower number = higher authority. Highest-ICP candidates
    # are routed to the lowest-rank cofounder first. Default 100 keeps new
    # cofounders out of the top spots until the operator promotes them.
    authority_rank: int = Field(default=100, ge=1, le=999)


class CofounderUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    linkedin_url: HttpUrl | None = None
    calendly_url: HttpUrl | None = None
    email: EmailStr | None = None
    daily_volume_target: int | None = Field(default=None, ge=1, le=200)
    authority_rank: int | None = Field(default=None, ge=1, le=999)
    active: bool | None = None
    unipile_account_id: str | None = Field(default=None, max_length=200)
    connect_message_template: str | None = Field(default=None, max_length=300)


class VoicePayload(BaseModel):
    tone_description: str = Field(min_length=10, max_length=2000)
    examples: list[VoiceExamplePayload] = Field(min_length=3, max_length=8)


class CofounderPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    display_name: str
    linkedin_url: str
    calendly_url: str | None = None
    email: EmailStr
    daily_volume_target: int
    authority_rank: int = 100
    voice_profile: dict[str, Any] | None = None
    unipile_account_id: str | None = None
    connect_message_template: str | None = None
    active: bool
    created_at: datetime


def cofounder_to_public(doc: dict[str, Any]) -> CofounderPublic:
    return CofounderPublic(
        _id=str(doc["_id"]),
        display_name=doc["display_name"],
        linkedin_url=doc["linkedin_url"],
        calendly_url=doc.get("calendly_url"),
        email=doc["email"],
        daily_volume_target=doc["daily_volume_target"],
        authority_rank=int(doc.get("authority_rank") or 100),
        voice_profile=doc.get("voice_profile"),
        unipile_account_id=doc.get("unipile_account_id"),
        connect_message_template=doc.get("connect_message_template"),
        active=doc.get("active", True),
        created_at=doc["created_at"],
    )


def new_cofounder_doc(operator_id, payload: CofounderCreate) -> dict[str, Any]:
    now = utcnow()
    return {
        "operator_id": operator_id,
        "display_name": payload.display_name,
        "linkedin_url": str(payload.linkedin_url),
        "calendly_url": str(payload.calendly_url) if payload.calendly_url else None,
        "email": payload.email,
        "daily_volume_target": payload.daily_volume_target,
        "authority_rank": payload.authority_rank,
        "voice_profile": None,
        "unipile_account_id": None,
        "connect_message_template": None,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }
