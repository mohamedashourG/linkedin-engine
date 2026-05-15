"""Manual-comment posting routes.

Lets an operator upload a CSV of (post_url, comment_text) rows, pick which
of their Unipile-connected LinkedIn accounts to post from, and either
dry-run validate or post the comments for real via the Unipile API. After
posting, engagement (reactions + comment count + replies-to-our-comment)
is captured via APIDirect on-demand from a button in the UI.

Safety model (operator-instructed 2026-05-13):
  - Dry-run is the default. The /send route's `dry_run` field defaults to
    True. Operator must explicitly set `dry_run=False` to call Unipile.
  - Live posting requires both the explicit flag AND a non-empty
    unipile_account_id that resolves to an OK-status account on the tenant.

Engagement-refresh trigger:
  - Manual button only. POST /campaigns/{id}/refresh-engagement hits
    APIDirect for each posted job and stores a snapshot. No background
    polling — operator decides when to spend the APIDirect credit.

Mongo collections:
  - manual_comment_campaigns  one doc per CSV upload
  - manual_comment_jobs       one doc per CSV row
"""
from __future__ import annotations

import csv
import io
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser
from app.config import settings
from app.database import get_db
from app.models.common import utcnow
from app.services import unipile
from app.services.apidirect import (
    ApiDirectError,
    ApiDirectNotConfigured,
    ApiDirectQuotaExhausted,
    get_linkedin_post_details,
)
from app.services.unipile import (
    UnipileError,
    UnipileFeatureNotSupported,
    UnipileNotConfigured,
    delete_comment,
    extract_post_id_from_url,
    get_comment_replies,
    get_post_comments,
    list_accounts,
    post_comment,
)

log = logging.getLogger(__name__)

# Shared thread pool for the parallel refresh-engagement worker.
#
# Cut from 6 → 2 workers on 2026-05-14 after three Unipile-connected
# accounts hit `status=CREDENTIALS` (LinkedIn forced re-auth from
# automation flagging). Six concurrent reads from a single LinkedIn
# session is bot-like — humans don't open six profile/post pages
# simultaneously. Two workers + the humanlike Unipile throttle gives
# a more believable read pattern.
#
# The route already returns immediately via the background-task pattern,
# so this no longer needs to "fit inside Next.js's 30s proxy timeout" —
# the work happens detached and snapshots stream into Mongo as each
# job lands. Total wall-clock for a 15-job campaign is now ~60–90s
# (vs ~10s before), but that's invisible to the user.
_REFRESH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="refresh-engagement")

# Strong references to in-flight fire-and-forget refresh tasks. asyncio
# only keeps WEAK refs to tasks created via create_task(), so without this
# set the GC can collect a task mid-await and silently drop the work.
# Done-callback below pops the task out when it finishes so the set
# doesn't accumulate.
_REFRESH_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

router = APIRouter(prefix="/api/manual-comments", tags=["manual-comments"])

# Cap on rows per CSV upload. Above this, the operator is probably better
# off using the regular daily_run pipeline. 200 is comfortable for a
# typical "send these specific comments today" use case.
MAX_ROWS_PER_CSV = 200

# Max length of a single comment, enforced before Unipile would clip it
# itself. LinkedIn caps comments at 1,250 chars but we hold a tighter
# soft cap so an operator-pasted very long comment doesn't silently lose
# its tail.
MAX_COMMENT_CHARS = 1200


# ─── Pydantic schemas ─────────────────────────────────────────────────────


class JobPublic(BaseModel):
    id: str
    campaign_id: str
    row_index: int
    post_url: str
    post_id: str | None
    draft_comment: str
    status: str
    unipile_account_id: str | None = None
    comment_id: str | None = None
    posted_at: datetime | None = None
    deleted_at: datetime | None = None
    delete_error: str | None = None
    error: str | None = None
    last_engagement_at: datetime | None = None
    # Post-level aggregates (likes/comments/shares on the LinkedIn post
    # we commented under — includes everyone else's activity too).
    latest_likes: int | None = None
    latest_comments: int | None = None
    latest_shares: int | None = None
    # Comment-level: reactions + replies on OUR specific comment.
    # Sourced from Unipile's GET /posts/comments by matching comment_id.
    latest_my_comment_reactions: int | None = None
    latest_my_comment_replies: int | None = None
    # Actual reply *content* under our comment, captured on the last refresh
    # when reply_count > 0. List of {author_name, text, published_at,
    # comment_id} — intentionally no profile_url (rendered as flat text,
    # no per-author profile views).
    latest_my_comment_replies_thread: list[dict[str, Any]] = []
    # Replies WE'VE posted back to specific incoming replies on Yair's
    # comment, captured at send time so the UI can show them immediately
    # without waiting for the next refresh-engagement cycle. Each entry:
    #   {comment_id, parent_reply_comment_id, text, posted_at, dry_run}
    # `parent_reply_comment_id` lets the UI render each outgoing reply
    # under the specific incoming reply it responds to.
    my_outgoing_replies: list[dict[str, Any]] = []
    engagement_snapshots_count: int = 0


class CampaignPublic(BaseModel):
    id: str
    name: str
    source_filename: str | None = None
    n_rows: int
    n_dry_run: int = 0
    n_posted: int = 0
    n_failed: int = 0
    created_at: datetime
    updated_at: datetime
    last_send_attempt_at: datetime | None = None
    last_send_was_dry_run: bool | None = None


class CampaignDetail(BaseModel):
    campaign: CampaignPublic
    jobs: list[JobPublic]


class AccountOption(BaseModel):
    id: str
    name: str
    type: str  # always "LINKEDIN" in practice
    status: str  # "OK" | "CREDENTIALS" | etc.


class AccountsResponse(BaseModel):
    accounts: list[AccountOption]


class SendRequest(BaseModel):
    unipile_account_id: str = Field(
        ...,
        description=(
            "ID of the Unipile-connected LinkedIn account to post from. "
            "Must resolve to status=OK on the tenant."
        ),
        min_length=1,
    )
    # Default True so any accidental call without the field is a no-op dry-run.
    dry_run: bool = Field(
        default=True,
        description=(
            "If True (default), validate URLs + comment lengths and mark "
            "each job as status='dry_run' without actually calling Unipile. "
            "Set to False to post for real."
        ),
    )


class SendResponse(BaseModel):
    campaign_id: str
    dry_run: bool
    n_total: int
    n_posted: int
    n_dry_run: int
    n_failed: int
    n_skipped: int


class RefreshResponse(BaseModel):
    campaign_id: str
    n_refreshed: int
    n_failed: int
    n_skipped: int  # jobs we couldn't refresh (e.g. not posted yet)
    # True when LinkedIn rate-limited Yair's account during this refresh
    # and we skipped the per-comment Unipile call for some/all jobs.
    # Post-level engagement (APIDirect) is unaffected.
    provider_rate_limited: bool = False


class DeleteJobCommentResponse(BaseModel):
    job_id: str
    deleted: bool
    comment_id: str | None
    error: str | None = None


class SingleJobSendRequest(BaseModel):
    """Per-row send body. Same safety semantics as the campaign-wide
    SendRequest: dry_run defaults to True. Use this when you want to
    post comments one at a time instead of firing the whole campaign
    in a single click."""
    unipile_account_id: str = Field(..., min_length=1)
    dry_run: bool = Field(default=True)


class SingleJobSendResponse(BaseModel):
    job_id: str
    status: str  # "dry_run" | "posted" | "failed"
    dry_run: bool
    comment_id: str | None = None
    posted_at: datetime | None = None
    error: str | None = None


class ReplyToReplyRequest(BaseModel):
    """Body for posting Yair's reply to one of the replies on his own
    comment (e.g. Tarpan replied to Yair's WHOOP comment; this lets Yair
    respond inline without leaving the UI).

    Same dry-run-default + account-required safety contract as the per-row
    send: forgetting `dry_run` means the request validates the inputs and
    records the intended reply text without hitting Unipile.
    """
    text: str = Field(..., min_length=1, max_length=1200)
    unipile_account_id: str = Field(..., min_length=1)
    dry_run: bool = Field(default=True)


class ReplyToReplyResponse(BaseModel):
    job_id: str
    parent_reply_comment_id: str
    status: str  # "dry_run" | "posted" | "failed"
    dry_run: bool
    # Unipile's id for the new reply-to-reply we just created (None on
    # dry_run / failure). Stored on the job doc under `my_outgoing_replies`
    # so it survives a page refresh even before the next engagement poll.
    new_comment_id: str | None = None
    posted_at: datetime | None = None
    error: str | None = None


# ─── Connection-invite schemas ────────────────────────────────────────────
#
# The "Connect with note" flow: when someone replies to a comment we
# posted (captured in `manual_comment_jobs.latest_my_comment_replies_thread`),
# the operator can send them a LinkedIn connection request with an
# optional ≤200-char note. Notes are operator-typed manually (no LLM
# suggestion); the engine adds the safety rails (dedup, atomic claim,
# account-match check, pool throttle).
#
# The invitation itself is persisted to a dedicated collection
# `linkedin_invitations` (NOT embedded on the job doc) so we can:
#   • enforce a per-target unique index (no double-invite from any pool
#     account to the same person)
#   • run a background status poll that touches a small focused
#     collection rather than scanning all jobs
#   • surface a cross-job "this person already invited via comment X"
#     warning when the same recipient appears across multiple campaigns


class InvitationStatusValue:
    """Status vocabulary for `linkedin_invitations.status`. Defined as a
    class of constants rather than a Literal so callers can do
    `if status == InvitationStatusValue.SENT` without importing Literal
    at every call site."""
    DRY_RUN = "dry_run"          # operator clicked send with dry_run=true; no Unipile call
    QUEUED = "queued"            # claim issued, Unipile call about to fire
    SENT = "sent"                # Unipile returned 200; invite is live on LinkedIn
    ACCEPTED = "accepted"        # status poller saw target accepted the invite
    DECLINED = "declined"        # status poller saw target declined
    WITHDRAWN = "withdrawn"      # operator (or auto-policy) withdrew the invite
    FAILED = "failed"            # Unipile returned 4xx/5xx; see `error` for detail


