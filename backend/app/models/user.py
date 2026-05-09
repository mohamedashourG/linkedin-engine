from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.common import PyObjectId, utcnow


class UserSignup(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", max_length=64)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: str = Field(alias="_id")
    email: EmailStr
    name: str
    timezone: str
    onboarding_complete: bool = False
    paused: bool = False
    created_at: datetime


class UserInDB(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    email: EmailStr
    password_hash: str
    name: str
    timezone: str
    product_description: str | None = None
    product_extracted: dict[str, Any] | None = None
    icp_rubric: dict[str, Any] | None = None
    comment_quotas: dict[str, list[int]] = Field(
        default_factory=lambda: {
            "A": [35, 40],
            "B": [22, 25],
            "C": [14, 16],
            "D": [9, 12],
            "E": [7, 10],
            "F": [0, 5],
        }
    )
    daily_target: int = 30
    hard_floor: int = 20
    run_time_local: str = "09:00"
    calendly_webhook_signing_key: str | None = None
    onboarding_complete: bool = False
    paused: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


def user_to_public(user_doc: dict[str, Any]) -> UserPublic:
    return UserPublic(
        _id=str(user_doc["_id"]),
        email=user_doc["email"],
        name=user_doc["name"],
        timezone=user_doc.get("timezone", "UTC"),
        onboarding_complete=user_doc.get("onboarding_complete", False),
        paused=user_doc.get("paused", False),
        created_at=user_doc["created_at"],
    )
