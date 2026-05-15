"""Unit tests for the Wiza Person Enrich provider.

Covers:
  * Response parsing of the `enrichment_level: "none"` payload (verified
    shape against the live Wiza API on 2026-05-14).
  * `enrich_profile` HTTP flow via a mock httpx.Client (start → poll →
    finished).
  * `enrich_profile` short-circuits on non-`/in/` URLs.
  * `enrich_profiles` batch helper deduplicates input and aggregates
    matches via the thread pool.
  * Quota / auth errors trip the process-wide circuit so the rest of
    discovery falls back to Crustdata.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services import wiza_enrich as wz


# ─── Response parsing ──────────────────────────────────────────────────────


def _sample_finished_data() -> dict:
    """Fixture mirroring the live shape from `curl /individual_reveals/{id}`
    when called with profile_url=Yair Lurie at enrichment_level=none."""
    return {
        "id": 519414593,
        "status": "finished",
        "is_complete": True,
        "name": "Yair Lurie",
        "company": "Cardiowell",
        "enrichment_level": "none",
        "linkedin_profile_url": "https://www.linkedin.com/in/yairlurie",
        "title": "Founder & CEO",
        "location": "San Francisco Bay Area",
        "company_size": 5,
        "company_domain": "cardiowell.com",
        "company_country": "United states",
        "company_founded": 2015,
        "company_industry": "Wellness and Fitness Services",
        "company_linkedin": "https://www.linkedin.com/company/cardiowell",
        "company_linkedin_id": "11032149",
        "company_locality": None,
        "company_location": "United states",
        "company_size_range": "2-10",
        "company_description": None,
        "company_subindustry": None,
        "sub_title": "Digital Health Founder | Helping clinics scale hypertension care...",
        "credits": {"api_credits": {"total": 1, "email_credits": 0, "phone_credits": 0, "scrape_credits": 1}},
    }


def test_parse_data_block_extracts_core_fields():
    p = wz._parse_data_block(_sample_finished_data())
    assert p.linkedin_url == "https://www.linkedin.com/in/yairlurie"
    assert p.name == "Yair Lurie"
    assert p.title == "Founder & CEO"
    assert p.headline.startswith("Digital Health Founder")
    assert p.location == "San Francisco Bay Area"
    assert p.employer_name == "Cardiowell"
    assert p.employer_domain == "cardiowell.com"
    assert p.employer_linkedin_id == "11032149"
    assert p.company_industry == "Wellness and Fitness Services"
    assert p.company_size == 5
    assert p.company_size_range == "2-10"
    assert p.company_country == "United states"
    assert p.company_founded == 2015


def test_parse_data_block_handles_nulls():
    data = {"linkedin_profile_url": "https://www.linkedin.com/in/x", "status": "finished"}
    p = wz._parse_data_block(data)
    assert p.linkedin_url == "https://www.linkedin.com/in/x"
    assert p.name is None
    assert p.company_industry is None
    assert p.company_size is None
    assert p.company_founded is None


# ─── enrich_profile short-circuits ─────────────────────────────────────────


def test_enrich_profile_returns_none_for_non_in_url():
    # Should not even attempt an HTTP call.
    assert wz.enrich_profile("") is None
    assert wz.enrich_profile("https://www.linkedin.com/company/cardiowell") is None
    assert wz.enrich_profile("https://example.com") is None


# ─── enrich_profile happy path (mock httpx) ────────────────────────────────


@pytest.fixture
def _reset_circuit():
    """Ensure each test starts with a clean circuit so tests don't bleed
    state into each other."""
    with wz._circuit_lock:
        wz._circuit_open = False
    yield
    with wz._circuit_lock:
        wz._circuit_open = False


def _make_mock_client(start_status: int, start_body: dict, poll_status: int, poll_body: dict):
    """Build a mock httpx.Client whose context manager returns a mock that
    responds to .post + .get with the configured statuses + bodies."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    start_resp = MagicMock()
    start_resp.status_code = start_status
    start_resp.json.return_value = start_body
    start_resp.text = str(start_body)
    client.post.return_value = start_resp

    poll_resp = MagicMock()
    poll_resp.status_code = poll_status
    poll_resp.json.return_value = poll_body
    poll_resp.text = str(poll_body)
    client.get.return_value = poll_resp

    return client


def test_enrich_profile_happy_path_yields_profile(_reset_circuit):
    mock_client = _make_mock_client(
        start_status=200,
        start_body={"data": {"id": 1, "status": "queued"}},
        poll_status=200,
        poll_body={"data": _sample_finished_data()},
    )
    with patch.object(wz, "_client", return_value=mock_client), \
         patch.object(wz.time, "sleep", lambda *_: None):
        with patch.object(wz.settings, "wiza_api_key", "dummy"):
            p = wz.enrich_profile("https://www.linkedin.com/in/yairlurie")
    assert p is not None
    assert p.company_industry == "Wellness and Fitness Services"
    assert p.location == "San Francisco Bay Area"


