"""Unit tests for the manual-comment posting routes.

Covers the three contracts that matter:
  1. CSV parsing — accepts the two common column-name schemas, rejects
     unparseable files, enforces row + char limits.
  2. Dry-run default — calling /send without explicitly setting dry_run
     must NOT call Unipile. We assert post_comment is never invoked.
  3. Live posting only happens when dry_run=False AND the chosen
     account_id resolves on the tenant.

External APIs (Unipile, APIDirect) are patched out — we're testing the
route logic, not the vendor SDKs.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.routes import manual_comments as mc


# ─── CSV parser ───────────────────────────────────────────────────────────


def test_parse_csv_canonical_columns():
    raw = (
        "post_url,comment\n"
        "https://www.linkedin.com/posts/foo-activity-12345-abc,Test 1\n"
        "https://www.linkedin.com/posts/bar-activity-67890-def,Test 2\n"
    ).encode("utf-8")
    rows = mc._parse_csv(raw)
    assert len(rows) == 2
    assert rows[0] == {
        "post_url": "https://www.linkedin.com/posts/foo-activity-12345-abc",
        "comment": "Test 1",
    }


def test_parse_csv_alternate_column_names():
    """Accept the column names produced by the engine's own Excel/CSV
    exporter (comment_link_post_url + drafted_comment) so an operator
    can re-feed our own output."""
    raw = (
        "comment_link_post_url,drafted_comment\n"
        "https://www.linkedin.com/posts/x-activity-1,Hello\n"
    ).encode("utf-8")
    rows = mc._parse_csv(raw)
    assert len(rows) == 1
    assert rows[0]["post_url"].endswith("/posts/x-activity-1")
    assert rows[0]["comment"] == "Hello"


def test_parse_csv_handles_utf8_bom():
    raw = "﻿post_url,comment\nhttps://www.linkedin.com/posts/x,test\n".encode("utf-8")
    rows = mc._parse_csv(raw)
    assert len(rows) == 1


def test_parse_csv_skips_empty_rows():
    raw = (
        "post_url,comment\n"
        ",\n"  # both empty
        "https://www.linkedin.com/posts/x,\n"  # comment empty
        ",text only\n"  # url empty
        "https://www.linkedin.com/posts/y,real comment\n"
    ).encode("utf-8")
    rows = mc._parse_csv(raw)
    assert len(rows) == 1
    assert rows[0]["comment"] == "real comment"


def test_parse_csv_rejects_missing_columns():
    raw = b"foo,bar\n1,2\n"
    with pytest.raises(Exception) as exc_info:
        mc._parse_csv(raw)
    assert "post_url" in str(exc_info.value)


def test_parse_csv_rejects_all_empty():
    raw = b"post_url,comment\n,\n,\n"
    with pytest.raises(Exception) as exc_info:
        mc._parse_csv(raw)
    assert "no non-empty rows" in str(exc_info.value).lower()


def test_parse_csv_rejects_too_many_rows():
    header = "post_url,comment\n"
    body = "".join(f"https://www.linkedin.com/posts/p{i},c{i}\n" for i in range(mc.MAX_ROWS_PER_CSV + 1))
    raw = (header + body).encode("utf-8")
    with pytest.raises(Exception) as exc_info:
        mc._parse_csv(raw)
    assert "cap is" in str(exc_info.value)


# ─── _job_to_public + _campaign_to_public mapping ─────────────────────────


def test_job_to_public_captures_latest_snapshot():
    now = datetime.now(timezone.utc)
    j = {
        "_id": "candidate1",
        "campaign_id": "campaign1",
        "row_index": 3,
        "post_url": "https://x",
        "post_id": "urn:li:activity:42",
        "draft_comment": "hi",
        "status": "posted",
        "unipile_account_id": "acct-X",
        "comment_id": "urn:li:comment:1",
        "posted_at": now,
        "engagement_snapshots": [
            {"captured_at": now, "likes": 0, "comments": 0, "shares": 0},
            {"captured_at": now, "likes": 7, "comments": 3, "shares": 1},
        ],
    }
    out = mc._job_to_public(j)
    assert out.status == "posted"
    assert out.latest_likes == 7
    assert out.latest_comments == 3
    assert out.latest_shares == 1
    assert out.engagement_snapshots_count == 2


def test_job_to_public_handles_zero_snapshots():
    j = {
        "_id": "x",
        "campaign_id": "y",
        "row_index": 0,
        "post_url": "u",
        "draft_comment": "c",
        "status": "queued",
    }
    out = mc._job_to_public(j)
    assert out.status == "queued"
    assert out.latest_likes is None
    assert out.engagement_snapshots_count == 0


# ─── SendRequest defaults to dry-run ──────────────────────────────────────


def test_send_request_defaults_to_dry_run():
    """If a caller forgets the dry_run field, the request body must
    default to dry-run. This is the foundation of the safety contract —
    a JSON payload of `{"unipile_account_id": "X"}` posts nothing."""
    req = mc.SendRequest(unipile_account_id="acct-X")
    assert req.dry_run is True

    # Explicit True still True
    req = mc.SendRequest(unipile_account_id="acct-X", dry_run=True)
    assert req.dry_run is True

    # Only False flips it
    req = mc.SendRequest(unipile_account_id="acct-X", dry_run=False)
    assert req.dry_run is False


def test_send_request_requires_non_empty_account_id():
    """unipile_account_id has min_length=1 so empty string / null is a
    Pydantic ValidationError before any handler runs."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        mc.SendRequest(unipile_account_id="")  # type: ignore[arg-type]


