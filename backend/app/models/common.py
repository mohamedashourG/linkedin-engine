from datetime import datetime, timezone
from typing import Annotated, Any

from bson import ObjectId
from pydantic import BeforeValidator, PlainSerializer


def _validate_object_id(v: Any) -> ObjectId:
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, str) and ObjectId.is_valid(v):
        return ObjectId(v)
    raise ValueError(f"Invalid ObjectId: {v!r}")


PyObjectId = Annotated[
    ObjectId,
    BeforeValidator(_validate_object_id),
    PlainSerializer(lambda v: str(v), return_type=str, when_used="json"),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
