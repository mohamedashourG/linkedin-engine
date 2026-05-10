"""Unipile classic post search body includes optional location geo ids."""

from __future__ import annotations

from app.services.unipile import _post_search_body


def test_post_search_body_includes_location_ids():
    body = _post_search_body(
        query="hiring",
        sort_by="date",
        date_posted="past_month",
        content_type="documents",
        author_keywords=None,
        location_ids=["103644278", "102448103"],
    )
    assert body["location"] == ["103644278", "102448103"]
    assert body["keywords"] == "hiring"


def test_post_search_body_omits_empty_location_ids():
    body = _post_search_body(
        query="hiring",
        sort_by="date",
        date_posted="past_month",
        content_type="documents",
        location_ids=[],
    )
    assert "location" not in body
