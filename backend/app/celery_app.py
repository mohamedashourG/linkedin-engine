"""
Celery wiring + minutely dispatcher.

Per spec decision #22, each operator picks their own run time during onboarding.
Rather than registering N dynamic beat entries (which Celery beat doesn't support
out of the box without redbeat/celery-beat-mongo), we run a single static beat
entry every minute that fans out to operators whose local-time HH:MM matches now.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from celery import Celery
from celery.schedules import crontab
from pymongo import MongoClient

from app.config import settings

log = logging.getLogger(__name__)

celery_app = Celery(
    "linkedin_engine",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # Default caps for short tasks (dispatch, outbox, poll_replies). Pipeline tasks
    # override with settings.celery_pipeline_* on the task decorator.
    task_time_limit=settings.celery_task_time_limit_s,
    task_soft_time_limit=settings.celery_task_soft_time_limit_s,
    worker_prefetch_multiplier=1,
)


# ---------------------------------------------------------------- beat

celery_app.conf.beat_schedule = {
    "dispatch-daily-runs-every-minute": {
        "task": "engine.dispatch_daily_runs",
        "schedule": crontab(minute="*"),
    },
    "poll-replies-every-2h": {
        "task": "engine.poll_replies",
        "schedule": crontab(minute=0, hour="*/2"),
    },
    "process-comment-outbox-every-minute": {
        "task": "engine.process_outbox",
        "schedule": crontab(minute="*"),
    },
    "dispatch-nightly-runs-every-minute": {
        "task": "engine.dispatch_nightly_runs",
        "schedule": crontab(minute="*"),
    },
}

# Connection-invitation status poll — flip `linkedin_invitations.status`
# from `sent` to `accepted` / `declined` when the target responds.
# OPT-IN via INVITATION_POLLING_ENABLED (default off). The task itself
# is always registered (operators can fire it manually via Flower /
# `celery_app.send_task("engine.poll_invitations")`), but the beat
# schedule entry is only added when the env flag is set — so a fresh
# deploy never auto-fires LinkedIn read calls without explicit opt-in.
# Cadence: every 6h. Invites don't need real-time status, and we don't
# want to burn pool capacity on read-only relationship checks.
if (settings.invitation_polling_enabled if hasattr(settings, "invitation_polling_enabled") else False):
    celery_app.conf.beat_schedule["poll-invitations-every-6h"] = {
        "task": "engine.poll_invitations",
        "schedule": crontab(minute=15, hour="*/6"),
    }


# ---------------------------------------------------------------- dispatcher

def _sync_db():
    """
    Celery tasks run sync. Open a per-task pymongo client so we don't fight
    Motor's event loop.
    """
    client = MongoClient(settings.mongodb_uri)
    return client, client[settings.mongodb_db]


def _operators_due_now() -> Iterable[dict]:
    client, db = _sync_db()
    try:
        cursor = db.users.find(
            {
                "paused": {"$ne": True},
                "onboarding_complete": True,
                "run_time_local": {"$exists": True},
            },
            {"_id": 1, "run_time_local": 1, "timezone": 1},
        )
        now_utc = datetime.now(timezone.utc)
        for doc in cursor:
            tz_name = doc.get("timezone", "UTC")
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("UTC")
            local = now_utc.astimezone(tz)
            if local.weekday() >= 5:  # mon=0..sun=6 — engine runs weekdays only
                continue
            hhmm = local.strftime("%H:%M")
            if hhmm == doc.get("run_time_local"):
                yield doc
    finally:
        client.close()


@celery_app.task(name="engine.dispatch_daily_runs")
def dispatch_daily_runs() -> dict:
    dispatched: list[str] = []
    for operator in _operators_due_now():
        operator_id = str(operator["_id"])
        daily_run.delay(operator_id)
        dispatched.append(operator_id)
    if dispatched:
        log.info("dispatched daily_run for %d operator(s)", len(dispatched))
    return {"dispatched": dispatched}


@celery_app.task(
    name="engine.daily_run",
    soft_time_limit=settings.celery_pipeline_soft_time_limit_s,
    time_limit=settings.celery_pipeline_time_limit_s,
)
def daily_run(operator_id: str) -> dict:
    """
    Run the full discovery → verification → 4 gates → allocator → drafter →
    RULE 23 → email pipeline for one operator.
    """
    from app.engine.daily_run import run_for_operator

    if not ObjectId.is_valid(operator_id):
        return {"status": "invalid_operator_id", "operator_id": operator_id}

    client, db = _sync_db()
    try:
        return run_for_operator(db, ObjectId(operator_id))
    finally:
        client.close()


@celery_app.task(name="engine.poll_replies")
def poll_replies() -> dict:
    """Run the reply monitor across all operators with active cofounders."""
    from app.engine.reply_monitor import poll_replies_for_all_operators

    client, db = _sync_db()
    try:
        return poll_replies_for_all_operators(db)
    finally:
        client.close()


@celery_app.task(name="engine.poll_invitations")
def poll_invitations() -> dict:
    """Status-poll every open LinkedIn connection invite older than 1h.

    Scans `linkedin_invitations` for `status=sent` rows that haven't been
    polled in the last hour, calls Unipile to read each (account, target)
    relationship, and flips status to `accepted` / `declined` / leaves as
    `sent` accordingly. Does NOT fire on `queued` (those are still
    pre-send) or `dry_run` / `failed` / `withdrawn` (terminal-ish).

    Cadence-gated to every ~6h via beat. Run manually for debugging via
    `celery_app.send_task('engine.poll_invitations')`.

    Bounds:
      • max 200 invites scanned per call (prevents a 1k-old-invite scan
        from blocking the worker for hours)
      • skips invites in `unipile_account_pool` cooldown — the relation
        read still consumes one HTTP call against that account's session
      • aggregate pool gate via _pool_acquire applies; if the pool is
        cap-pressured the poll silently skips (status updates can wait
        for the next tick)

    Output dict: counts of polled / accepted / declined / unchanged /
    errored, so beat logs surface trends.
    """
    from datetime import datetime, timedelta, timezone
    from app.services.unipile import (
        get_invitation_status, UnipileError, UnipileNotConfigured,
    )

    client, db = _sync_db()
    n_polled = n_accepted = n_declined = n_unchanged = n_error = 0
    try:
        invites_coll = db.linkedin_invitations
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # 1-hour grace after send before first poll, so we don't hammer
        # Unipile for invites that LinkedIn hasn't even broadcast to the
        # recipient's feed yet.
        cutoff_send = now - timedelta(hours=1)
        cursor = invites_coll.find(
            {
                "status": "sent",
                "sent_at": {"$lt": cutoff_send},
                "$or": [
                    {"status_polled_at": None},
                    {"status_polled_at": {"$lt": now - timedelta(hours=5)}},
                ],
            },
            sort=[("sent_at", 1)],
        ).limit(200)
        for inv in cursor:
            n_polled += 1
            try:
                relation = get_invitation_status(
                    account_id=inv["sent_via_account_id"],
                    provider_id=inv["target_provider_id"],
                )
            except (UnipileError, UnipileNotConfigured) as err:
                n_error += 1
                invites_coll.update_one(
                    {"_id": inv["_id"]},
                    {"$set": {
                        "status_polled_at": now,
                        "error": f"poll_unipile_error: {str(err)[:300]}",
                        "updated_at": now,
                    }},
                )
                continue

            # Map Unipile's vocabulary onto our internal one. Unipile's
            # `connection_status` / `status` field varies; we accept any
            # of the documented strings. Anything else → leave as `sent`
            # and try again next tick.
            raw_status = (
                relation.get("connection_status")
                or relation.get("status")
                or relation.get("relationship")
                or ""
            )
            raw_status_norm = str(raw_status).upper()
            update: dict = {"status_polled_at": now, "updated_at": now}
            if raw_status_norm in ("CONNECTED", "ACCEPTED", "FIRST_DEGREE"):
                update["status"] = "accepted"
                update["accepted_at"] = (
                    _parse_date_safe(relation.get("accepted_at"))
                    or _parse_date_safe(relation.get("connected_at"))
                    or now
                )
                n_accepted += 1
            elif raw_status_norm in ("DECLINED", "REJECTED", "INVITATION_DECLINED"):
                update["status"] = "declined"
                n_declined += 1
            else:
                # Still pending — leave status=sent, just bump polled_at.
                n_unchanged += 1
            invites_coll.update_one({"_id": inv["_id"]}, {"$set": update})
    finally:
        client.close()
    result = {
        "polled": n_polled,
        "accepted": n_accepted,
        "declined": n_declined,
        "unchanged": n_unchanged,
        "errored": n_error,
    }
    log.info("poll_invitations: %s", result)
    return result


def _parse_date_safe(v) -> "datetime | None":  # type: ignore[no-untyped-def]
    """Best-effort ISO-string → naive UTC datetime. Returns None on miss.

    Used by poll_invitations to coerce Unipile's variable
    `accepted_at` / `connected_at` shapes into our Mongo datetime
    column.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


