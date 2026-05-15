"""API endpoints for the Unipile account pool dashboard.

Exposes read-only state for the UI's live pool view plus a couple of
admin actions (sync from Unipile, manual cooldown reset, role change).
All endpoints require the standard CurrentUser auth.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.database import get_db
from app.services.unipile_pool import get_pool

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/unipile-pool", tags=["unipile-pool"])


class PoolAccountPublic(BaseModel):
    account_id: str
    display_name: str
    operator_id: str | None = None
    role: str  # discovery | posting | stats | disabled
    capabilities: list[str]
    proxy_country: str
    status: str  # OK | COOLDOWN | CREDENTIALS | DISABLED
    cooldown_until: datetime | None = None
    cooldown_seconds_remaining: int | None = None
    daily_caps: dict[str, int]
    daily_usage: dict[str, Any]
    last_used_at: datetime | None = None
    last_429_at: datetime | None = None
    consecutive_errors: int = 0
    consecutive_429s: int = 0
    total_calls: int = 0
    total_errors: int = 0


class PoolListResponse(BaseModel):
    accounts: list[PoolAccountPublic]
    # Aggregate summary so the dashboard can show a single "X/Y healthy"
    # KPI without re-deriving it client-side.
    summary: dict[str, int]


def _to_public(doc: dict[str, Any], *, now: datetime) -> PoolAccountPublic:
    cooldown_until = doc.get("cooldown_until")
    remaining: int | None = None
    if cooldown_until is not None and cooldown_until > now:
        remaining = int((cooldown_until - now).total_seconds())
    return PoolAccountPublic(
        account_id=doc.get("account_id", ""),
        display_name=doc.get("display_name", ""),
        operator_id=str(doc["operator_id"]) if doc.get("operator_id") else None,
        role=doc.get("role", "discovery"),
        capabilities=list(doc.get("capabilities") or []),
        proxy_country=doc.get("proxy_country", "?"),
        status=doc.get("status", "OK"),
        cooldown_until=cooldown_until,
        cooldown_seconds_remaining=remaining,
        daily_caps=dict(doc.get("daily_caps") or {}),
        daily_usage=dict(doc.get("daily_usage") or {}),
        last_used_at=doc.get("last_used_at"),
        last_429_at=doc.get("last_429_at"),
        consecutive_errors=int(doc.get("consecutive_errors") or 0),
        consecutive_429s=int(doc.get("consecutive_429s") or 0),
        total_calls=int(doc.get("total_calls") or 0),
        total_errors=int(doc.get("total_errors") or 0),
    )


@router.get("/accounts", response_model=PoolListResponse)
async def list_pool_accounts(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> PoolListResponse:
    """All pool accounts + their live health/usage state.

    Returned ordering is role-then-name so the dashboard groups
    discovery / posting / stats accounts predictably. Per-account
    state includes cooldown-seconds-remaining so the UI can render
    a live countdown without recomputing relative time client-side.
    """
    # Use the sync pool's collection through a fresh Mongo find — keeps
    # this endpoint async-friendly with Motor without coupling to the
    # pool's internal pymongo handle.
    docs = await db["unipile_account_pool"].find().to_list(length=None)
    now = datetime.utcnow()
    accounts = [_to_public(d, now=now) for d in docs]
    # Sort discovery first, then posting, then stats, then disabled.
    role_order = {"discovery": 0, "posting": 1, "stats": 2, "disabled": 3}
    accounts.sort(
        key=lambda a: (role_order.get(a.role, 9), a.display_name.lower()),
    )

    summary = {
        "total": len(accounts),
        "ok": sum(1 for a in accounts if a.status == "OK"),
        "cooldown": sum(1 for a in accounts if a.cooldown_seconds_remaining),
        "credentials": sum(1 for a in accounts if a.status == "CREDENTIALS"),
        "disabled": sum(1 for a in accounts if a.status == "DISABLED"),
        "discovery": sum(1 for a in accounts if a.role == "discovery"),
        "posting": sum(1 for a in accounts if a.role == "posting"),
        "stats": sum(1 for a in accounts if a.role == "stats"),
    }
    return PoolListResponse(accounts=accounts, summary=summary)


class SyncResponse(BaseModel):
    created: int
    updated: int


@router.post("/sync", response_model=SyncResponse)
async def sync_pool(user: CurrentUser) -> SyncResponse:
    """Pull the current Unipile account list and upsert each into the
    pool collection. Refreshes status + capabilities + proxy info for
    existing accounts; inserts new ones with role=discovery default.

    Operator-assigned role/cooldown/usage fields are NOT clobbered —
    only volatile fields the dashboard cares about. Safe to call as
    often as you want; idempotent."""
    pool = get_pool()
    try:
        # The pool service is sync — run it inline; the call hits Unipile
        # (small request, ~1s) and a few Mongo upserts.
        import asyncio
        result = await asyncio.to_thread(pool.sync_from_unipile)
    except Exception as err:
        log.exception("pool.sync failed: %s", err)
        raise HTTPException(502, f"Sync failed: {err}")
    return SyncResponse(created=result.get("created", 0), updated=result.get("updated", 0))


class CooldownResetResponse(BaseModel):
    account_id: str
    reset: bool


@router.post(
    "/accounts/{account_id}/reset-cooldown",
    response_model=CooldownResetResponse,
)
async def reset_cooldown(
    account_id: Annotated[str, Path()],
    user: CurrentUser,
) -> CooldownResetResponse:
    """Manually clear an account's cooldown_until + reset 429 counters.

    Useful when an operator knows a flagged-as-429 event was transient
    (e.g. a one-off network blip or a misclassified error) and wants
    to put the account back in rotation immediately rather than waiting
    out the random 20-60min cooldown window."""
    pool = get_pool()
    import asyncio
    ok = await asyncio.to_thread(pool.reset_cooldown, account_id)
    return CooldownResetResponse(account_id=account_id, reset=ok)


class RoleUpdateRequest(BaseModel):
    role: Literal["discovery", "posting", "stats", "disabled"] = Field(
        ..., description="New role for this account in the pool"
    )


class RoleUpdateResponse(BaseModel):
    account_id: str
    role: str
    updated: bool


# ── Operator's per-run pool selection (allowlist) ──────────────────────


class PoolSelectionResponse(BaseModel):
    """Operator's saved pool-account selection.

    Semantics of ``account_ids``:
      * ``None`` → no filter; future runs use ALL discovery accounts
      * ``[]``   → empty allowlist; future runs would have no eligible
                   accounts (effectively disables pool-routed discovery)
      * ``[...]``→ strict allowlist; only these accounts can be acquired
                   for discovery in future runs
    """
    account_ids: list[str] | None = None
    saved_at: datetime | None = None


class PoolSelectionUpdateRequest(BaseModel):
    account_ids: list[str] | None = None


@router.get("/selection", response_model=PoolSelectionResponse)
async def get_pool_selection(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> PoolSelectionResponse:
    """Operator's currently-saved pool selection.

    Snapshotted onto every new ``slate_run`` at run creation, so changes
    here only affect FUTURE runs. Past runs keep the snapshot they were
    started with."""
    u = await db.users.find_one(
        {"_id": user["_id"]},
        {"pool_account_ids": 1, "pool_account_ids_saved_at": 1},
    )
    return PoolSelectionResponse(
        account_ids=(u or {}).get("pool_account_ids"),
        saved_at=(u or {}).get("pool_account_ids_saved_at"),
    )


@router.put("/selection", response_model=PoolSelectionResponse)
async def update_pool_selection(
    payload: PoolSelectionUpdateRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> PoolSelectionResponse:
    """Update which Unipile accounts are eligible for THIS operator's
    discovery runs.

    Validation: every account_id in the list must exist in the pool
    collection AND have role=discovery. Posting / stats accounts can't
    be in the allowlist (they're acquired via different role paths).
    Unknown account_ids fail loudly so the UI never silently drops a
    selection."""
    now = datetime.utcnow()
    chosen = payload.account_ids
    if chosen is not None:
        # Validate every id is a known discovery account.
        if chosen:
            docs = await db["unipile_account_pool"].find(
                {"account_id": {"$in": chosen}, "role": "discovery"},
                {"account_id": 1},
            ).to_list(length=None)
            found = {d["account_id"] for d in docs}
            missing = [a for a in chosen if a not in found]
            if missing:
                raise HTTPException(
                    400,
                    f"Unknown / non-discovery account(s): {missing}. "
                    "Run a Unipile pool sync first or check the Pool dashboard.",
                )
        # Dedupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for a in chosen:
            if a and a not in seen:
                seen.add(a)
                deduped.append(a)
        chosen = deduped
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "pool_account_ids": chosen,
                "pool_account_ids_saved_at": now,
            },
        },
    )
    return PoolSelectionResponse(account_ids=chosen, saved_at=now)


class RunPoolStatePublic(BaseModel):
    """Pool state scoped to one slate_run.

    Shows ONLY the accounts that were in the run's allowlist at start
    (or the full discovery pool if no allowlist was set), along with
    each account's current live state. Used by the run-detail page to
    show "which accounts participated in this run".
    """
    slate_run_id: str
    pool_account_ids_snapshot: list[str] | None  # what was set at run start
    accounts: list[PoolAccountPublic]


@router.get("/runs/{slate_run_id}", response_model=RunPoolStatePublic)
async def get_run_pool_state(
    slate_run_id: Annotated[str, Path()],
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> RunPoolStatePublic:
    """Per-run snapshot: which accounts were allowed for this run, with
    their current live health/usage state.

    If ``pool_account_ids`` on the run is None, returns every discovery-
    role account (= the default "no allowlist" behavior). If it's a list,
    returns only those accounts so the UI never accidentally shows an
    account that didn't participate."""
    from bson import ObjectId
    try:
        oid = ObjectId(slate_run_id)
    except Exception:
        raise HTTPException(400, "Invalid slate_run_id")
    slate = await db.slate_runs.find_one(
        {"_id": oid, "operator_id": user["_id"]},
        {"pool_account_ids": 1},
    )
    if not slate:
        raise HTTPException(404, "Slate run not found")
    snapshot = slate.get("pool_account_ids")

    query: dict[str, Any] = {}
    if snapshot is None:
        # No allowlist set on this run → show all discovery accounts.
        query = {"role": "discovery"}
    elif not snapshot:
        # Empty allowlist → empty result.
        return RunPoolStatePublic(
            slate_run_id=slate_run_id,
            pool_account_ids_snapshot=[],
            accounts=[],
        )
    else:
        query = {"account_id": {"$in": snapshot}}
    docs = await db["unipile_account_pool"].find(query).to_list(length=None)
    now = datetime.utcnow()
    accounts = [_to_public(d, now=now) for d in docs]
    accounts.sort(key=lambda a: a.display_name.lower())
    return RunPoolStatePublic(
        slate_run_id=slate_run_id,
        pool_account_ids_snapshot=snapshot,
        accounts=accounts,
    )


@router.post("/accounts/{account_id}/role", response_model=RoleUpdateResponse)
async def update_role(
    account_id: Annotated[str, Path()],
    payload: RoleUpdateRequest,
    user: CurrentUser,
) -> RoleUpdateResponse:
    """Change an account's pool role.

      - ``discovery``: shared pool for keyword search / RULE 24 / profile
        view fallback. Most accounts live here.
      - ``posting``: pinned to one operator for comment writes.
      - ``stats``: reserved for engagement-refresh reads (manual-comments).
      - ``disabled``: temporarily excluded entirely.

    Promoting out of ``disabled`` restores status=OK so the account
    re-enters rotation."""
    pool = get_pool()
    import asyncio
    ok = await asyncio.to_thread(pool.set_role, account_id, payload.role)
    return RoleUpdateResponse(account_id=account_id, role=payload.role, updated=ok)
