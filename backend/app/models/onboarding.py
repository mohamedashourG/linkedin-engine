from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class ProductExtractRequest(BaseModel):
    free_text: str = Field(min_length=20, max_length=50000)


class ProductExtractResponse(BaseModel):
    product_extracted: dict[str, Any]
    icp_rubric: dict[str, Any]


class ProductSaveRequest(BaseModel):
    product_description: str = Field(min_length=20, max_length=50000)
    product_extracted: dict[str, Any]
    icp_rubric: dict[str, Any]


class CalendlyConnectRequest(BaseModel):
    calendly_url: HttpUrl


class ScheduleRequest(BaseModel):
    run_time_local: str = Field(
        description="HH:MM 24-hour local time, e.g. '09:00'.",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    # RULE 2 audit-locked default. Operators can dial down for low-volume
    # rollouts; abort_floor still kicks in at 25 regardless.
    daily_target: int = Field(default=50, ge=1, le=200)


class OnboardingStatus(BaseModel):
    has_product: bool
    cofounder_count: int
    cofounders_with_voice: int
    has_calendly: bool
    has_schedule: bool
    onboarding_complete: bool
