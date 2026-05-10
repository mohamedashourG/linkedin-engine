"""
Manual target-contact list. CRUD on `discovery_seeds` with source="manual".
Supports three import paths:
  1. Single contact form (POST /)
  2. Bulk text paste  (POST /bulk)
  3. CSV/Excel upload (POST /upload)
"""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path, Query, UploadFile, File, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.database import get_db
from app.models.common import utcnow
from app.models.contact import (
    ContactBulkRequest,
    ContactCreate,
    ContactPublic,
    contact_to_public,
    parse_bulk,
    parse_csv_bytes,
    parse_excel_bytes,
)


class ContactBulkDeleteRequest(BaseModel):
    """Body for POST /api/contacts/bulk-delete.

    `ids` is a list of contact IDs to delete. Empty list is a no-op (returns 0).
    To wipe the entire list, use `DELETE /api/contacts/` instead.
    """

    ids: list[str] = Field(default_factory=list)


class ContactBulkGroupRequest(BaseModel):
    """Body for POST /api/contacts/bulk-group.

    Assigns every id in `ids` to `group`. Pass `group=null` to clear the
    group label (move back to "ungrouped").
    """

    ids: list[str] = Field(default_factory=list)
    group: str | None = Field(default=None, max_length=120)


class ActiveGroupRequest(BaseModel):
    """Body for PUT /api/contacts/active-group.

    `group=null` clears the active filter — discovery walks every contact.
    A non-empty string scopes discovery to ONLY contacts with that group.
    """

    group: str | None = Field(default=None, max_length=120)


class GroupSummary(BaseModel):
    name: str | None  # None = ungrouped bucket
    count: int


class GroupsResponse(BaseModel):
    groups: list[GroupSummary]
    total_contacts: int
    active_group: str | None  # None = "all contacts" (no filter)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

# Manual seeds get a 10-year TTL so the discovery filter (`expires_at > now`)
# keeps them around indefinitely without changing the existing query shape.
_MANUAL_TTL = timedelta(days=3650)


def _seed_doc(
    operator_id: ObjectId,
    payload: ContactCreate,
    *,
    group: str | None = None,
) -> dict[str, Any]:
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
        # Group label: per-batch override beats per-payload (the form / file
        # upload sets one group for the whole batch). Stored as None when
        # blank so the discovery filter `{group: $eq: None}` works.
        "group": (group or payload.group or None),
        "expires_at": now + _MANUAL_TTL,
        "created_at": now,
        "updated_at": now,
    }


def _matches(seed: dict[str, Any], payload: ContactCreate) -> bool:
    if payload.linkedin_url and seed.get("linkedin_url"):
        return seed["linkedin_url"] == str(payload.linkedin_url)
    return (seed.get("extracted_name") or "").lower() == payload.name.lower()


async def _auto_group_for_operator(
    db: AsyncIOMotorDatabase,
    operator_id: ObjectId,
    *,
    suggested: str | None,
    fallback_prefix: str,
) -> str:
    """Pick a never-collides group name for a new import.

    `suggested` — preferred base (e.g., the uploaded filename without
    extension). Sanitised + truncated to 80 chars.
    `fallback_prefix` — used when `suggested` is empty. Combined with the
    current UTC timestamp ("Pasted list 2026-05-09 22:31").

    Collision strategy: append " (2)", " (3)", ... until unused. Never
    overwrites an existing group's contents.
    """
    base = (suggested or "").strip()
    if not base:
        base = f"{fallback_prefix} {utcnow().strftime('%Y-%m-%d %H:%M')}"
    base = base[:80].strip(" -_")
    if not base:
        # Pathological case: suggested was 80 chars of separators. Fall
        # back to the timestamp form.
        base = f"{fallback_prefix} {utcnow().strftime('%Y-%m-%d %H:%M')}"

    existing_raw = await db.discovery_seeds.distinct(
        "group", {"operator_id": operator_id, "source": "manual"}
    )
    existing = {g for g in existing_raw if isinstance(g, str) and g.strip()}

    if base not in existing:
        return base
    n = 2
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def _stem_from_filename(filename: str | None) -> str | None:
    """Filename minus the trailing extension. Used as the suggested group
    name on file upload."""
    if not filename:
        return None
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem.strip() or None