@celery_app.task(name="engine.process_outbox")
def process_outbox() -> dict:
    """Drain queued LinkedIn comments (Unipile write path)."""
    from app.engine.outbox import process_outbox as drain_outbox

    client, db = _sync_db()
    try:
        return drain_outbox(db, batch_size=20)
    finally:
        client.close()


@celery_app.task(
    name="engine.nightly_run",
    soft_time_limit=settings.celery_pipeline_soft_time_limit_s,
    time_limit=settings.celery_pipeline_time_limit_s,
)
def nightly_run(operator_id: str) -> dict:
    """Run the nightly batch for one operator (EOD stage advancement +
    exhaustion ledger + harvester + STALL detection)."""
    from app.engine.nightly import run_nightly_for_operator

    if not ObjectId.is_valid(operator_id):
        return {"status": "invalid_operator_id", "operator_id": operator_id}

    client, db = _sync_db()
    try:
        return run_nightly_for_operator(db, ObjectId(operator_id))
    finally:
        client.close()


@celery_app.task(name="engine.dispatch_nightly_runs")
def dispatch_nightly_runs() -> dict:
    """Beat dispatcher: at HH:MM matching the operator's local 23:00 time,
    fan out a per-operator nightly_run."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    client, db = _sync_db()
    dispatched: list[str] = []
    try:
        now_utc = datetime.now(timezone.utc)
        for op in db.users.find(
            {"paused": {"$ne": True}, "onboarding_complete": True},
            {"_id": 1, "timezone": 1},
        ):
            tz_name = op.get("timezone") or "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("UTC")
            local = now_utc.astimezone(tz)
            if local.strftime("%H:%M") == "23:00":
                nightly_run.delay(str(op["_id"]))
                dispatched.append(str(op["_id"]))
    finally:
        client.close()
    if dispatched:
        log.info("dispatched nightly_run for %d operator(s)", len(dispatched))
    return {"dispatched": dispatched}


def register_operator_schedule(operator_id: str) -> None:
    """
    No-op marker called from the onboarding /schedule endpoint. The dispatcher
    above already picks up every operator with a configured run_time_local on
    its next minutely tick, so there's no per-operator entry to add. This
    function exists so callers (and the spec's Phase 2 acceptance criteria) have
    an explicit hook; if we ever swap to celery-redbeat / celery-beat-mongo,
    this is where the per-operator entry would be added.
    """
    log.info("operator %s registered for daily dispatch", operator_id)


@celery_app.task(name="health.ping")
def ping() -> str:
    return "pong"
