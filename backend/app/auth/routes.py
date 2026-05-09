from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.auth.deps import ACCESS_COOKIE, CurrentUser
from app.auth.jwt import create_access_token, hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.models.common import utcnow
from app.models.user import UserLogin, UserPublic, UserSignup, user_to_public

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        max_age=settings.jwt_expires_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


@router.post("/signup", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: UserSignup,
    response: Response,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> UserPublic:
    now = utcnow()
    user_doc = {
        "email": payload.email.lower(),
        "password_hash": hash_password(payload.password),
        "name": payload.name,
        "timezone": payload.timezone,
        "product_description": None,
        "product_extracted": None,
        "icp_rubric": None,
        "comment_quotas": {
            "A": [35, 40],
            "B": [22, 25],
            "C": [14, 16],
            "D": [9, 12],
            "E": [7, 10],
            "F": [0, 5],
        },
        "daily_target": 30,
        "hard_floor": 20,
        "run_time_local": "09:00",
        "calendly_webhook_signing_key": None,
        "onboarding_complete": False,
        "paused": False,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await db.users.insert_one(user_doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    user_doc["_id"] = result.inserted_id
    token = create_access_token(str(result.inserted_id), payload.email.lower())
    _set_auth_cookie(response, token)
    return user_to_public(user_doc)


@router.post("/login", response_model=UserPublic)
async def login(
    payload: UserLogin,
    response: Response,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> UserPublic:
    user = await db.users.find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    token = create_access_token(str(user["_id"]), user["email"])
    _set_auth_cookie(response, token)
    return user_to_public(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(
        key=ACCESS_COOKIE,
        domain=settings.cookie_domain,
        path="/",
    )


@router.get("/me", response_model=UserPublic)
async def get_me(user: CurrentUser) -> UserPublic:
    return user_to_public(user)