@router.get("/", response_model=list[ContactPublic])
async def list_contacts(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    group: Annotated[
        str | None,
        Query(
            description=(
                "Filter to one group. Use the literal string '__ungrouped__' "
                "to fetch only contacts with no group assigned. Omit to fetch "
                "every contact regardless of group."
            )
        ),
    ] = None,
) -> list[ContactPublic]:
    query: dict[str, Any] = {"operator_id": user["_id"], "source": "manual"}
    if group == "__ungrouped__":
        query["$or"] = [{"group": None}, {"group": {"$exists": False}}, {"group": ""}]
    elif group:
        query["group"] = group
    cursor = db.discovery_seeds.find(query).sort("created_at", -1)
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
) -> dict[str, Any]:
    parsed = parse_bulk(payload.text)
    if not parsed:
        return {"inserted": 0, "skipped_duplicate": 0, "parsed": 0, "group": None}

    existing = await db.discovery_seeds.find(
        {"operator_id": user["_id"], "source": "manual"}
    ).to_list(length=None)

    # If the operator didn't pick / type a group, mint a unique one so the
    # batch lands in its own bucket instead of swelling "Ungrouped".
    batch_group = (payload.group or "").strip() or None
    if not batch_group:
        batch_group = await _auto_group_for_operator(
            db, user["_id"], suggested=None, fallback_prefix="Pasted list"
        )

    inserted = 0
    skipped = 0
    to_insert: list[dict[str, Any]] = []
    matched_existing_groups: list[str | None] = []
    for c in parsed:
        existing_match = next((s for s in existing if _matches(s, c)), None)
        if existing_match is not None:
            matched_existing_groups.append(existing_match.get("group") or None)
            skipped += 1
            continue
        if any(
            _matches({"linkedin_url": d.get("linkedin_url"), "extracted_name": d.get("extracted_name")}, c)
            for d in to_insert
        ):
            skipped += 1
            continue
        to_insert.append(_seed_doc(user["_id"], c, group=batch_group))
        inserted += 1

    if to_insert:
        await db.discovery_seeds.insert_many(to_insert)

    if inserted > 0:
        effective_group: str | None = batch_group
    elif matched_existing_groups:
        unique = {g for g in matched_existing_groups if g}
        effective_group = unique.pop() if len(unique) == 1 else None
    else:
        effective_group = None

    return {
        "inserted": inserted,
        "skipped_duplicate": skipped,
        "parsed": len(parsed),
        "group": effective_group,
    }


_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


@router.post("/upload")
async def upload_file(
    file: Annotated[UploadFile, File(description="CSV or Excel (.xlsx) file")],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    group: Annotated[
        str | None,
        Query(description="Optional group label to assign to every uploaded contact."),
    ] = None,
) -> dict[str, Any]:
    """Import contacts from a CSV or Excel file.

    Accepts `.csv`, `.xlsx`, `.xls` files up to 5 MB.
    Column headers are auto-detected (name/title/company/linkedin url).
    Pass `?group=Foo` to assign every imported row to a group.
    """
    if not file.filename:
        raise HTTPException(400, "No file provided")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("csv", "xlsx", "xls", "tsv"):
        raise HTTPException(
            400,
            f"Unsupported file type '.{ext}'. Upload a .csv or .xlsx file.",
        )

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (max 5 MB)")

    try:
        if ext in ("xlsx", "xls"):
            parsed = parse_excel_bytes(data)
        else:
            parsed = parse_csv_bytes(data)
    except Exception as err:
        raise HTTPException(400, f"Failed to parse file: {err}")

    if not parsed:
        return {
            "inserted": 0,
            "skipped_duplicate": 0,
            "parsed": 0,
            "filename": file.filename,
            "group": None,
        }

    existing = await db.discovery_seeds.find(
        {"operator_id": user["_id"], "source": "manual"}
    ).to_list(length=None)

    # If the operator didn't pick / type a group, derive one from the
    # filename (minus extension) and make it unique. Keeps every upload
    # in its own bucket so they don't collapse into "Ungrouped".
    batch_group = (group or "").strip() or None
    if not batch_group:
        batch_group = await _auto_group_for_operator(
            db,
            user["_id"],
            suggested=_stem_from_filename(file.filename),
            fallback_prefix="Upload",
        )

    inserted = 0
    skipped = 0
    to_insert: list[dict[str, Any]] = []
    matched_existing_groups: list[str | None] = []
    for c in parsed:
        existing_match = next((s for s in existing if _matches(s, c)), None)
        if existing_match is not None:
            matched_existing_groups.append(existing_match.get("group") or None)
            skipped += 1
            continue
        if any(
            _matches(
                {"linkedin_url": d.get("linkedin_url"), "extracted_name": d.get("extracted_name")},
                c,
            )
            for d in to_insert
        ):
            skipped += 1
            continue
        to_insert.append(_seed_doc(user["_id"], c, group=batch_group))
        inserted += 1

    if to_insert:
        await db.discovery_seeds.insert_many(to_insert)

    if inserted > 0:
        effective_group: str | None = batch_group
    elif matched_existing_groups:
        unique = {g for g in matched_existing_groups if g}
        effective_group = unique.pop() if len(unique) == 1 else None
    else:
        effective_group = None

    return {
        "inserted": inserted,
        "skipped_duplicate": skipped,
        "parsed": len(parsed),
        "filename": file.filename,
        "group": effective_group,
    }


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


