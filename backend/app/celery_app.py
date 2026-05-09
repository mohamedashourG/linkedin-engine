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
    # Daily run is the longest task — gates can take ~15 min sequentially on
    # 700+ candidates. With ThreadPool parallelization that drops to ~2 min,
    # but we keep generous headroom for slow upstream APIs (Azure, Unipile).
    task_time_limit=1800,
    task_soft_time_limit=1500,
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
    "dispatch-nightly-runs-every-minute": {
        "task": "engine.dispatch_nightly_runs",
        "schedule": crontab(minute="*"),
    },
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


@celery_app.task(name="engine.daily_run")
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


@celery_app.task(name="engine.nightly_run")
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
