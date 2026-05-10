"""Reconcile local cofounder state with existing Crustdata production watches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from app.routes.crustdata import RegisterWatchRequest, register
from app.services import crustdata as cd


def test_notification_endpoint_matches_ignores_host_ordering(monkeypatch):
    monkeypatch.setattr(
        cd.settings,
        "crustdata_webhook_secret",
        "test-secret-reconcile",
        raising=False,
    )
    monkeypatch.setattr(
        cd.settings,
        "crustdata_webhook_base_url",
        "https://a.example.com",
        raising=False,
    )
    cid = "507f1f77bcf86cd799439011"
    expected = cd.webhook_url_for(cid)
    alt = expected.replace("https://a.example.com", "https://b.example.com")
    assert cd.notification_endpoint_matches_cofounder(alt, cid)
    assert not cd.notification_endpoint_matches_cofounder(
        alt + "x", cid
    )


def test_find_reconciled_returns_first_match(monkeypatch):
    monkeypatch.setattr(
        cd.settings,
        "crustdata_webhook_secret",
        "test-secret-reconcile-2",
        raising=False,
    )
    monkeypatch.setattr(
        cd.settings,
        "crustdata_webhook_base_url",
        "https://tunnel.example",
        raising=False,
    )
    cid = "507f191e810c19729de860ea"
    url = cd.webhook_url_for(cid)

    fake_watches = [
        {"id": "other", "notification_endpoint": "https://x/wrong"},
        {
            "id": "42",
            "notification_endpoint": url,
            "keyword_expression": "alpha OR beta",
        },
    ]

    with patch.object(cd, "list_watches", return_value=fake_watches):
        out = cd.find_reconciled_production_watch(cid)

    assert out is not None
    assert out["watch_id"] == "42"
    assert out["keyword_expression"] == "alpha OR beta"


def test_find_reconciled_none_when_no_match(monkeypatch):
    with patch.object(cd, "list_watches", return_value=[]):
        assert cd.find_reconciled_production_watch("507f191e810c19729de860ea") is None


@pytest.mark.asyncio
async def test_register_skips_create_when_reconciled(monkeypatch):
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = {
        "_id": op_id,
        "product_extracted": {
            "suggested_keywords": {"tier_1": ["saas"], "tier_2": []},
        },
    }
    db = MagicMock()
    db.cofounders.find_one = AsyncMock(
        return_value={"_id": cf_id, "operator_id": op_id}
    )
    db.cofounders.update_one = AsyncMock()

    post_calls: list[object] = []

    def no_post(**_kwargs):
        post_calls.append(True)
        return {"id": "should-not-run"}

    monkeypatch.setattr(
        "app.routes.crustdata.find_reconciled_production_watch",
        lambda _cid: {
            "watch_id": "remote-77",
            "keyword_expression": "saas",
        },
    )
    monkeypatch.setattr(
        "app.routes.crustdata.webhook_url_for",
        lambda cid: f"https://h.test/w?cofounder_id={cid}&token=tok",
    )
    monkeypatch.setattr("app.routes.crustdata.register_keyword_watch", no_post)

    out = await register(
        str(cf_id),
        RegisterWatchRequest(),
        operator,
        db,
    )

    assert out.get("reconciled") is True
    assert out.get("watch_id") == "remote-77"
    assert post_calls == []
    db.cofounders.update_one.assert_awaited()


@pytest.mark.asyncio
async def test_register_calls_create_when_not_reconciled(monkeypatch):
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = {
        "_id": op_id,
        "product_extracted": {
            "suggested_keywords": {"tier_1": ["saas"], "tier_2": []},
        },
    }
    db = MagicMock()
    db.cofounders.find_one = AsyncMock(
        return_value={"_id": cf_id, "operator_id": op_id}
    )
    db.cofounders.update_one = AsyncMock()

    monkeypatch.setattr(
        "app.routes.crustdata.find_reconciled_production_watch",
        lambda _cid: None,
    )
    monkeypatch.setattr(
        "app.routes.crustdata.webhook_url_for",
        lambda cid: f"https://h.test/w?cofounder_id={cid}&token=tok",
    )
    monkeypatch.setattr(
        "app.routes.crustdata.register_keyword_watch",
        lambda **_k: {"id": "new-99"},
    )

    out = await register(
        str(cf_id),
        RegisterWatchRequest(),
        operator,
        db,
    )

    assert out.get("reconciled") is not True
    assert out.get("watch_id") == "new-99"
