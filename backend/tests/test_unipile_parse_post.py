"""Unipile post payload parsing (company vs person authors)."""

from __future__ import annotations

from app.services.unipile import _infer_author_is_company, _parse_unipile_post


def test_infer_company_from_flag_and_url():
    assert _infer_author_is_company({"is_company": True}, None) is True
    assert _infer_author_is_company(
        {},
        "https://www.linkedin.com/company/acme/posts",
    ) is True
    assert _infer_author_is_company({"is_company": False}, "https://linkedin.com/in/jane") is False


def test_infer_company_from_type():
    assert _infer_author_is_company({"type": "COMPANY"}, None) is True
    assert _infer_author_is_company({"type": "MEMBER"}, None) is False


def test_parse_post_marks_company_author():
    raw = {
        "id": "1",
        "share_url": "https://linkedin.com/feed/update/1",
        "text": "We are hiring",
        "author": {
            "name": "Acme Inc",
            "is_company": True,
            "public_profile_url": "https://www.linkedin.com/company/acme",
        },
        "parsed_datetime": "2026-01-01T12:00:00Z",
    }
    post = _parse_unipile_post(raw)
    assert post is not None
    assert post.author_is_company is True


def test_parse_post_person_author():
    raw = {
        "id": "2",
        "share_url": "https://linkedin.com/feed/update/2",
        "text": "Thoughts on RCM",
        "author": {
            "name": "Jane Doe",
            "headline": "CFO at HealthCo",
            "public_profile_url": "https://www.linkedin.com/in/janedoe",
        },
        "parsed_datetime": "2026-01-02T12:00:00Z",
    }
    post = _parse_unipile_post(raw)
    assert post is not None
    assert post.author_is_company is False