@router.post("/bulk-delete")
async def bulk_delete(
    payload: ContactBulkDeleteRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, int]:
    """Delete a set of manual contacts by id."""
    if not payload.ids:
        return {"deleted": 0}
    object_ids = [ObjectId(i) for i in payload.ids if ObjectId.is_valid(i)]
    if not object_ids:
        raise HTTPException(400, "No valid ids provided")
    result = await db.discovery_seeds.delete_many(
        {
            "_id": {"$in": object_ids},
            "operator_id": user["_id"],
            "source": "manual",
        }
    )
    return {"deleted": result.deleted_count}


@router.get("/groups", response_model=GroupsResponse)
async def list_groups(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> GroupsResponse:
    """Distinct groups + member counts + the operator's active group."""
    pipeline = [
        {"$match": {"operator_id": user["_id"], "source": "manual"}},
        {"$group": {"_id": "$group", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    cursor = db.discovery_seeds.aggregate(pipeline)
    rows = await cursor.to_list(length=None)
    groups: list[GroupSummary] = []
    total = 0
    for r in rows:
        name = r.get("_id")
        # Mongo returns "" for blank-string groups and None for missing
        # field; collapse both into None so the frontend has one bucket.
        if not name:
            name = None
        groups.append(GroupSummary(name=name, count=int(r.get("count", 0))))
        total += int(r.get("count", 0))
    # Sort: real groups alphabetical, ungrouped bucket last.
    groups.sort(key=lambda g: (g.name is None, (g.name or "").lower()))
    return GroupsResponse(
        groups=groups,
        total_contacts=total,
        active_group=user.get("active_contact_group") or None,
    )


@router.post("/bulk-group")
async def bulk_assign_group(
    payload: ContactBulkGroupRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, int]:
    """Assign a set of contact IDs to a group. `group=null` clears the
    label (moves contacts back to the ungrouped bucket)."""
    if not payload.ids:
        return {"updated": 0}
    object_ids = [ObjectId(i) for i in payload.ids if ObjectId.is_valid(i)]
    if not object_ids:
        raise HTTPException(400, "No valid ids provided")
    new_group = (payload.group or "").strip() or None
    result = await db.discovery_seeds.update_many(
        {
            "_id": {"$in": object_ids},
            "operator_id": user["_id"],
            "source": "manual",
        },
        {"$set": {"group": new_group, "updated_at": utcnow()}},
    )
    return {"updated": result.modified_count}


@router.put("/active-group")
async def set_active_group(
    payload: ActiveGroupRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, str | None]:
    """Set the operator's active contact group. When set, daily runs only
    walk contacts with this group label. Pass `group=null` to walk every
    contact regardless of group."""
    new_group = (payload.group or "").strip() or None
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"active_contact_group": new_group, "updated_at": utcnow()}},
    )
    return {"active_group": new_group}


@router.delete("/")
async def delete_all_contacts(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, int]:
    """Wipe every manual contact for the current user."""
    result = await db.discovery_seeds.delete_many(
        {"operator_id": user["_id"], "source": "manual"}
    )
    return {"deleted": result.deleted_count}