def test_enrich_profile_402_trips_circuit(_reset_circuit):
    """402 quota response should latch the global circuit so the rest of
    the discovery batch stops paying for failing calls."""
    mock_client = _make_mock_client(
        start_status=402,
        start_body={"error": "quota"},
        poll_status=200,
        poll_body={},
    )
    with patch.object(wz, "_client", return_value=mock_client), \
         patch.object(wz.settings, "wiza_api_key", "dummy"):
        with pytest.raises(wz.WizaEnrichQuotaExhausted):
            wz.enrich_profile("https://www.linkedin.com/in/yairlurie")
    # Second call: circuit is open, should raise immediately even before
    # any HTTP call.
    with pytest.raises(wz.WizaEnrichQuotaExhausted):
        wz.enrich_profile("https://www.linkedin.com/in/anyone")


def test_enrich_profile_failed_status_returns_none(_reset_circuit):
    """Wiza marks `status='failed'` when it couldn't match — caller treats
    as a miss, not an error."""
    mock_client = _make_mock_client(
        start_status=200,
        start_body={"data": {"id": 1}},
        poll_status=200,
        poll_body={"data": {"status": "failed", "fail_error": "no match"}},
    )
    with patch.object(wz, "_client", return_value=mock_client), \
         patch.object(wz.time, "sleep", lambda *_: None), \
         patch.object(wz.settings, "wiza_api_key", "dummy"):
        assert wz.enrich_profile("https://www.linkedin.com/in/x") is None


def test_enrich_profile_finished_but_no_url_is_miss(_reset_circuit):
    """`status='finished'` but missing `linkedin_profile_url` means Wiza
    'finished' the reveal but didn't actually find the person."""
    mock_client = _make_mock_client(
        start_status=200,
        start_body={"data": {"id": 1}},
        poll_status=200,
        poll_body={"data": {"status": "finished"}},  # no linkedin_profile_url
    )
    with patch.object(wz, "_client", return_value=mock_client), \
         patch.object(wz.time, "sleep", lambda *_: None), \
         patch.object(wz.settings, "wiza_api_key", "dummy"):
        assert wz.enrich_profile("https://www.linkedin.com/in/x") is None


# ─── enrich_profiles batch helper ──────────────────────────────────────────


def test_enrich_profiles_dedups_and_aggregates(_reset_circuit):
    """The batch helper should dedup input URLs before fanning out to the
    thread pool, and merge per-URL results into a single dict."""
    calls = {"count": 0}

    def fake_enrich(url):
        calls["count"] += 1
        return wz._parse_data_block({
            "linkedin_profile_url": url, "status": "finished", "name": "X",
        })

    urls = [
        "https://www.linkedin.com/in/a",
        "https://www.linkedin.com/in/b",
        "https://www.linkedin.com/in/a",  # duplicate
    ]
    with patch.object(wz, "enrich_profile", side_effect=fake_enrich):
        out = wz.enrich_profiles(urls)
    assert calls["count"] == 2  # deduped
    assert set(out.keys()) == {urls[0], urls[1]}


def test_enrich_profiles_empty_input_returns_empty(_reset_circuit):
    assert wz.enrich_profiles([]) == {}


def test_enrich_profiles_per_url_error_does_not_kill_batch(_reset_circuit):
    """One bad URL must not abort the whole batch — other matches still
    return."""
    good_url = "https://www.linkedin.com/in/good"

    def fake_enrich(url):
        if url == good_url:
            return wz._parse_data_block({
                "linkedin_profile_url": url, "status": "finished",
            })
        raise wz.WizaEnrichError("transient")

    with patch.object(wz, "enrich_profile", side_effect=fake_enrich):
        out = wz.enrich_profiles([
            good_url,
            "https://www.linkedin.com/in/bad",
        ])
    assert good_url in out
    assert "https://www.linkedin.com/in/bad" not in out


def test_enrich_profiles_quota_error_propagates(_reset_circuit):
    """Quota exhaustion is the one error class we re-raise so the caller
    can stop calling Wiza for the rest of the slate run."""
    def fake_enrich(url):
        raise wz.WizaEnrichQuotaExhausted("out of credits")

    with patch.object(wz, "enrich_profile", side_effect=fake_enrich):
        with pytest.raises(wz.WizaEnrichQuotaExhausted):
            wz.enrich_profiles(["https://www.linkedin.com/in/a"])
