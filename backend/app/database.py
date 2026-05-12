from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings


class _Mongo:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


mongo = _Mongo()


async def connect_to_mongo() -> None:
    mongo.client = AsyncIOMotorClient(settings.mongodb_uri)
    mongo.db = mongo.client[settings.mongodb_db]
    await _ensure_indexes()


async def close_mongo_connection() -> None:
    if mongo.client is not None:
        mongo.client.close()
        mongo.client = None
        mongo.db = None


def get_db() -> AsyncIOMotorDatabase:
    if mongo.db is None:
        raise RuntimeError("MongoDB is not initialized. Call connect_to_mongo() first.")
    return mongo.db


async def _ensure_indexes() -> None:
    db = get_db()
    await db.users.create_index("email", unique=True)
    await db.cofounders.create_index([("operator_id", 1), ("active", 1)])
    await db.candidates.create_index([("operator_id", 1), ("status", 1)])
    await db.candidates.create_index([("operator_id", 1), ("shipped_at", -1)])
    # Pipeline + slate/today load candidates by run; without this Mongo scans the full collection.
    await db.candidates.create_index([("slate_run_id", 1), ("status", 1)])
    await db.leads.create_index([("operator_id", 1), ("linkedin_url", 1)], unique=True)
    await db.exhaustion_ledger.create_index(
        [("operator_id", 1), ("linkedin_url", 1)], unique=True
    )
    await db.slate_runs.create_index([("operator_id", 1), ("run_date", -1)])
    await db.replies.create_index([("operator_id", 1), ("detected_at", -1)])
    await db.bookings.create_index([("operator_id", 1), ("booked_at", -1)])
    await db.audit_records.create_index([("operator_id", 1), ("created_at", -1)])
    await db.discovery_seeds.create_index(
        [("operator_id", 1), ("status", 1), ("expires_at", 1)]
    )
    await db.eod_logs.create_index([("operator_id", 1), ("log_date", -1)])
    await db.our_comments.create_index(
        [("comment_id", 1)], unique=True, sparse=True
    )
    await db.our_comments.create_index(
        [("candidate_id", 1), ("parent_comment_id", 1)], unique=True
    )
    await db.our_comments.create_index([("operator_id", 1), ("posted_at", -1)])
    await db.comment_engagement_snapshots.create_index(
        [("comment_id", 1), ("polled_at", -1)]
    )
    await db.parent_post_snapshots.create_index(
        [("parent_post_url", 1), ("polled_at", -1)]
    )
