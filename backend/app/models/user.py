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
    client_slug: str = "glnk"
    product_description: str | None = None
    product_extracted: dict[str, Any] | None = None
    icp_rubric: dict[str, Any] | None = None
    # RULE 7 audit (2026-05-05): A capped 25 (was workhorse, now reduced),
    # B capped 10 (dead, 3% reply), C floor 25 (workhorse), D 10-15, E floor
    # 20 (best performer, 31% reply), F 5-10. Old [lo, hi] shape still
    # accepted by allocator._normalize_quota; new shape is {floor, cap}.
    comment_quotas: dict[str, Any] = Field(
        default_factory=lambda: {
            "A": {"floor": 0,  "cap": 25},
            "B": {"floor": 0,  "cap": 10},
            "C": {"floor": 25, "cap": 100},
            "D": {"floor": 10, "cap": 15},
            "E": {"floor": 20, "cap": 100},
            "F": {"floor": 5,  "cap": 10},
        }
    )
    # RULE 2 (locked): aspirational target 50, hard floor 30 (warn below),
    # abort floor 25 (engine refuses to ship). Score ≤5 candidates are
    # dropped at icp_low gate so they automatically don't count toward
    # these.
    daily_target: int = 50
    hard_floor: int = 30
    abort_floor: int = 25
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
