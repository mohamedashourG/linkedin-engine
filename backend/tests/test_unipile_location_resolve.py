"""Location text → LinkedIn geo id resolution helpers (Unipile PARAMETERS)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.unipile import (
    UnipileSearchParameter,
    _location_query_variants,
    _score_location_title_match,
    resolve_location_ids_from_text,
)


def test_location_variants_includes_segments():
    v = _location_query_variants("San Francisco, CA")
    assert "San Francisco, CA" in v
    assert "San Francisco" in v
    assert "CA" in v


def test_location_variants_single_city():
    v = _location_query_variants("San Francisco")
    assert v[0] == "San Francisco"


def test_score_exact_and_metro_prefix():
    assert _score_location_title_match("San Francisco", "San Francisco") == 100.0
    assert _score_location_title_match(
        "San Francisco", "San Francisco Bay Area"
    ) > _score_location_title_match("San Francisco", "South San Francisco, CA")


def test_score_greater_london():
    assert _score_location_title_match("London", "Greater London") >= 70.0


@patch("app.services.unipile.search_parameter_ids")
def test_resolve_picks_best_row(mock_params):
    mock_params.side_effect = [
        [
            UnipileSearchParameter(id="bad", title="South San Francisco, California"),
            UnipileSearchParameter(id="good", title="San Francisco Bay Area"),
        ],
    ]

    ids = resolve_location_ids_from_text(
        account_id="acc",
        location_text="San Francisco",
        max_api_calls=1,
    )
    assert ids == ["good"]


@patch("app.services.unipile.search_parameter_ids")
def test_resolve_tiebreak_prefers_england_london(mock_params):
    """Same prefix score for two ``London, …`` rows → stable secondary sort by title."""
    mock_params.return_value = [
        UnipileSearchParameter(id="ont", title="London, Ontario, Canada"),
        UnipileSearchParameter(id="eng", title="London, England, United Kingdom"),
    ]
    ids = resolve_location_ids_from_text(
        account_id="acc",
        location_text="London",
        max_api_calls=1,
    )
    assert ids[0] == "eng"
