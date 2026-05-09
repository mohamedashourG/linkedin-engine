"""
Replies + Unipile-account routes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.celery_app import poll_replies as poll_replies_task
from app.database import get_db
from app.models.common import utcnow
from app.config import settings
from app.services.unipile import (
    UnipileError,
    UnipileNotConfigured,
    create_hosted_auth_link,
    find_account_by_name,
    list_accounts,
)

router = APIRouter(prefix="/api/replies", tags=["replies"])


# ---------------------------------------------------------------- models

class ReplyPublic(BaseModel):
    id: str
    candidate_id: str
    cofounder_id: str
    lead_id: str | None = None
    reply_text: str
    reply_author_name: str | None
    reply_author_linkedin_url: str | None
    reply_author_is_post_owner: bool
    reply_published_at: datetime | None
    detected_at: datetime
    suggested_reply: str
    suggested_reply_type: str
    user_action: str
    candidate_post_text: str | None = None
    candidate_post_url: str | None = None
    cofounder_name: str | None = None
    our_comment: str | None = None


class ReplyActionRequest(BaseModel):
    action: Literal["sent", "dismissed", "edited"]
    edited_text: str | None = None


class UnreadCount(BaseModel):
    unread: int


class UnipileAccountPublic(BaseModel):
    id: str
    name: str
    profile_url: str | None = None
    avatar_url: str | None = None
    account_type: str | None = None


def _reply_to_public(
    reply: dict[str, Any],
    candidate: dict[str, Any] | None,
    cofounder: dict[str, Any] | None,
) -> ReplyPublic:
    return ReplyPublic(
        id=str(reply["_id"]),
        candidate_id=str(reply["candidate_id"]),
        cofounder_id=str(reply["cofounder_id"]),
        lead_id=str(reply["lead_id"]) if reply.get("lead_id") else None,
        reply_text=reply.get("reply_text") or "",
        reply_author_name=reply.get("reply_author_name"),
        reply_author_linkedin_url=reply.get("reply_author_linkedin_url"),
        reply_author_is_post_owner=bool(reply.get("reply_author_is_post_owner")),
        reply_published_at=reply.get("reply_published_at"),
        detected_at=reply["detected_at"],
        suggested_reply=reply.get("suggested_reply") or "",
        suggested_reply_type=reply.get("suggested_reply_type") or "A",
        user_action=reply.get("user_action") or "pending",
        candidate_post_text=(candidate or {}).get("post_text"),
        candidate_post_url=(candidate or {}).get("post_url"),
        our_comment=(candidate or {}).get("comment_text"),
        cofounder_name=(cofounder or {}).get("display_name"),
    )


# ---------------------------------------------------------------- list

@router.get("/", response_model=list[ReplyPublic])
async def list_replies(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    days: int = 14,
) -> list[ReplyPublic]:
    cutoff = utcnow() - timedelta(days=max(1, days))
    cursor = db.replies.find(
        {"operator_id": user["_id"], "detected_at": {"$gte": cutoff}}
    ).sort("detected_at", -1)
    rows = await cursor.to_list(length=200)
    if not rows:
        return []
    candidate_ids = list({r["candidate_id"] for r in rows})
    cofounder_ids = list({r["cofounder_id"] for r in rows})
    candidates = {
        c["_id"]: c
        async for c in db.candidates.find({"_id": {"$in": candidate_ids}})
    }
    cofounders = {
        cf["_id"]: cf
        async for cf in db.cofounders.find({"_id": {"$in": cofounder_ids}})
    }
    return [
        _reply_to_public(r, candidates.get(r["candidate_id"]), cofounders.get(r["cofounder_id"]))
        for r in rows
    ]


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> UnreadCount:
    cutoff = utcnow() - timedelta(days=14)
    n = await db.replies.count_documents(
        {
            "operator_id": user["_id"],
            "detected_at": {"$gte": cutoff},
            "user_action": "pending",
        }
    )
    return UnreadCount(unread=n)


@router.put("/{reply_id}/action")
async def reply_action(
    reply_id: Annotated[str, Path()],
    payload: ReplyActionRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    if not ObjectId.is_valid(reply_id):
        raise HTTPException(400, "Invalid id")
    update: dict[str, Any] = {
        "user_action": payload.action,
        "acted_at": utcnow(),
        "updated_at": utcnow(),
    }
    if payload.action == "edited" and payload.edited_text:
        update["suggested_reply"] = payload.edited_text
    result = await db.replies.find_one_and_update(
        {"_id": ObjectId(reply_id), "operator_id": user["_id"]},
        {"$set": update},
        return_document=True,
    )
    if not result:
        raise HTTPException(404, "Reply not found")
    return {"ok": True}


@router.post("/poll-now")
async def poll_now(user: CurrentUser) -> dict[str, str]:
    """Manually run the reply monitor once across all operators (testing/recovery)."""
    result = poll_replies_task.delay()
    return {"task_id": result.id, "status": "queued"}


# ---------------------------------------------------------------- unipile

@router.get("/unipile/accounts", response_model=list[UnipileAccountPublic])
async def unipile_accounts(user: CurrentUser) -> list[UnipileAccountPublic]:
    try:
        accs = list_accounts()
    except UnipileNotConfigured as err:
        raise HTTPException(503, str(err))
    except UnipileError as err:
        raise HTTPException(502, f"Unipile error: {err}")
    return [
        UnipileAccountPublic(
            id=a.id,
            name=a.name,
            profile_url=a.profile_url,
            avatar_url=a.avatar_url,
            account_type=a.account_type,
        )
        for a in accs
    ]


# ---------------------------------------------------------------- hosted-auth flow

class ConnectLinkResponse(BaseModel):
    url: str
    name: str


def _correlation_name(cofounder_id: str) -> str:
    """The `name` we hand Unipile during hosted-auth so the resulting account
    can be matched back to the right cofounder when we sync."""
    return f"cof_{cofounder_id}"


@router.post(
    "/unipile/connect/{cofounder_id}", response_model=ConnectLinkResponse
)
async def connect_cofounder_to_unipile(
    cofounder_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ConnectLinkResponse:
    if not ObjectId.is_valid(cofounder_id):
        raise HTTPException(400, "Invalid id")
    cf = await db.cofounders.find_one(
        {"_id": ObjectId(cofounder_id), "operator_id": user["_id"]}
    )
    if not cf:
        raise HTTPException(404, "Cofounder not found")

    name = _correlation_name(cofounder_id)
    app_url = settings.app_url.rstrip("/")
    success_url = f"{app_url}/settings?connected={cofounder_id}"
    failure_url = f"{app_url}/settings?failed={cofounder_id}"
    try:
        url = create_hosted_auth_link(
            name=name,
            success_redirect_url=success_url,
            failure_redirect_url=failure_url,
        )
    except UnipileNotConfigured as err:
        raise HTTPException(503, str(err))
    except UnipileError as err:
        raise HTTPException(502, f"Unipile error: {err}")
    return ConnectLinkResponse(url=url, name=name)


class SyncResponse(BaseModel):
    attached: bool
    account_id: str | None = None


@router.post(
    "/unipile/sync/{cofounder_id}", response_model=SyncResponse
)
async def sync_cofounder_unipile(
    cofounder_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SyncResponse:
    if not ObjectId.is_valid(cofounder_id):
        raise HTTPException(400, "Invalid id")
    cf = await db.cofounders.find_one(
        {"_id": ObjectId(cofounder_id), "operator_id": user["_id"]}
    )
    if not cf:
        raise HTTPException(404, "Cofounder not found")

    name = _correlation_name(cofounder_id)
    try:
        account = find_account_by_name(name)
    except UnipileNotConfigured as err:
        raise HTTPException(503, str(err))
    except UnipileError as err:
        raise HTTPException(502, f"Unipile error: {err}")

    if not account:
        return SyncResponse(attached=False)

    await db.cofounders.update_one(
        {"_id": cf["_id"]},
        {
            "$set": {
                "unipile_account_id": account.id,
                "updated_at": utcnow(),
            }
        },
    )
    return SyncResponse(attached=True, account_id=account.id)