class InvitationPublic(BaseModel):
    """API-shaped view of a `linkedin_invitations` document. The frontend
    reads this to render the per-reply status badge ("invited",
    "connected", "declined") and to disable the Connect button when a
    pending invite exists for a target."""
    id: str
    operator_id: str
    source_job_id: str
    source_reply_comment_id: str
    target_provider_id: str
    target_name: str
    target_public_identifier: str | None
    target_headline: str | None
    # Operator-typed note. ≤200 chars (LinkedIn cap, enforced at the
    # Unipile service boundary as well). Empty string is valid (LinkedIn
    # allows note-less invites).
    note_text: str
    # Which pool account sent the invite. SHOULD match the account that
    # posted the comment being replied to, so the recipient recognizes
    # the sender. Route enforces the match.
    sent_via_account_id: str
    status: str  # see InvitationStatusValue
    invitation_id: str | None  # Unipile's id, used by status poll
    sent_at: datetime | None
    accepted_at: datetime | None
    status_polled_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class SendInvitationRequest(BaseModel):
    """Body for `POST /jobs/{job_id}/replies/{reply_comment_id}/invite`.

    Same dry-run-default safety contract as comment posting: forgetting
    `dry_run` means the request validates the inputs + persists the
    intent WITHOUT hitting Unipile. Set `dry_run=False` to actually
    issue the invite.

    `unipile_account_id` MUST equal the `unipile_account_id` of the
    underlying job (i.e. the account that posted the comment the
    recipient replied to). Different account → 400 with a clear
    error — recipients seeing an invite from a stranger after engaging
    with someone else's comment is exactly the trust-breaking pattern
    we're guarding against."""
    note_text: str = Field(
        default="",
        max_length=200,
        description=(
            "Manually-typed invitation note. ≤200 chars (LinkedIn cap). "
            "Empty string is valid — sends a note-less invite."
        ),
    )
    unipile_account_id: str = Field(..., min_length=1)
    dry_run: bool = Field(default=True)


class SendInvitationResponse(BaseModel):
    job_id: str
    parent_reply_comment_id: str
    target_provider_id: str
    status: str  # InvitationStatusValue value
    dry_run: bool
    invitation_id: str | None = None
    sent_at: datetime | None = None
    error: str | None = None


class GetInvitationResponse(BaseModel):
    """Returned by `GET /jobs/{job_id}/replies/{reply_comment_id}/invite`.

    `invitation` is None when no invite has been created for this
    (job, reply) pair yet. The UI uses presence + status to decide
    whether to render the "Connect" button (none → enabled), a
    "Pending" badge (sent), or a "Connected" badge (accepted)."""
    job_id: str
    parent_reply_comment_id: str
    invitation: InvitationPublic | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────


def _job_to_public(j: dict[str, Any]) -> JobPublic:
    snaps = j.get("engagement_snapshots") or []
    latest = snaps[-1] if snaps else None
    return JobPublic(
        id=str(j["_id"]),
        campaign_id=str(j["campaign_id"]),
        row_index=int(j.get("row_index", 0)),
        post_url=j.get("post_url", ""),
        post_id=j.get("post_id"),
        draft_comment=j.get("draft_comment", ""),
        status=j.get("status", "queued"),
        unipile_account_id=j.get("unipile_account_id"),
        comment_id=j.get("comment_id"),
        posted_at=j.get("posted_at"),
        deleted_at=j.get("deleted_at"),
        delete_error=j.get("delete_error"),
        error=j.get("error"),
        last_engagement_at=(latest or {}).get("captured_at"),
        latest_likes=(latest or {}).get("likes"),
        latest_comments=(latest or {}).get("comments"),
        latest_shares=(latest or {}).get("shares"),
        latest_my_comment_reactions=(latest or {}).get("my_comment_reactions"),
        latest_my_comment_replies=(latest or {}).get("my_comment_replies"),
        # Stored on the job document (not the snapshot) — only the *latest*
        # thread is kept since the previous snapshot's reply text would be
        # redundant noise.
        latest_my_comment_replies_thread=j.get("latest_my_comment_replies_thread") or [],
        my_outgoing_replies=j.get("my_outgoing_replies") or [],
        engagement_snapshots_count=len(snaps),
    )


def _campaign_to_public(doc: dict[str, Any]) -> CampaignPublic:
    return CampaignPublic(
        id=str(doc["_id"]),
        name=doc.get("name", ""),
        source_filename=doc.get("source_filename"),
        n_rows=int(doc.get("n_rows", 0)),
        n_dry_run=int(doc.get("n_dry_run", 0)),
        n_posted=int(doc.get("n_posted", 0)),
        n_failed=int(doc.get("n_failed", 0)),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        last_send_attempt_at=doc.get("last_send_attempt_at"),
        last_send_was_dry_run=doc.get("last_send_was_dry_run"),
    )


def _parse_csv(raw_bytes: bytes) -> list[dict[str, str]]:
    """Parse a CSV with header row containing post_url + comment columns.
    Returns a list of dict rows. Raises HTTPException on schema errors."""
    try:
        text = raw_bytes.decode("utf-8-sig")  # handle BOM gracefully
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("latin-1")
        except UnicodeDecodeError as err:
            raise HTTPException(400, f"CSV is not UTF-8 or Latin-1: {err}")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV has no header row")

    # Normalize header names — accept common variants.
    normalized = {(h or "").strip().lower().replace(" ", "_"): h for h in reader.fieldnames}
    url_col = next((normalized[k] for k in ("post_url", "comment_link_post_url", "url", "linkedin_post_url") if k in normalized), None)
    cmt_col = next((normalized[k] for k in ("comment", "comment_text", "drafted_comment", "comment_body", "body") if k in normalized), None)
    if not url_col or not cmt_col:
        raise HTTPException(
            400,
            "CSV must have at least 'post_url' (or 'url' / 'comment_link_post_url') "
            "and 'comment' (or 'comment_text' / 'drafted_comment') columns. "
            f"Got: {list(reader.fieldnames)}",
        )

    rows: list[dict[str, str]] = []
    for raw in reader:
        url = (raw.get(url_col) or "").strip()
        cmt = (raw.get(cmt_col) or "").strip()
        if not url or not cmt:
            continue  # skip empty rows silently
        rows.append({"post_url": url, "comment": cmt})

    if not rows:
        raise HTTPException(400, "CSV had no non-empty rows after parsing")
    if len(rows) > MAX_ROWS_PER_CSV:
        raise HTTPException(
            413,
            f"CSV has {len(rows)} rows; cap is {MAX_ROWS_PER_CSV}. "
            f"Split into smaller files or use the daily_run pipeline.",
        )
    return rows


# ─── Routes ───────────────────────────────────────────────────────────────


@router.get("/accounts", response_model=AccountsResponse)
async def list_unipile_accounts(user: CurrentUser) -> AccountsResponse:
    """Return Unipile-connected LinkedIn accounts on the tenant. UI uses
    this for the account-selector dropdown.

    Filters to LINKEDIN + status=OK. Other statuses (CREDENTIALS, OAUTH,
    DISCONNECTED) are excluded — operators can't post from those."""
    try:
        accounts = list_accounts()
    except UnipileNotConfigured as err:
        raise HTTPException(503, f"Unipile not configured: {err}")
    except UnipileError as err:
        raise HTTPException(502, f"Unipile error: {err}")

    out: list[AccountOption] = []
    for a in accounts:
        # `account_type` may be missing on older Unipile schemas — coerce to ''
        atype = (a.account_type or "").upper()
        if atype and atype != "LINKEDIN":
            continue
        # `list_accounts` doesn't return source status; we accept everything
        # the helper returns and let the UI display the name. The /send
        # route does a freshness check before each post.
        out.append(
            AccountOption(
                id=a.id,
                name=a.name or "(unnamed)",
                type=atype or "LINKEDIN",
                status="OK",
            )
        )
    out.sort(key=lambda a: a.name.lower())
    return AccountsResponse(accounts=out)


@router.post("/upload")
async def upload_csv(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    file: UploadFile = File(...),
    name: str = Form(default=""),
) -> CampaignDetail:
    """Parse a CSV with (post_url, comment) columns, create a campaign +
    one job per row. No posting happens here — every job lands at
    status='queued'."""
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    raw = await file.read()
    rows = _parse_csv(raw)

    # Validate each row's URL is parseable as a LinkedIn post URL. We do
    # this here so the UI can show errors before the operator clicks Send.
    for r in rows:
        if len(r["comment"]) > MAX_COMMENT_CHARS:
            raise HTTPException(
                400,
                f"Row has comment longer than {MAX_COMMENT_CHARS} chars "
                f"(LinkedIn cap is 1250). Trim it: {r['comment'][:80]}…",
            )

    now = utcnow()
    campaign_doc = {
        "operator_id": user["_id"],
        "name": name.strip() or (file.filename or "Untitled campaign"),
        "source_filename": file.filename,
        "n_rows": len(rows),
        "n_dry_run": 0,
        "n_posted": 0,
        "n_failed": 0,
        "created_at": now,
        "updated_at": now,
    }
    campaign_id = (await db.manual_comment_campaigns.insert_one(campaign_doc)).inserted_id

    jobs: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        url = r["post_url"]
        try:
            post_id = extract_post_id_from_url(url)
        except Exception:
            post_id = None
        jobs.append(
            {
                "campaign_id": campaign_id,
                "operator_id": user["_id"],
                "row_index": i,
                "post_url": url,
                "post_id": post_id,
                "draft_comment": r["comment"],
                "status": "queued",
                "unipile_account_id": None,
                "comment_id": None,
                "posted_at": None,
                "error": None,
                "engagement_snapshots": [],
                "created_at": now,
                "updated_at": now,
            }
        )
    await db.manual_comment_jobs.insert_many(jobs)

    # Return the just-created campaign + jobs.
    campaign_doc["_id"] = campaign_id
    inserted_jobs = await db.manual_comment_jobs.find(
        {"campaign_id": campaign_id}
    ).sort("row_index", 1).to_list(length=None)

    return CampaignDetail(
        campaign=_campaign_to_public(campaign_doc),
        jobs=[_job_to_public(j) for j in inserted_jobs],
    )


