"""
Manual target-contact list. CRUD on `discovery_seeds` with source="manual".
"""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth.deps import CurrentUser
from app.database import get_db
from app.models.common import utcnow
from app.models.contact import (
    ContactBulkRequest,
    ContactCreate,
    ContactPublic,
    contact_to_public,
    parse_bulk,
)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

# Manual seeds get a 10-year TTL so the discovery filter (`expires_at > now`)
# keeps them around indefinitely without changing the existing query shape.
_MANUAL_TTL = timedelta(days=3650)


def _seed_doc(operator_id: ObjectId, payload: ContactCreate) -> dict[str, Any]:
    now = utcnow()
    return {
        "operator_id": operator_id,
        "source": "manual",
        "source_candidate_id": None,
        "extracted_name": payload.name,
        "extracted_title": payload.title,
        "extracted_company": payload.company,
        "linkedin_url": str(payload.linkedin_url) if payload.linkedin_url else None,
        "pattern": "manual",
        "status": "pending",
        "expires_at": now + _MANUAL_TTL,
        "created_at": now,
        "updated_at": now,
    }


def _matches(seed: dict[str, Any], payload: ContactCreate) -> bool:
    if payload.linkedin_url and seed.get("linkedin_url"):
        return seed["linkedin_url"] == str(payload.linkedin_url)
    return (seed.get("extracted_name") or "").lower() == payload.name.lower()


@router.get("/", response_model=list[ContactPublic])
async def list_contacts(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> list[ContactPublic]:
    cursor = db.discovery_seeds.find(
        {"operator_id": user["_id"], "source": "manual"}
    ).sort("created_at", -1)
    docs = await cursor.to_list(length=None)
    return [contact_to_public(d) for d in docs]


@router.post(
    "/", response_model=ContactPublic, status_code=status.HTTP_201_CREATED
)
async def create_contact(
    payload: ContactCreate,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ContactPublic:
    doc = _seed_doc(user["_id"], payload)
    result = await db.discovery_seeds.insert_one(doc)
    doc["_id"] = result.inserted_id
    return contact_to_public(doc)


@router.post("/bulk")
async def bulk_import(
    payload: ContactBulkRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, int]:
    parsed = parse_bulk(payload.text)
    if not parsed:
        return {"inserted": 0, "skipped_duplicate": 0, "parsed": 0}

    existing = await db.discovery_seeds.find(
        {"operator_id": user["_id"], "source": "manual"}
    ).to_list(length=None)

    inserted = 0
    skipped = 0
    to_insert: list[dict[str, Any]] = []
    for c in parsed:
        if any(_matches(s, c) for s in existing) or any(
            _matches({"linkedin_url": d.get("linkedin_url"), "extracted_name": d.get("extracted_name")}, c)
            for d in to_insert
        ):
            skipped += 1
            continue
        to_insert.append(_seed_doc(user["_id"], c))
        inserted += 1

    if to_insert:
        await db.discovery_seeds.insert_many(to_insert)
    return {"inserted": inserted, "skipped_duplicate": skipped, "parsed": len(parsed)}


@router.delete("/{contact_id}")
async def delete_contact(
    contact_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, bool]:
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(400, "Invalid id")
    result = await db.discovery_seeds.delete_one(
        {"_id": ObjectId(contact_id), "operator_id": user["_id"], "source": "manual"}
    )
    if result.deleted_count == 0:
        raise HTTPException(404, "Contact not found")
    return {"ok": True}
