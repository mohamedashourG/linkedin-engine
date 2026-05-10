"""GET /v1/linkedin/post parsing and mock enrichment."""
from __future__ import annotations

from app.services.apidirect import LinkedInPostDetails, _mock_post_details


def test_linked_in_post_details_from_api_sample_doc():
    raw = {
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:1",
        "text": "Hello world",
        "date": "2024-07-15 10:30:00",
        "author": "Jane Smith",
        "author_description": "1,234 followers",
        "author_url": "https://www.linkedin.com/in/janesmith/",
        "likes": 1,
        "urn": "urn:li:activity:7219434359085252608",
        "is_repost": False,
        "source": "LinkedIn",
        "domain": "linkedin.com",
    }
    d = LinkedInPostDetails.from_api(raw)
    assert d.author == "Jane Smith"
    assert d.author_url == "https://www.linkedin.com/in/janesmith/"
    assert d.text == "Hello world"
    assert d.urn == "urn:li:activity:7219434359085252608"
    assert d.is_repost is False
    assert d.published_at is not None


def test_mock_post_details_returns_profile_url():
    url = "https://www.linkedin.com/posts/jane-rivera-platform_internal-developer-platform-activity-1"
    d = _mock_post_details(url)
    assert d is not None
    assert d.author == "Jane Rivera"
    assert d.author_url and "/in/" in d.author_url


def test_mock_post_details_unknown_url():
    assert _mock_post_details("https://example.com/nope") is None