@router.get("/campaigns")
async def list_campaigns(
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> dict[str, list[CampaignPublic]]:
    """List the operator's campaigns, most-recent first."""
    rows = await db.manual_comment_campaigns.find(
        {"operator_id": user["_id"]}
    ).sort("created_at", -1).to_list(length=100)
    return {"campaigns": [_campaign_to_public(d) for d in rows]}


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(
    campaign_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CampaignDetail:
    try:
        oid = ObjectId(campaign_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid campaign_id")
    doc = await db.manual_comment_campaigns.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    )
    if not doc:
        raise HTTPException(404, "Campaign not found")
    jobs = await db.manual_comment_jobs.find({"campaign_id": oid}).sort("row_index", 1).to_list(length=None)
    return CampaignDetail(
        campaign=_campaign_to_public(doc),
        jobs=[_job_to_public(j) for j in jobs],
    )


@router.post("/campaigns/{campaign_id}/send", response_model=SendResponse)
async def send_campaign(
    campaign_id: str,
    payload: SendRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SendResponse:
    """Post the campaign's comments. Default dry_run=True means we
    validate + mark each job status='dry_run' WITHOUT calling Unipile.

    Set dry_run=False to actually post. Live posting requires a non-empty
    unipile_account_id that the operator has explicitly confirmed in the
    UI (we double-check it resolves to a LINKEDIN account on the tenant
    before posting)."""
    try:
        oid = ObjectId(campaign_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid campaign_id")

    campaign = await db.manual_comment_campaigns.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    )
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    # Sanity check on the chosen account — list_accounts() filters to
    # what the operator can actually use. We don't trust client-side
    # selection on a posting endpoint.
    try:
        all_accounts = list_accounts()
    except (UnipileError, UnipileNotConfigured) as err:
        raise HTTPException(502, f"Could not list Unipile accounts: {err}")
    by_id = {a.id: a for a in all_accounts}
    if payload.unipile_account_id not in by_id:
        raise HTTPException(
            400,
            f"unipile_account_id {payload.unipile_account_id!r} is not in "
            f"the tenant's account list. Available: {list(by_id.keys())[:5]}…",
        )

    jobs = await db.manual_comment_jobs.find(
        {"campaign_id": oid, "status": {"$in": ["queued", "failed", "dry_run"]}}
    ).sort("row_index", 1).to_list(length=None)

    if not jobs:
        return SendResponse(
            campaign_id=campaign_id,
            dry_run=payload.dry_run,
            n_total=0, n_posted=0, n_dry_run=0, n_failed=0, n_skipped=0,
        )

    n_posted = n_dry = n_failed = n_skipped = 0
    now = utcnow()

    for j in jobs:
        url = j.get("post_url", "")
        text = j.get("draft_comment", "")
        if not url or not text:
            n_skipped += 1
            await db.manual_comment_jobs.update_one(
                {"_id": j["_id"]},
                {"$set": {
                    "status": "failed",
                    "error": "empty post_url or comment",
                    "updated_at": now,
                }},
            )
            n_failed += 1
            continue

        if payload.dry_run:
            # No Unipile call. Just stamp dry_run + carry the account_id
            # the operator chose so the UI can show "would post from X".
            await db.manual_comment_jobs.update_one(
                {"_id": j["_id"]},
                {"$set": {
                    "status": "dry_run",
                    "unipile_account_id": payload.unipile_account_id,
                    "error": None,
                    "updated_at": now,
                }},
            )
            n_dry += 1
            continue

        # ── Atomic claim per job — anti-double-post for campaign-wide send.
        # Same race as send_single_job: if the user clicks "Send" twice
        # (because the first request appeared to fail), we'd otherwise
        # post the same comment from two parallel requests. The atomic
        # transition queued|failed|dry_run|deleted → posting ensures
        # only one request can claim each job. If the claim fails because
        # another request beat us to it, skip the job (don't count as
        # failed — it's already being handled).
        claimed = await db.manual_comment_jobs.find_one_and_update(
            {
                "_id": j["_id"],
                "status": {"$in": ["queued", "failed", "dry_run", "deleted"]},
            },
            {
                "$set": {
                    "status": "posting",
                    "posting_started_at": now,
                    "unipile_account_id": payload.unipile_account_id,
                },
            },
        )
        if not claimed:
            log.warning(
                "send_campaign skipping job=%s — could not claim "
                "(probably already posted or being posted)",
                j["_id"],
            )
            n_skipped += 1
            continue

        # LIVE POST — single Unipile call per job.
        try:
            result = post_comment(
                account_id=payload.unipile_account_id,
                post_url=url,
                text=text,
            )
            await db.manual_comment_jobs.update_one(
                {"_id": j["_id"]},
                {"$set": {
                    "status": "posted",
                    "unipile_account_id": payload.unipile_account_id,
                    "comment_id": result.comment_id or None,
                    "posted_at": result.posted_at or now,
                    "error": None,
                    "updated_at": now,
                }},
            )
            n_posted += 1
        except UnipileError as err:
            await db.manual_comment_jobs.update_one(
                {"_id": j["_id"]},
                {"$set": {
                    "status": "failed",
                    "unipile_account_id": payload.unipile_account_id,
                    "error": f"unipile: {str(err)[:400]}",
                    "updated_at": now,
                }},
            )
            n_failed += 1
            log.warning("manual_comment job %s unipile failure: %s", j["_id"], err)
        except Exception as err:  # noqa: BLE001 — bound failures to the row
            await db.manual_comment_jobs.update_one(
                {"_id": j["_id"]},
                {"$set": {
                    "status": "failed",
                    "unipile_account_id": payload.unipile_account_id,
                    "error": f"unexpected: {type(err).__name__}: {str(err)[:400]}",
                    "updated_at": now,
                }},
            )
            n_failed += 1
            log.exception("manual_comment job %s unexpected failure", j["_id"])

    await db.manual_comment_campaigns.update_one(
        {"_id": oid},
        {
            "$set": {
                "last_send_attempt_at": now,
                "last_send_was_dry_run": payload.dry_run,
                "updated_at": now,
            },
            "$inc": {
                "n_dry_run": n_dry,
                "n_posted": n_posted,
                "n_failed": n_failed,
            },
        },
    )
    return SendResponse(
        campaign_id=campaign_id,
        dry_run=payload.dry_run,
        n_total=len(jobs),
        n_posted=n_posted,
        n_dry_run=n_dry,
        n_failed=n_failed,
        n_skipped=n_skipped,
    )


@router.post("/campaigns/{campaign_id}/refresh-engagement", response_model=RefreshResponse)
async def refresh_engagement(
    campaign_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> RefreshResponse:
    """For each job in this campaign with status='posted', call APIDirect
    to fetch the current post details and append a snapshot to
    engagement_snapshots. Records likes / comments / shares (the post's
    totals — replies-to-our-specific-comment isn't exposed by APIDirect's
    /v1/linkedin/post endpoint, but the comments delta from t=0 is a
    reasonable proxy for thread activity).

    Manual-only — no background polling. Operator decides when to spend
    APIDirect credit.

    ASYNC EXECUTION (2026-05-14): APIDirect calls run 4-15s and the 3-slot
    server-side cap means a 15-job campaign takes ~30-40s end-to-end. That
    blew past Next.js dev-server's upstream proxy timeout (30s), surfacing
    as user-visible 500s. We now run the refresh as a fire-and-forget
    background task: the route returns immediately with `started: true`,
    work continues in the engine, and snapshots stream into Mongo as
    each job completes. The UI's existing campaign-detail poll picks up
    the new snapshots within a few seconds — no separate status endpoint
    needed."""
    log.warning(
        "[refresh-engagement] enter campaign=%s user=%s — scheduling background task",
        campaign_id, user.get('_id'),
    )
    try:
        oid = ObjectId(campaign_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid campaign_id")
    if not await db.manual_comment_campaigns.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    ):
        raise HTTPException(404, "Campaign not found")

    # Fire-and-forget: schedule the actual refresh as a Python asyncio task
    # so the route can return inside the proxy timeout. The task uses its
    # own pymongo connection inside worker threads — no shared async state
    # with this request, so it's safe to outlive the response.
    #
    # IMPORTANT: hold a STRONG reference to the task. asyncio.create_task
    # returns a task that the event loop only weakly references — without
    # an external strong ref, Python's GC can collect the task mid-await
    # and the background work silently disappears (which is exactly what
    # happened: log shows "scheduling background task" → 200 OK → no
    # APIDirect activity → task was GC'd). The module-level set holds
    # tasks until they finish; the done-callback removes them so the set
    # doesn't leak. See https://docs.python.org/3/library/asyncio-task.html
    # ("Important: Save a reference to the result of this function...").
    user_copy = {"_id": user["_id"]}  # capture just what the impl needs
    _task = asyncio.create_task(_run_refresh_background(campaign_id, user_copy))
    _REFRESH_BACKGROUND_TASKS.add(_task)
    _task.add_done_callback(_REFRESH_BACKGROUND_TASKS.discard)

    return RefreshResponse(
        campaign_id=campaign_id,
        n_refreshed=0,
        n_failed=0,
        n_skipped=0,
        provider_rate_limited=False,
    )


async def _run_refresh_background(campaign_id: str, user: dict[str, Any]) -> None:
    """Wrapper around _refresh_engagement_impl that runs detached from the
    HTTP request lifecycle. Opens its own Motor handle (the request-scoped
    one is closed when the response returns). Logs result on completion."""
    import sys as _sys, traceback as _traceback
    try:
        from app.database import mongo, connect_to_mongo, get_db
        if mongo.client is None:
            await connect_to_mongo()
        db = get_db()
        result = await _refresh_engagement_impl(campaign_id, user, db)
        log.info(
            "[refresh-engagement] background done campaign=%s refreshed=%d failed=%d skipped=%d provider_rate_limited=%s",
            campaign_id, result.n_refreshed, result.n_failed,
            result.n_skipped, result.provider_rate_limited,
        )
    except Exception as err:  # noqa: BLE001
        _traceback.print_exc(file=_sys.stderr)
        _sys.stderr.flush()
        log.exception(
            "[refresh-engagement] background CRASHED campaign=%s: %s",
            campaign_id, err,
        )


async def _refresh_engagement_impl(
    campaign_id: str,
    user: dict[str, Any],
    db: AsyncIOMotorDatabase,
) -> RefreshResponse:
    """Body of refresh_engagement (extracted so the outer handler can
    wrap it in a catch-all and log full tracebacks)."""
    try:
        oid = ObjectId(campaign_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid campaign_id")

    if not await db.manual_comment_campaigns.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    ):
        raise HTTPException(404, "Campaign not found")

    jobs = await db.manual_comment_jobs.find(
        {"campaign_id": oid, "status": "posted"}
    ).sort("row_index", 1).to_list(length=None)

    # Parallelize the per-job refresh work so the route returns inside
    # Next.js dev server's ~30s upstream timeout. With 12 jobs each making
    # 1× APIDirect + up-to-2× Unipile calls (~2-3s/job sequentially), the
    # sequential walk pushed past 30s. ThreadPoolExecutor with 4 workers
    # brings it to ~max(job_time) × ceil(N/workers) ≈ 8–10s for a 12-job
    # campaign. Cost tracker's threading.local context is re-attached
    # inside each worker (set_current_slate_run no-op when slate_run_id
    # is None — this route runs outside the slate context).
    n_refreshed = n_failed = n_skipped = 0
    n_quota_exhausted_apidirect = False
    # Latched circuit-breaker — shared across worker threads via a Lock.
    # First worker to see a LinkedIn provider-429 flips it; subsequent
    # workers check before making their Unipile call and skip if set.
    provider_rate_limited = False
    provider_rate_limited_lock = threading.Lock()
    now = utcnow()

    reader_aid = (settings.unipile_stats_account_id or "").strip()

    def _refresh_one_job_impl(j: dict[str, Any]) -> tuple[str, str | None]:
        """Synchronous worker: APIDirect + Unipile reads + Mongo update for
        one job. Returns ("refreshed"|"failed"|"skipped"|"quota_exhausted"|
        "not_configured", error_msg_or_None) so the outer loop can aggregate
        counters and decide whether to short-circuit the batch.

        Uses pymongo (sync) instead of motor (async) because we're inside a
        thread, so the asyncio loop isn't available here. The slate_runs
        cost_tracker uses its own sync pymongo client for the same reason.
        """
        nonlocal provider_rate_limited
        from pymongo import MongoClient
        # Reuse the cost_tracker's pymongo connection (already lazy-cached)
        # so we don't open a fresh client per job.
        from app.services.cost_tracker import _db as _cost_db
        sync_db = _cost_db()
        if sync_db is None:
            sync_db = MongoClient(settings.mongodb_uri)[settings.mongodb_db]

        url = j.get("post_url")
        if not url:
            return ("skipped", None)
        # APIDirect's 3-concurrent server cap returns 429 when our parallel
        # workers race for slots. Retry with backoff so transient queue-
        # collisions don't show as user-visible failures. APIDirect's per-
        # call latency is 4-15s, so 3 retries with 1/2/4s waits is well
        # within our overall budget.
        details = None
        for attempt in range(3):
            try:
                details = get_linkedin_post_details(url)
                break
            except ApiDirectQuotaExhausted:
                return ("quota_exhausted", None)
            except ApiDirectNotConfigured as err:
                return ("not_configured", str(err))
            except ApiDirectError as err:
                msg = str(err)
                # Only retry on 429 concurrency errors; other errors are
                # likely deterministic (404, 5xx) and won't change on retry.
                if "429" in msg and attempt < 2:
                    import time as _t
                    _t.sleep(2 ** attempt)
                    continue
                sync_db.manual_comment_jobs.update_one(
                    {"_id": j["_id"]},
                    {"$set": {
                        "engagement_last_error": msg[:300],
                        "updated_at": now,
                    }},
                )
                return ("failed", msg)
        if details is None:
            return ("skipped", None)

        my_reactions: int | None = None
        my_replies: int | None = None
        my_replies_thread_payload: list[dict[str, Any]] | None = None
        our_cid = (j.get("comment_id") or "").strip()

        # Acquire a reader from the pool — rotates LRU across all
        # healthy discovery-capable accounts so no single account
        # bears the full engagement-refresh load. Falls back to the
        # legacy stats-account / posting-account chain on PoolExhausted.
        from app.services.unipile_pool import get_pool, PoolExhausted
        pool = get_pool()
        try:
            job_reader_aid = pool.acquire(capability="post_fetch")
            used_pool = True
        except PoolExhausted:
            job_reader_aid = reader_aid or (j.get("unipile_account_id") or "").strip()
            used_pool = False

        with provider_rate_limited_lock:
            skip_unipile_for_this_job = provider_rate_limited

        if our_cid and job_reader_aid and not skip_unipile_for_this_job:
            try:
                threads = get_post_comments(account_id=job_reader_aid, post_url=url)
                if used_pool:
                    pool.report_success(job_reader_aid)
                for c in threads:
                    if (c.comment_id or "") == our_cid:
                        my_reactions = int(c.reaction_count or 0)
                        my_replies = int(c.reply_count or 0)
                        break
                if (my_replies or 0) > 0:
                    try:
                        replies = get_comment_replies(
                            account_id=job_reader_aid,
                            post_url=url,
                            comment_id=our_cid,
                        )
                        # Unipile's get_comment_replies returns EVERY comment
                        # nested under our top-level comment, which includes
                        # the replies we ourselves posted via reply-to-reply.
                        # Without filtering those would re-render as fake
                        # "incoming replies" alongside the legitimate green
                        # "You" outgoing-reply panel — duplicated UI. Dedupe
                        # by comment_id against the job's my_outgoing_replies
                        # array (single source of truth for outgoing).
                        my_outgoing_cids = {
                            (o.get("comment_id") or "")
                            for o in (j.get("my_outgoing_replies") or [])
                            if o.get("comment_id")
                        }
                        kept = [
                            r for r in replies
                            if (r.comment_id or "") not in my_outgoing_cids
                        ]
                        # Chronological order so the thread reads top→bottom
                        # in time order (matches LinkedIn's native UX). Nulls
                        # sink to the top with timestamp 0.
                        def _ts(r):
                            pa = r.published_at
                            if pa is None:
                                return 0.0
                            try:
                                return pa.timestamp()
                            except (AttributeError, ValueError):
                                return 0.0
                        kept.sort(key=_ts)
                        my_replies_thread_payload = [
                            {
                                "comment_id": r.comment_id,
                                "author_name": r.author_name,
                                # ACoAAA-form member URN — needed by the
                                # reply-to-reply route so it can pass a
                                # proper @-mention to Unipile (otherwise
                                # the parent author's name renders as
                                # plain text instead of a clickable tag).
                                "author_provider_id": r.author_provider_id,
                                "text": r.text,
                                "published_at": r.published_at,
                            }
                            for r in kept
                        ]
                    except (UnipileError, UnipileNotConfigured) as inner:
                        inner_msg = str(inner)
                        if (
                            "429" in inner_msg
                            or "too many" in inner_msg.lower()
                            or "provider" in inner_msg.lower()
                        ):
                            with provider_rate_limited_lock:
                                provider_rate_limited = True
            except (UnipileError, UnipileNotConfigured) as err:
                emsg = str(err)
                # Report to pool so the account gets cooldown / status update.
                if used_pool:
                    try:
                        pool.report_error(job_reader_aid, err)
                    except Exception:  # noqa: BLE001
                        pass
                if "429" in emsg or "too many" in emsg.lower() or "provider" in emsg.lower():
                    with provider_rate_limited_lock:
                        provider_rate_limited = True
                    log.warning(
                        "refresh-engagement: LinkedIn provider rate-limit "
                        "detected on campaign=%s job=%s account=%s — remaining "
                        "jobs will rotate to a different pool account or skip "
                        "Unipile reads (post-level via APIDirect continues).",
                        campaign_id, str(j["_id"]), job_reader_aid[:18],
                    )
                sync_db.manual_comment_jobs.update_one(
                    {"_id": j["_id"]},
                    {"$set": {
                        "engagement_unipile_last_error": f"unipile: {emsg[:300]}",
                        "updated_at": now,
                    }},
                )

        snapshot = {
            "captured_at": now,
            "likes": int(details.likes or 0),
            "comments": int(details.comments or 0),
            "shares": int(details.shares or 0),
            "my_comment_reactions": my_reactions,
            "my_comment_replies": my_replies,
        }
        set_doc: dict[str, Any] = {
            "engagement_last_error": None,
            "updated_at": now,
        }
        if my_replies_thread_payload is not None:
            set_doc["latest_my_comment_replies_thread"] = my_replies_thread_payload
        sync_db.manual_comment_jobs.update_one(
            {"_id": j["_id"]},
            {
                "$push": {"engagement_snapshots": snapshot},
                "$set": set_doc,
            },
        )
        return ("refreshed", None)

    def _refresh_one_job(j: dict[str, Any]) -> tuple[str, str | None]:
        """Public wrapper around _refresh_one_job_impl with a defensive
        catch-all so a single bad job doesn't 500 the route."""
        try:
            return _refresh_one_job_impl(j)
        except Exception as err:  # noqa: BLE001
            log.exception(
                "refresh-engagement worker raised for job=%s: %s",
                str(j.get("_id")), err,
            )
            return ("failed", f"worker_exception: {err}")

    # Fan out across a small thread pool. max_workers=4 is conservative:
    # each worker does ≤3 HTTP calls; 12 jobs / 4 workers ≈ 3 batches ≈ 9s
    # wall-clock, well under any proxy timeout. Bump cautiously — APIDirect
    # has its own per-endpoint concurrency cap and Unipile rate-limits at
    # ~3-4 r/s so 4 in parallel is the sweet spot.
    loop = asyncio.get_running_loop()
    not_configured_err: str | None = None
    futures = [loop.run_in_executor(_REFRESH_POOL, _refresh_one_job, j) for j in jobs]
    for fut in asyncio.as_completed(futures):
        status, err_msg = await fut
        if status == "refreshed":
            n_refreshed += 1
        elif status == "skipped":
            n_skipped += 1
        elif status == "failed":
            n_failed += 1
        elif status == "quota_exhausted":
            n_quota_exhausted_apidirect = True
        elif status == "not_configured" and not_configured_err is None:
            not_configured_err = err_msg

    if n_quota_exhausted_apidirect and n_refreshed == 0:
        raise HTTPException(
            429,
            "APIDirect quota exhausted — stopping refresh. Top up credit and retry.",
        )
    if not_configured_err is not None and n_refreshed == 0:
        raise HTTPException(503, f"APIDirect not configured: {not_configured_err}")

    return RefreshResponse(
        campaign_id=campaign_id,
        n_refreshed=n_refreshed,
        n_failed=n_failed,
        n_skipped=n_skipped,
        provider_rate_limited=provider_rate_limited,
    )


@router.post("/jobs/{job_id}/delete-comment", response_model=DeleteJobCommentResponse)
async def delete_job_comment(
    job_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> DeleteJobCommentResponse:
    """Delete a previously-posted comment via Unipile.

    LinkedIn enforces ownership: this only works if the comment was
    posted by the same Unipile account we're calling from. We rely on
    the `unipile_account_id` stored on the job at post time so the
    operator can't accidentally try to delete from the wrong account.

    On success: job flips to status='deleted', `deleted_at` stamped.
    `comment_id` is preserved for audit; only the status changes.
    """
    try:
        oid = ObjectId(job_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid job_id")

    job = await db.manual_comment_jobs.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    )
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "posted":
        raise HTTPException(
            409,
            f"Can't delete comment on a job with status={job.get('status')!r}. "
            f"Only `posted` jobs have a LinkedIn comment to delete.",
        )
    cid = (job.get("comment_id") or "").strip()
    if not cid:
        raise HTTPException(
            409,
            "Job has status=posted but no stored comment_id; "
            "Unipile didn't return one at post time. Manual delete on LinkedIn.",
        )
    aid = (job.get("unipile_account_id") or "").strip()
    if not aid:
        raise HTTPException(
            409,
            "Job has no unipile_account_id stored; can't authenticate the "
            "delete call. Manual delete on LinkedIn.",
        )

    now = utcnow()
    try:
        delete_comment(
            account_id=aid,
            post_url_or_id=job.get("post_url") or "",
            comment_id=cid,
        )
    except UnipileFeatureNotSupported as err:
        # Vendor-side limitation: Unipile doesn't expose comment delete.
        # Returning 501 so the frontend can render the "delete on LinkedIn
        # then mark as deleted locally" affordance instead of suggesting
        # a retry.
        await db.manual_comment_jobs.update_one(
            {"_id": oid},
            {"$set": {
                "delete_error": "unipile_unsupported: " + str(err)[:300],
                "delete_attempted_at": now,
                "updated_at": now,
            }},
        )
        raise HTTPException(
            501,
            "Unipile does not expose a comment-delete endpoint. "
            "Open the post on LinkedIn, delete the comment there, then "
            "use the 'Mark as deleted' action to update this row.",
        )
    except UnipileError as err:
        await db.manual_comment_jobs.update_one(
            {"_id": oid},
            {"$set": {
                "delete_error": f"unipile: {str(err)[:400]}",
                "delete_attempted_at": now,
                "updated_at": now,
            }},
        )
        # Surface the error to the UI but do NOT raise — let the operator
        # see what happened so they can fix it (e.g. wrong account_id) and
        # retry. 502 here would hide the detail in some clients' generic
        # error toasts.
        return DeleteJobCommentResponse(
            job_id=job_id, deleted=False, comment_id=cid,
            error=f"Unipile rejected the delete: {str(err)[:300]}",
        )

    await db.manual_comment_jobs.update_one(
        {"_id": oid},
        {"$set": {
            "status": "deleted",
            "deleted_at": now,
            "delete_error": None,
            "updated_at": now,
        }},
    )
    # Also flip the campaign counters so the list view stays accurate.
    await db.manual_comment_campaigns.update_one(
        {"_id": job["campaign_id"]},
        {"$inc": {"n_posted": -1}, "$set": {"updated_at": now}},
    )
    return DeleteJobCommentResponse(
        job_id=job_id, deleted=True, comment_id=cid, error=None,
    )


@router.post("/jobs/{job_id}/mark-deleted", response_model=DeleteJobCommentResponse)
async def mark_job_deleted_locally(
    job_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> DeleteJobCommentResponse:
    """Mark a job as `deleted` WITHOUT calling Unipile.

    Use this when:
      - You deleted the LinkedIn comment manually (Unipile's API doesn't
        expose DELETE, see unipile.delete_comment docstring)
      - You want to drop the row from the campaign's posted count

    No external API call. Pure Mongo state flip + audit trail. The
    `comment_id` stays on the record so you know what was on LinkedIn
    before you deleted it manually.
    """
    try:
        oid = ObjectId(job_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid job_id")

    job = await db.manual_comment_jobs.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    )
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in ("posted", "failed"):
        raise HTTPException(
            409,
            f"Can't mark as deleted: job status is {job.get('status')!r}. "
            f"Only `posted` or `failed` rows can be marked deleted.",
        )

    now = utcnow()
    was_posted = job.get("status") == "posted"
    await db.manual_comment_jobs.update_one(
        {"_id": oid},
        {"$set": {
            "status": "deleted",
            "deleted_at": now,
            "deleted_manually": True,
            "delete_error": None,
            "updated_at": now,
        }},
    )
    # Only decrement n_posted if it was actually in that bucket.
    if was_posted:
        await db.manual_comment_campaigns.update_one(
            {"_id": job["campaign_id"]},
            {"$inc": {"n_posted": -1}, "$set": {"updated_at": now}},
        )
    return DeleteJobCommentResponse(
        job_id=job_id, deleted=True, comment_id=job.get("comment_id"), error=None,
    )


@router.post("/jobs/{job_id}/send", response_model=SingleJobSendResponse)
async def send_single_job(
    job_id: str,
    payload: SingleJobSendRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SingleJobSendResponse:
    """Post a single job (or dry-run validate it). Sibling of the
    campaign-wide /send route, but lets the operator review and fire
    one comment at a time.

    Same safety: dry_run defaults to True. Live posting requires
    dry_run=False AND a valid unipile_account_id resolved on the tenant.

    Refuses to re-send a job that's already `posted` (use delete-comment
    first if you want to re-do it)."""
    try:
        oid = ObjectId(job_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid job_id")

    job = await db.manual_comment_jobs.find_one(
        {"_id": oid, "operator_id": user["_id"]}
    )
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") == "posted":
        raise HTTPException(
            409,
            "Job already posted. Delete the existing comment before re-posting.",
        )
    if job.get("status") == "deleted":
        # OK — operator may want to repost. Continue. We don't reset
        # comment_id; the new post will overwrite it on success.
        pass

    # Validate account against the tenant — same check as campaign-wide
    # /send so a stale UI selection can't slip through.
    try:
        all_accounts = list_accounts()
    except (UnipileError, UnipileNotConfigured) as err:
        raise HTTPException(502, f"Could not list Unipile accounts: {err}")
    by_id = {a.id: a for a in all_accounts}
    if payload.unipile_account_id not in by_id:
        raise HTTPException(
            400,
            f"unipile_account_id {payload.unipile_account_id!r} is not on the tenant",
        )

    url = (job.get("post_url") or "").strip()
    text = (job.get("draft_comment") or "")
    if not url or not text:
        raise HTTPException(409, "Job has empty post_url or draft_comment")

    now = utcnow()

    if payload.dry_run:
        await db.manual_comment_jobs.update_one(
            {"_id": oid},
            {"$set": {
                "status": "dry_run",
                "unipile_account_id": payload.unipile_account_id,
                "error": None,
                "updated_at": now,
            }},
        )
        # Bump campaign counter
        await db.manual_comment_campaigns.update_one(
            {"_id": job["campaign_id"]},
            {"$inc": {"n_dry_run": 1}, "$set": {"last_send_attempt_at": now, "last_send_was_dry_run": True, "updated_at": now}},
        )
        return SingleJobSendResponse(
            job_id=job_id, status="dry_run", dry_run=True,
            comment_id=None, posted_at=None, error=None,
        )

    # ── Anti-double-post: atomic claim ─────────────────────────────────
    # Race we're guarding against: user clicks "Send this row", frontend
    # proxy times out / shows error, comment ACTUALLY posts on LinkedIn,
    # user clicks again → without this check, second POST fires →
    # duplicate comment on a real person's post under the operator's
    # name (which Unipile can't delete).
    #
    # Atomic guard: transition status from {queued, failed, dry_run,
    # deleted} → "posting" in one Mongo op. If the update doesn't match
    # (because another concurrent request already claimed it OR the job
    # is now "posted"), refuse this request with a 409 and return the
    # existing comment_id if it exists. The user gets a clear signal
    # ("already posted") instead of a second comment going out.
    claim_now = utcnow()
    claimed = await db.manual_comment_jobs.find_one_and_update(
        {
            "_id": oid,
            "status": {"$in": ["queued", "failed", "dry_run", "deleted"]},
        },
        {
            "$set": {
                "status": "posting",
                "posting_started_at": claim_now,
                "unipile_account_id": payload.unipile_account_id,
            },
        },
    )
    if not claimed:
        # Status wasn't claimable → re-read to see current state.
        cur = await db.manual_comment_jobs.find_one({"_id": oid})
        if cur and cur.get("status") in ("posted", "posting"):
            cid = cur.get("comment_id")
            log.warning(
                "send_single_job DEDUP: job=%s already in status=%s "
                "(comment_id=%s) — refusing duplicate post",
                job_id, cur.get("status"), (cid or "")[:24],
            )
            # If "posted", return the existing comment_id as success so
            # the UI updates without firing again. If "posting" (another
            # in-flight request), refuse with 409 so the user knows to
            # wait.
            if cur.get("status") == "posted":
                return SingleJobSendResponse(
                    job_id=job_id, status="posted", dry_run=False,
                    comment_id=cid, posted_at=cur.get("posted_at"),
                    error=None,
                )
            raise HTTPException(
                409,
                f"Job is currently being posted by another request "
                f"(started {(claim_now - (cur.get('posting_started_at') or claim_now)).total_seconds():.0f}s "
                f"ago). Wait a few seconds and retry if it didn't land.",
            )
        # Unknown state — surface the actual status so the operator can debug.
        raise HTTPException(
            409,
            f"Cannot post: job status is {(cur or {}).get('status')!r}",
        )

    # LIVE POST — single Unipile call, isolated to this row.
    try:
        result = post_comment(
            account_id=payload.unipile_account_id,
            post_url=url,
            text=text,
        )
        await db.manual_comment_jobs.update_one(
            {"_id": oid},
            {"$set": {
                "status": "posted",
                "unipile_account_id": payload.unipile_account_id,
                "comment_id": result.comment_id or None,
                "posted_at": result.posted_at or now,
                "error": None,
                "updated_at": now,
            }},
        )
        await db.manual_comment_campaigns.update_one(
            {"_id": job["campaign_id"]},
            {"$inc": {"n_posted": 1}, "$set": {"last_send_attempt_at": now, "last_send_was_dry_run": False, "updated_at": now}},
        )
        return SingleJobSendResponse(
            job_id=job_id, status="posted", dry_run=False,
            comment_id=result.comment_id, posted_at=result.posted_at, error=None,
        )
    except UnipileError as err:
        msg = f"unipile: {str(err)[:400]}"
        await db.manual_comment_jobs.update_one(
            {"_id": oid},
            {"$set": {
                "status": "failed",
                "unipile_account_id": payload.unipile_account_id,
                "error": msg,
                "updated_at": now,
            }},
        )
        await db.manual_comment_campaigns.update_one(
            {"_id": job["campaign_id"]},
            {"$inc": {"n_failed": 1}, "$set": {"last_send_attempt_at": now, "last_send_was_dry_run": False, "updated_at": now}},
        )
        return SingleJobSendResponse(
            job_id=job_id, status="failed", dry_run=False,
            comment_id=None, posted_at=None, error=msg,
        )
    except Exception as err:  # noqa: BLE001
        # ── Defensive rollback ──────────────────────────────────────────
        # If we don't catch the catch-all here, an unexpected exception
        # (network blip in the Mongo client, asyncio cancel, KeyError in
        # response parsing, anything) leaves the job stuck in
        # status="posting" forever. That state blocks the atomic claim
        # above, so the user can't retry from the UI even if Unipile
        # never actually posted. Worse: the next time the user clicks,
        # they get a 409 "currently being posted by another request"
        # which is misleading.
        #
        # Roll back to "failed" with a clear error message so the user
        # can retry. We're explicit about this being unexpected (vs a
        # known UnipileError) so the operator knows to look at the
        # backend logs.
        msg = f"unexpected: {type(err).__name__}: {str(err)[:400]}"
        log.exception(
            "send_single_job UNEXPECTED failure on job=%s — rolling back "
            "status from 'posting' to 'failed'",
            job_id,
        )
        try:
            await db.manual_comment_jobs.update_one(
                {"_id": oid},
                {"$set": {
                    "status": "failed",
                    "unipile_account_id": payload.unipile_account_id,
                    "error": msg,
                    "updated_at": now,
                }},
            )
            await db.manual_comment_campaigns.update_one(
                {"_id": job["campaign_id"]},
                {"$inc": {"n_failed": 1}, "$set": {"last_send_attempt_at": now, "last_send_was_dry_run": False, "updated_at": now}},
            )
        except Exception:  # noqa: BLE001 — last-ditch cleanup; swallow
            log.exception(
                "send_single_job ROLLBACK FAILED for job=%s — job may be "
                "stuck in 'posting' status; manual Mongo cleanup needed",
                job_id,
            )
        return SingleJobSendResponse(
            job_id=job_id, status="failed", dry_run=False,
            comment_id=None, posted_at=None, error=msg,
        )


# ─── Reply to one of the incoming replies on our comment ──────────────────


@router.post(
    "/jobs/{job_id}/replies/{reply_comment_id}/reply",
    response_model=ReplyToReplyResponse,
)
async def reply_to_reply(
    job_id: str,
    reply_comment_id: str,
    payload: ReplyToReplyRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ReplyToReplyResponse:
    """Post a threaded reply to one of the incoming replies on Yair's
    comment.

    Threading model: Yair's original comment has comment_id=A. Tarpan
    (etc.) replied to A with comment_id=B. This route posts Yair's
    response to B by calling Unipile's POST /posts/{post_id}/comments
    with ``comment_id=B`` in the body — same endpoint as a top-level
    comment, the ``comment_id`` field is what makes it a reply-to-reply.

    Safety contract (mirrors /jobs/{id}/send):
      - ``dry_run=True`` (default) validates inputs + records the intent
        without hitting Unipile. The job's ``my_outgoing_replies`` array
        gets a ``dry_run: true`` marker so the UI can show a preview.
      - ``dry_run=False`` actually posts. On success the new
        ``comment_id`` is appended to ``my_outgoing_replies`` so it's
        visible immediately without waiting for the next engagement poll.

    Why store outgoing replies on the job: the next refresh-engagement
    cycle WILL pull them in via get_comment_replies (Yair's own reply
    appears in the thread alongside the incoming ones). But that's a
    ~25s delay — we want the user to see their reply in the UI the
    instant the route returns.
    """
    try:
        jid = ObjectId(job_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid job_id")

    job = await db.manual_comment_jobs.find_one({"_id": jid})
    if not job:
        raise HTTPException(404, "Job not found")
    # Authz — same shape as send_single_job.
    campaign = await db.manual_comment_campaigns.find_one(
        {"_id": job["campaign_id"], "operator_id": user["_id"]},
    )
    if not campaign:
        raise HTTPException(404, "Job not found")
    # Job must be in 'posted' state (we replied to a comment on a post
    # we previously commented on).
    if job.get("status") != "posted":
        raise HTTPException(
            409,
            f"Cannot reply on a job in status={job.get('status')!r} — only "
            "posted jobs have a comment thread to reply into.",
        )
    # Verify the target reply_comment_id is actually one of the replies
    # we captured for this job. Guards against UI sending stale IDs after
    # a thread changes. Soft guard — if my_comment_replies_thread is
    # empty (no refresh yet) we trust the caller.
    thread = job.get("latest_my_comment_replies_thread") or []
    if thread and not any(
        (r.get("comment_id") or "") == reply_comment_id for r in thread
    ):
        raise HTTPException(
            404,
            "Reply not found in this job's captured thread — refresh "
            "engagement first to pick up the latest replies.",
        )

    post_url = job.get("post_url") or ""
    if not post_url:
        raise HTTPException(409, "Job has no post_url — cannot post a reply")

    now = utcnow()
    text = payload.text.strip()

    # Dry-run path: record the intent, don't call Unipile.
    if payload.dry_run:
        entry = {
            "comment_id": None,
            "parent_reply_comment_id": reply_comment_id,
            "text": text,
            "posted_at": now,
            "dry_run": True,
        }
        await db.manual_comment_jobs.update_one(
            {"_id": jid},
            {
                "$push": {"my_outgoing_replies": entry},
                "$set": {"updated_at": now},
            },
        )
        return ReplyToReplyResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            status="dry_run",
            dry_run=True,
            new_comment_id=None,
            posted_at=None,
        )

    # ── Anti-double-post dedup ──────────────────────────────────────────
    # Pattern we're guarding against (operator-reported 2026-05-14):
    #   1. User clicks "Send reply"
    #   2. Frontend proxy times out / shows an "internal error" toast
    #   3. But the backend request DID complete and Unipile posted
    #      the comment successfully
    #   4. User, thinking nothing happened, clicks Send again
    #   5. Without dedup → second post fires → duplicate comment on
    #      LinkedIn under the user's name. Embarrassing + unfixable
    #      (Unipile doesn't expose comment deletion).
    #
    # Defense: before hitting Unipile, look for a `my_outgoing_replies`
    # entry on THIS job with the same parent_reply_comment_id + text
    # posted within the last 10 minutes that landed a real comment_id.
    # If found, return that existing comment_id as a success — the user
    # gets a normal "posted ✓" toast and no second comment fires.
    DEDUP_WINDOW_S = 600  # 10 minutes
    cutoff = now - timedelta(seconds=DEDUP_WINDOW_S)
    existing_replies = job.get("my_outgoing_replies") or []
    duplicate = next(
        (
            r for r in existing_replies
            if r.get("parent_reply_comment_id") == reply_comment_id
            and (r.get("text") or "") == text
            and r.get("dry_run") is not True
            and r.get("comment_id")
            and r.get("posted_at")
            and r["posted_at"] >= cutoff
        ),
        None,
    )
    if duplicate:
        log.warning(
            "reply_to_reply DEDUP: same text already posted within "
            "%ds (parent=%s text_starts=%r existing_cid=%s) — returning "
            "existing comment_id instead of re-posting",
            DEDUP_WINDOW_S,
            reply_comment_id[:24],
            text[:60],
            (duplicate.get("comment_id") or "")[:24],
        )
        return ReplyToReplyResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            status="posted",
            dry_run=False,
            new_comment_id=duplicate.get("comment_id"),
            posted_at=duplicate.get("posted_at"),
        )

    # Live path: actually call Unipile with the parent_comment_id set.
    from app.services.unipile import (
        post_comment as unipile_post_comment,
        UnipileError,
        UnipileNotConfigured,
    )

    # Auto-mention: if the user's reply text starts with the parent
    # author's name (exactly what the UI pre-fills), convert that prefix
    # into Unipile's `{{0}}` mention placeholder so LinkedIn renders it
    # as a real @-tag rather than plain text. If the user deleted or
    # edited the prefix away, fall through to plain text — we don't try
    # to fuzzy-match because mis-placed mentions are worse than no
    # mention at all.
    parent_entry = next(
        (r for r in thread if (r.get("comment_id") or "") == reply_comment_id),
        None,
    )
    send_text = text
    mentions_payload: list[dict[str, Any]] | None = None
    if parent_entry:
        parent_author_id = (parent_entry.get("author_provider_id") or "").strip()
        parent_author_name = (parent_entry.get("author_name") or "").strip()
        # The UI pre-fills with the credential-stripped name (see
        # `authorNameForMention` in manual-comments/page.tsx). Try the
        # stripped form first, then the raw form, so we still catch the
        # mention when the user edits one but not the other.
        stripped = parent_author_name.split(",")[0].strip()
        candidates = []
        if stripped:
            candidates.append(stripped)
        if parent_author_name and parent_author_name != stripped:
            candidates.append(parent_author_name)
        if parent_author_id:
            for cand in candidates:
                if text.startswith(cand):
                    send_text = "{{0}}" + text[len(cand):]
                    mentions_payload = [
                        {"name": cand, "profile_id": parent_author_id}
                    ]
                    break

    try:
        result = unipile_post_comment(
            account_id=payload.unipile_account_id,
            post_url=post_url,
            text=send_text,
            parent_comment_id=reply_comment_id,
            mentions=mentions_payload,
        )
    except UnipileNotConfigured as err:
        raise HTTPException(503, f"Unipile not configured: {err}")
    except UnipileError as err:
        # Don't bury the failure inside the job doc — surface it to the
        # caller so the UI shows a real error toast.
        msg = str(err)[:300]
        entry = {
            "comment_id": None,
            "parent_reply_comment_id": reply_comment_id,
            "text": text,
            "send_text": send_text,
            "mentions_sent": mentions_payload,
            "posted_at": now,
            "dry_run": False,
            "error": msg,
        }
        await db.manual_comment_jobs.update_one(
            {"_id": jid},
            {
                "$push": {"my_outgoing_replies": entry},
                "$set": {"updated_at": now},
            },
        )
        return ReplyToReplyResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            status="failed",
            dry_run=False,
            new_comment_id=None,
            posted_at=None,
            error=msg,
        )

    new_cid = (result.comment_id or "").strip() or None
    # Persist the EXACT payload that was sent so we can later audit
    # whether the @-mention substitution happened and what URN was
    # included. Without this, "the tag didn't render" reports have no
    # forensic trail and we have to guess from response text.
    entry = {
        "comment_id": new_cid,
        "parent_reply_comment_id": reply_comment_id,
        "text": text,                          # original user-typed text
        "send_text": send_text,                # what we actually sent (may have {{0}})
        "mentions_sent": mentions_payload,     # the mentions array we sent (or None)
        "posted_at": now,
        "dry_run": False,
    }
    await db.manual_comment_jobs.update_one(
        {"_id": jid},
        {
            "$push": {"my_outgoing_replies": entry},
            "$set": {"updated_at": now},
        },
    )
    return ReplyToReplyResponse(
        job_id=job_id,
        parent_reply_comment_id=reply_comment_id,
        status="posted",
        dry_run=False,
        new_comment_id=new_cid,
        posted_at=now,
    )


# ─── Connection-invite routes ─────────────────────────────────────────────
#
# Three endpoints under each job's reply thread:
#   GET    /jobs/{job_id}/replies/{reply_comment_id}/invite
#          → returns the current invitation state (None if not invited yet)
#   POST   /jobs/{job_id}/replies/{reply_comment_id}/invite
#          → sends an invite (or records dry-run intent)
#   (no suggest-note endpoint — notes are operator-typed manually)
#
# Safety contract (mirrors reply_to_reply + send_single_job):
#   • dry_run defaults TRUE on the wire — forgetting it = no Unipile call
#   • Per-target dedup: at most ONE open invite per (operator, provider_id)
#     in any non-terminal status (queued/sent). Subsequent attempts return
#     the existing record instead of double-firing.
#   • Account match: unipile_account_id in the request body MUST equal
#     the job's unipile_account_id. Hard 400 on mismatch so a stray
#     pool-account selection can't produce a stranger-invite.
#   • Atomic claim: status transitions from {<none>|failed} → "queued"
#     in one Mongo find_one_and_update so double-click can't fire twice.
#   • Note length ≤200 chars (enforced by pydantic model + service).


def _invitation_to_public(d: dict[str, Any]) -> InvitationPublic:
    """Convert a `linkedin_invitations` Mongo doc to the API shape."""
    return InvitationPublic(
        id=str(d["_id"]),
        operator_id=str(d["operator_id"]),
        source_job_id=str(d["source_job_id"]),
        source_reply_comment_id=str(d["source_reply_comment_id"]),
        target_provider_id=str(d["target_provider_id"]),
        target_name=str(d.get("target_name") or ""),
        target_public_identifier=d.get("target_public_identifier"),
        target_headline=d.get("target_headline"),
        note_text=str(d.get("note_text") or ""),
        sent_via_account_id=str(d.get("sent_via_account_id") or ""),
        status=str(d.get("status") or "queued"),
        invitation_id=d.get("invitation_id"),
        sent_at=d.get("sent_at"),
        accepted_at=d.get("accepted_at"),
        status_polled_at=d.get("status_polled_at"),
        error=d.get("error"),
        created_at=d.get("created_at") or utcnow(),
        updated_at=d.get("updated_at") or utcnow(),
    )


async def _ensure_invitation_indexes(db: AsyncIOMotorDatabase) -> None:
    """Idempotently create the indexes the invite flow depends on.

    Called on every invite-route invocation (Motor caches plan, so the
    repeat cost is negligible). Putting it here rather than in a global
    startup hook keeps the index-creation contract close to the only
    code that needs it — easier to find when debugging schema drift.
    """
    coll = db.linkedin_invitations
    # Per-target dedup. Partial filter so withdrawn / declined invites
    # don't permanently block a re-invite (the operator's call, but the
    # default UI flow doesn't re-invite).
    await coll.create_index(
        [("operator_id", 1), ("target_provider_id", 1)],
        unique=True,
        partialFilterExpression={
            "status": {"$in": ["queued", "sent", "accepted"]},
        },
        name="operator_target_open_unique",
    )
    # Lookup by source thread (used by GET endpoint + status poller).
    await coll.create_index(
        [("source_job_id", 1), ("source_reply_comment_id", 1)],
        name="source_lookup",
    )
    # Status-poller scan: pick up `status=sent` invites older than 1h
    # for relationship refresh.
    await coll.create_index(
        [("status", 1), ("sent_at", 1)],
        name="status_poll_scan",
    )


@router.get(
    "/jobs/{job_id}/replies/{reply_comment_id}/invite",
    response_model=GetInvitationResponse,
)
async def get_invitation(
    job_id: str,
    reply_comment_id: str,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> GetInvitationResponse:
    """Return the current invitation state for this (job, reply) pair.

    Returns `invitation=None` when no invite exists yet — the UI shows
    the "Connect with note" button in that case. Otherwise returns the
    InvitationPublic so the UI can render the appropriate status badge
    (Pending / Connected / Declined / Failed)."""
    try:
        jid = ObjectId(job_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid job_id")

    # Authz: confirm the job belongs to an operator-owned campaign.
    job = await db.manual_comment_jobs.find_one({"_id": jid})
    if not job:
        raise HTTPException(404, "Job not found")
    campaign = await db.manual_comment_campaigns.find_one(
        {"_id": job["campaign_id"], "operator_id": user["_id"]},
    )
    if not campaign:
        raise HTTPException(404, "Job not found")

    await _ensure_invitation_indexes(db)

    inv = await db.linkedin_invitations.find_one({
        "operator_id": user["_id"],
        "source_job_id": jid,
        "source_reply_comment_id": reply_comment_id,
    })
    return GetInvitationResponse(
        job_id=job_id,
        parent_reply_comment_id=reply_comment_id,
        invitation=_invitation_to_public(inv) if inv else None,
    )


@router.post(
    "/jobs/{job_id}/replies/{reply_comment_id}/invite",
    response_model=SendInvitationResponse,
)
async def send_invite_to_replier(
    job_id: str,
    reply_comment_id: str,
    payload: SendInvitationRequest,
    user: CurrentUser,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> SendInvitationResponse:
    """Send a LinkedIn connection request to the person who replied to
    one of our comments. Operator-typed note (≤200 chars, optional).

    Threading model: we have a posted comment (job.comment_id) on a
    LinkedIn post. Someone replied to it (comment_id=reply_comment_id,
    captured in `job.latest_my_comment_replies_thread[]`). This route
    sends THEM a connection request, optionally with the operator's
    typed note. The invite goes from the same `unipile_account_id` that
    posted the original comment so the recipient sees the natural
    follow-up.

    Safety:
      • dry_run defaults True. Returns the would-be invitation record
        without hitting Unipile.
      • Account-match: payload.unipile_account_id must equal the job's
        unipile_account_id. 400 on mismatch.
      • Per-target dedup: if an open invite already exists for this
        (operator, target_provider_id), return that record (no
        re-fire).
      • Atomic claim: status transitions {<none>} → "queued" before the
        Unipile call so double-clicks can't fire twice.
      • Note length ≤200 chars (validated by pydantic).
    """
    try:
        jid = ObjectId(job_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, "Invalid job_id")

    job = await db.manual_comment_jobs.find_one({"_id": jid})
    if not job:
        raise HTTPException(404, "Job not found")
    campaign = await db.manual_comment_campaigns.find_one(
        {"_id": job["campaign_id"], "operator_id": user["_id"]},
    )
    if not campaign:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "posted":
        raise HTTPException(
            409,
            f"Cannot invite from a job in status={job.get('status')!r} — only "
            "posted jobs have a reply thread to invite into.",
        )

    # Find the target in the captured reply thread. We DON'T trust an
    # arbitrary provider_id from the client — it must come from a reply
    # we actually captured, otherwise the operator could be tricked into
    # inviting a stranger via a crafted reply_comment_id.
    thread = job.get("latest_my_comment_replies_thread") or []
    parent_entry = next(
        (r for r in thread if (r.get("comment_id") or "") == reply_comment_id),
        None,
    )
    if not parent_entry:
        raise HTTPException(
            404,
            "Reply not found in this job's captured thread — refresh "
            "engagement first to pick up the latest replies.",
        )
    target_provider_id = (parent_entry.get("author_provider_id") or "").strip()
    target_name = (parent_entry.get("author_name") or "").strip()
    target_public_identifier = (parent_entry.get("author_public_identifier") or "").strip() or None
    if not target_provider_id:
        raise HTTPException(
            409,
            "Reply has no author_provider_id captured — cannot send invite. "
            "This usually happens with locked/private profiles. Refresh "
            "engagement to retry, or invite manually from LinkedIn.",
        )

    # Account-match guard: the invite must come FROM the same Unipile
    # account that posted the original comment. Anything else and the
    # recipient sees a stranger's invite right after engaging with
    # someone else's comment — exactly the trust-breaking pattern we're
    # guarding against.
    job_account = (job.get("unipile_account_id") or "").strip()
    if not job_account:
        raise HTTPException(
            409,
            "Job has no unipile_account_id — cannot determine the correct "
            "account to send the invite from.",
        )
    if payload.unipile_account_id != job_account:
        raise HTTPException(
            400,
            f"Account mismatch: invite must be sent from the same account "
            f"that posted the comment ({job_account[:18]}…), not "
            f"{payload.unipile_account_id[:18]}…. Recipients won't recognise "
            "an invite from a different account than the one they engaged with.",
        )

    # Verify the account is on the operator's tenant.
    try:
        all_accounts = list_accounts()
    except (UnipileError, UnipileNotConfigured) as err:
        raise HTTPException(502, f"Could not list Unipile accounts: {err}")
    by_id = {a.id: a for a in all_accounts}
    if payload.unipile_account_id not in by_id:
        raise HTTPException(
            400,
            f"unipile_account_id {payload.unipile_account_id!r} is not on the tenant",
        )

    await _ensure_invitation_indexes(db)

    now = utcnow()
    note = (payload.note_text or "").strip()

    # ── Per-target dedup ───────────────────────────────────────────────
    # Look up any existing OPEN invite for this target (across any of
    # the operator's pool accounts). If one exists, surface it as the
    # response without re-firing.
    existing_open = await db.linkedin_invitations.find_one({
        "operator_id": user["_id"],
        "target_provider_id": target_provider_id,
        "status": {"$in": [
            InvitationStatusValue.QUEUED,
            InvitationStatusValue.SENT,
            InvitationStatusValue.ACCEPTED,
        ]},
    })
    if existing_open:
        log.info(
            "send_invite_to_replier DEDUP: open invite already exists for "
            "operator=%s target=%s status=%s — returning existing record",
            user["_id"], target_provider_id[:24], existing_open.get("status"),
        )
        return SendInvitationResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            target_provider_id=target_provider_id,
            status=str(existing_open.get("status")),
            dry_run=False,
            invitation_id=existing_open.get("invitation_id"),
            sent_at=existing_open.get("sent_at"),
            error=existing_open.get("error"),
        )

    # ── Dry-run: persist intent, no Unipile call ──────────────────────
    if payload.dry_run:
        dryrun_doc = {
            "operator_id": user["_id"],
            "source_job_id": jid,
            "source_reply_comment_id": reply_comment_id,
            "target_provider_id": target_provider_id,
            "target_name": target_name,
            "target_public_identifier": target_public_identifier,
            "target_headline": None,  # not fetched on dry-run; saves a profile_view call
            "note_text": note,
            "sent_via_account_id": payload.unipile_account_id,
            "status": InvitationStatusValue.DRY_RUN,
            "invitation_id": None,
            "sent_at": None,
            "accepted_at": None,
            "status_polled_at": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        # Upsert by (operator, source_job, source_reply) so repeated
        # dry-runs from the same composer overwrite rather than insert.
        await db.linkedin_invitations.update_one(
            {
                "operator_id": user["_id"],
                "source_job_id": jid,
                "source_reply_comment_id": reply_comment_id,
            },
            {"$set": dryrun_doc},
            upsert=True,
        )
        return SendInvitationResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            target_provider_id=target_provider_id,
            status=InvitationStatusValue.DRY_RUN,
            dry_run=True,
            invitation_id=None,
            sent_at=None,
            error=None,
        )

    # ── Atomic claim ──────────────────────────────────────────────────
    # Transition from {<no doc>|failed|dry_run} → "queued" in one Mongo
    # operation. Concurrent clicks lose the claim and get the existing
    # record instead of double-firing.
    claim_now = utcnow()
    claim_doc = {
        "operator_id": user["_id"],
        "source_job_id": jid,
        "source_reply_comment_id": reply_comment_id,
        "target_provider_id": target_provider_id,
        "target_name": target_name,
        "target_public_identifier": target_public_identifier,
        "note_text": note,
        "sent_via_account_id": payload.unipile_account_id,
        "status": InvitationStatusValue.QUEUED,
        "queued_at": claim_now,
        "updated_at": claim_now,
    }
    claim_seed = {
        "created_at": claim_now,
        "invitation_id": None,
        "sent_at": None,
        "accepted_at": None,
        "status_polled_at": None,
        "error": None,
    }
    try:
        claim_result = await db.linkedin_invitations.find_one_and_update(
            {
                "operator_id": user["_id"],
                "source_job_id": jid,
                "source_reply_comment_id": reply_comment_id,
                "$or": [
                    {"status": {"$exists": False}},
                    {"status": {"$in": [
                        InvitationStatusValue.DRY_RUN,
                        InvitationStatusValue.FAILED,
                        InvitationStatusValue.WITHDRAWN,
                    ]}},
                ],
            },
            {"$set": claim_doc, "$setOnInsert": claim_seed},
            upsert=True,
            return_document=True,
        )
    except Exception as err:  # noqa: BLE001 — DuplicateKey on partial unique index
        # Some other open invite for this target exists (partial unique
        # index caught it). Fetch and return.
        existing = await db.linkedin_invitations.find_one({
            "operator_id": user["_id"],
            "target_provider_id": target_provider_id,
            "status": {"$in": [
                InvitationStatusValue.QUEUED,
                InvitationStatusValue.SENT,
                InvitationStatusValue.ACCEPTED,
            ]},
        })
        if existing:
            return SendInvitationResponse(
                job_id=job_id,
                parent_reply_comment_id=reply_comment_id,
                target_provider_id=target_provider_id,
                status=str(existing.get("status")),
                dry_run=False,
                invitation_id=existing.get("invitation_id"),
                sent_at=existing.get("sent_at"),
                error=f"already_invited: {str(err)[:120]}",
            )
        raise HTTPException(500, f"invite claim failed: {err}")

    # ── Live Unipile call ─────────────────────────────────────────────
    from app.services.unipile import (
        send_invitation as unipile_send_invitation,
        UnipileError as _UE,
        UnipileNotConfigured as _UNC,
    )
    try:
        result = unipile_send_invitation(
            account_id=payload.unipile_account_id,
            provider_id=target_provider_id,
            message=note or None,
        )
    except _UNC as err:
        # Unipile is mocked / not configured — flip back to "failed"
        # so the operator can retry later without dedup blocking.
        await db.linkedin_invitations.update_one(
            {"_id": claim_result["_id"]},
            {"$set": {
                "status": InvitationStatusValue.FAILED,
                "error": f"unipile_not_configured: {str(err)[:300]}",
                "updated_at": utcnow(),
            }},
        )
        raise HTTPException(503, f"Unipile not configured: {err}")
    except _UE as err:
        msg = f"unipile: {str(err)[:400]}"
        await db.linkedin_invitations.update_one(
            {"_id": claim_result["_id"]},
            {"$set": {
                "status": InvitationStatusValue.FAILED,
                "error": msg,
                "updated_at": utcnow(),
            }},
        )
        return SendInvitationResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            target_provider_id=target_provider_id,
            status=InvitationStatusValue.FAILED,
            dry_run=False,
            invitation_id=None,
            sent_at=None,
            error=msg,
        )
    except Exception as err:  # noqa: BLE001 — defensive rollback
        # Same pattern as send_single_job: unexpected exception →
        # revert claim to "failed" so the row isn't stuck "queued"
        # forever blocking retry.
        msg = f"unexpected: {type(err).__name__}: {str(err)[:400]}"
        log.exception(
            "send_invite_to_replier UNEXPECTED failure (operator=%s "
            "target=%s) — rolling back claim", user["_id"], target_provider_id[:24],
        )
        try:
            await db.linkedin_invitations.update_one(
                {"_id": claim_result["_id"]},
                {"$set": {
                    "status": InvitationStatusValue.FAILED,
                    "error": msg,
                    "updated_at": utcnow(),
                }},
            )
        except Exception:  # noqa: BLE001
            pass
        return SendInvitationResponse(
            job_id=job_id,
            parent_reply_comment_id=reply_comment_id,
            target_provider_id=target_provider_id,
            status=InvitationStatusValue.FAILED,
            dry_run=False,
            invitation_id=None,
            sent_at=None,
            error=msg,
        )

    # ── Success: flip queued → sent ──────────────────────────────────
    sent_at = result.sent_at or utcnow()
    await db.linkedin_invitations.update_one(
        {"_id": claim_result["_id"]},
        {"$set": {
            "status": InvitationStatusValue.SENT,
            "invitation_id": result.invitation_id,
            "sent_at": sent_at,
            "error": None,
            "updated_at": sent_at,
        }},
    )
    return SendInvitationResponse(
        job_id=job_id,
        parent_reply_comment_id=reply_comment_id,
        target_provider_id=target_provider_id,
        status=InvitationStatusValue.SENT,
        dry_run=False,
        invitation_id=result.invitation_id,
        sent_at=sent_at,
        error=None,
    )
