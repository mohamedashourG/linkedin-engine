"""
Manual target-contact list. Stored in `discovery_seeds` with source="manual"
so the existing discovery stage already consumes them. Manual seeds never
expire and are reused every run (vs harvester seeds which are one-shot).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    linkedin_url: HttpUrl | None = None


class ContactBulkRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=200_000,
        description=(
            "One contact per line. Each line accepts: 'Name', 'Name, "
            "https://linkedin.com/in/slug', 'Name, Title, Company', or just a "
            "LinkedIn URL."
        ),
    )


class ContactPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    name: str
    title: str | None = None
    company: str | None = None
    linkedin_url: str | None = None
    status: str
    created_at: datetime


def contact_to_public(doc: dict[str, Any]) -> ContactPublic:
    return ContactPublic(
        _id=str(doc["_id"]),
        name=doc.get("extracted_name") or "",
        title=doc.get("extracted_title"),
        company=doc.get("extracted_company"),
        linkedin_url=doc.get("linkedin_url"),
        status=doc.get("status") or "pending",
        created_at=doc["created_at"],
    )


def parse_bulk(text: str) -> list[ContactCreate]:
    """Parse the freeform textarea input into ContactCreate rows."""
    out: list[ContactCreate] = []
    for raw in text.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("#"):
            continue
        # URL-only line.
        if line.lower().startswith(("http://", "https://")) and "," not in line:
            slug = _slug_from_url(line)
            out.append(ContactCreate(name=slug or line, linkedin_url=line))
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0]
        if not name:
            continue
        title: str | None = None
        company: str | None = None
        url: str | None = None
        for p in parts[1:]:
            if p.lower().startswith(("http://", "https://")):
                url = p
            elif title is None:
                title = p
            elif company is None:
                company = p
        try:
            out.append(
                ContactCreate(
                    name=name,
                    title=title,
                    company=company,
                    linkedin_url=url,  # type: ignore[arg-type]
                )
            )
        except (ValueError, TypeError):
            # malformed URL — keep the contact without it
            out.append(ContactCreate(name=name, title=title, company=company))
    return out


def _slug_from_url(url: str) -> str | None:
    if "/in/" not in url:
        return None
    try:
        return url.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    except (IndexError, ValueError):
        return None
