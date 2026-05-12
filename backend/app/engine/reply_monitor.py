"""
Reply monitor. Celery beat delegates to ``engagement_poller`` so one
``get_post_comments`` pass updates shipped-thread replies and ``our_comments``
engagement snapshots.
"""
from __future__ import annotations

import logging
from typing import Any

from pymongo.database import Database

from app.engine.engagement_poller import poll_engagement_and_replies_for_all_operators

log = logging.getLogger(__name__)


def poll_replies_for_all_operators(db: Database) -> dict[str, Any]:
    """Top-level entrypoint called by Celery beat (merged engagement + replies)."""
    return poll_engagement_and_replies_for_all_operators(db)
