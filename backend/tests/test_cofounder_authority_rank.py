"""
RULE 13 — cofounder roster carries an authority_rank used by RULE 1 to route
top-ICP candidates to the highest-authority account first.

Pure-pydantic tests; no Mongo. The integration with the allocator lands in
the RULE 1 commit.
"""
from __future__ import annotations

import pytest
from bson import ObjectId
from pydantic import HttpUrl, ValidationError

from app.models.cofounder import (
    CofounderCreate,
    CofounderUpdate,
    cofounder_to_public,
    new_cofounder_doc,
)


def _payload(**overrides) -> dict:
    base = dict(
        display_name="Alex",
        linkedin_url="https://www.linkedin.com/in/alex/",
        email="alex@example.com",
        daily_volume_target=22,
    )
    base.update(overrides)
    return base


def test_authority_rank_defaults_to_100():
    """New cofounders default to the bottom of the rotation. Operators have
    to deliberately promote them via authority_rank=1 (etc.)."""
    payload = CofounderCreate(**_payload())
    assert payload.authority_rank == 100


def test_authority_rank_explicit_value_persisted():
    payload = CofounderCreate(**_payload(authority_rank=1))
    doc = new_cofounder_doc(ObjectId(), payload)
    assert doc["authority_rank"] == 1


def test_authority_rank_lower_bound_rejected():
    """1 is the top spot; 0 / negative makes no sense."""
    with pytest.raises(ValidationError):
        CofounderCreate(**_payload(authority_rank=0))
    with pytest.raises(ValidationError):
        CofounderCreate(**_payload(authority_rank=-1))


def test_authority_rank_upper_bound_rejected():
    with pytest.raises(ValidationError):
        CofounderCreate(**_payload(authority_rank=1000))


def test_cofounder_update_can_promote_or_demote():
    promote = CofounderUpdate(authority_rank=1)
    assert promote.authority_rank == 1
    demote = CofounderUpdate(authority_rank=999)
    assert demote.authority_rank == 999


def test_cofounder_update_authority_rank_is_optional():
    """Updating other fields must not require resending authority_rank."""
    upd = CofounderUpdate(daily_volume_target=14)
    assert upd.authority_rank is None
    assert upd.daily_volume_target == 14


def test_cofounder_to_public_carries_authority_rank():
    from datetime import datetime, timezone
    doc = {
        "_id": ObjectId(),
        "display_name": "Rayan",
        "linkedin_url": "https://www.linkedin.com/in/rayan/",
        "calendly_url": None,
        "email": "rayan@example.com",
        "daily_volume_target": 14,
        "authority_rank": 2,
        "active": True,
        "created_at": datetime.now(timezone.utc),
    }
    public = cofounder_to_public(doc)
    assert public.authority_rank == 2


def test_cofounder_to_public_old_doc_without_rank_falls_back_to_100():
    """Back-compat: cofounders created before RULE 13 don't have the field
    in Mongo. They surface as the lowest priority (100) until promoted."""
    from datetime import datetime, timezone
    doc = {
        "_id": ObjectId(),
        "display_name": "Old Cofounder",
        "linkedin_url": "https://www.linkedin.com/in/old/",
        "calendly_url": None,
        "email": "old@example.com",
        "daily_volume_target": 14,
        "active": True,
        "created_at": datetime.now(timezone.utc),
        # NO authority_rank field
    }
    public = cofounder_to_public(doc)
    assert public.authority_rank == 100


def test_audit_example_alex_22_rayan_14_michael_14_distinct_ranks():
    """The audit's slate-50 example is Alex 22 / Rayan 14 / Michael 14, with
    Alex highest authority. Rank assignment should be straightforward."""
    alex = CofounderCreate(**_payload(display_name="Alex", daily_volume_target=22, authority_rank=1))
    rayan = CofounderCreate(**_payload(display_name="Rayan", daily_volume_target=14, authority_rank=2))
    michael = CofounderCreate(**_payload(display_name="Michael", daily_volume_target=14, authority_rank=3))
    assert (alex.authority_rank, rayan.authority_rank, michael.authority_rank) == (1, 2, 3)
    assert alex.daily_volume_target + rayan.daily_volume_target + michael.daily_volume_target == 50
