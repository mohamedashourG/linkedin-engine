from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ManualReply(BaseModel):
    linkedin_url: HttpUrl
    text: str = Field(min_length=1, max_length=5000)


class ManualBooking(BaseModel):
    linkedin_url: HttpUrl
    meeting_at: datetime


class CofounderEodInput(BaseModel):
    cofounder_id: str
    crs_sent: list[HttpUrl] = Field(default_factory=list)
    dms_sent: list[HttpUrl] = Field(default_factory=list)
    connections_accepted: list[HttpUrl] = Field(default_factory=list)
    replies_received_manual: list[ManualReply] = Field(default_factory=list)
    bookings_manual: list[ManualBooking] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)


class EodSubmitRequest(BaseModel):
    per_cofounder: list[CofounderEodInput]


class EodCofounderPrefill(BaseModel):
    cofounder_id: str
    cofounder_name: str
    shipped: int
    dropped: int
    edited: int
    replies_count: int
    bookings_count: int


class EodPrefillResponse(BaseModel):
    log_date: date
    per_cofounder: list[EodCofounderPrefill]
    last_submitted_at: datetime | None = None


class EodSubmitResponse(BaseModel):
    log_id: str
    nightly_task_id: str