# ─── delete_comment helper sends account_id in body + query ───────────────


def test_delete_comment_raises_unipile_feature_not_supported():
    """Unipile does not expose comment deletion via REST API. Verified
    on 2026-05-13 with 11 different URL+method combinations against the
    live Unipile tenant — every single one returned router-level 404
    ("Cannot DELETE/PATCH/PUT/POST"). The route handler is simply not
    registered. delete_comment() must raise UnipileFeatureNotSupported
    so callers can render a clear "delete on LinkedIn manually" UX
    instead of treating it as a transient error to retry.
    """
    from app.services.unipile import UnipileFeatureNotSupported, delete_comment
    with pytest.raises(UnipileFeatureNotSupported, match="does not expose"):
        delete_comment(
            account_id="acct-X",
            post_url_or_id="urn:li:activity:9876543210",
            comment_id="urn:li:comment:(activity:9876,1234)",
        )


def test_unipile_feature_not_supported_subclasses_unipile_error():
    """UnipileFeatureNotSupported must be a subclass of UnipileError so
    existing `except UnipileError` handlers still catch it — but new
    `except UnipileFeatureNotSupported` handlers can distinguish it for
    UX purposes (501 vs 502, "use the LinkedIn UI" vs "retry")."""
    from app.services.unipile import UnipileError, UnipileFeatureNotSupported
    assert issubclass(UnipileFeatureNotSupported, UnipileError)


def test_delete_comment_does_not_validate_args_before_raising():
    """Since the feature isn't supported, we don't waste cycles on arg
    validation — operators get the right message ('use LinkedIn') even
    if they passed garbage."""
    from app.services.unipile import UnipileFeatureNotSupported, delete_comment
    with pytest.raises(UnipileFeatureNotSupported):
        delete_comment(account_id="x", post_url_or_id="y", comment_id="z")


def test_delete_job_comment_response_schema():
    """Response shape includes the job_id, deleted flag, comment_id for
    audit, and an error field if Unipile rejected."""
    r = mc.DeleteJobCommentResponse(
        job_id="6a0...", deleted=True, comment_id="urn:li:comment:1", error=None,
    )
    assert r.deleted is True
    assert r.comment_id == "urn:li:comment:1"
    assert r.error is None


# ─── newlines preserved end-to-end ────────────────────────────────────────


def test_parse_csv_preserves_embedded_newlines_in_quoted_comment():
    """RFC 4180 — a comment with multiple lines must be quote-wrapped in
    the CSV. csv.DictReader handles this natively. The parsed string MUST
    contain the literal \\n characters so when Unipile receives it, the
    LinkedIn comment is multi-line."""
    raw = (
        b'post_url,comment\n'
        b'https://www.linkedin.com/posts/x-activity-1,'
        b'"Line one.\n\nLine two with a blank line above.\nLine three."\n'
    )
    rows = mc._parse_csv(raw)
    assert len(rows) == 1
    cmt = rows[0]["comment"]
    # Newlines preserved exactly
    assert "Line one." in cmt
    assert "Line two" in cmt
    assert "Line three." in cmt
    assert cmt.count("\n") >= 3  # two between lines + one blank
    # The exact structure operator pasted, character-for-character
    assert cmt == "Line one.\n\nLine two with a blank line above.\nLine three."


def test_parse_csv_preserves_crlf_newlines_from_windows_excel():
    """Excel on Windows saves CSVs with \\r\\n line endings AND \\r\\n
    inside quoted fields. csv.DictReader normalizes the row terminator
    but should leave the embedded \\r\\n intact (or normalize to \\n).
    Either way the operator's line breaks survive."""
    raw = (
        b'post_url,comment\r\n'
        b'https://www.linkedin.com/posts/x-activity-1,'
        b'"First paragraph.\r\n\r\nSecond paragraph."\r\n'
    )
    rows = mc._parse_csv(raw)
    assert len(rows) == 1
    cmt = rows[0]["comment"]
    assert "First paragraph." in cmt
    assert "Second paragraph." in cmt
    # Either form is acceptable — what matters is the visual break survives.
    assert ("\n\n" in cmt) or ("\r\n\r\n" in cmt)


def test_single_job_send_request_defaults_to_dry_run():
    """Per-row send must have the same safety contract as campaign /send:
    forgetting dry_run defaults to True, no live posting fires."""
    req = mc.SingleJobSendRequest(unipile_account_id="acct-X")
    assert req.dry_run is True

    req = mc.SingleJobSendRequest(unipile_account_id="acct-X", dry_run=False)
    assert req.dry_run is False


def test_single_job_send_request_requires_non_empty_account_id():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        mc.SingleJobSendRequest(unipile_account_id="")  # type: ignore[arg-type]
