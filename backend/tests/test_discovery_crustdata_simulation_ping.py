"""Crustdata simulation ping after empty inbox drain in discovery."""

from __future__ import annotations

from bson import ObjectId

from app.engine.stages import discovery


def _operator_with_keywords(op_id: ObjectId) -> dict:
    return {
        "_id": op_id,
        "product_description": "x",
        "product_extracted": {
            "suggested_keywords": {"tier_1": ["saas"], "tier_2": ["b2b"]},
        },
    }


def _fake_db_empty_inbox():
    class _FakeCursor:
        def sort(self, *_a, **_k):
            return self

        def __iter__(self):
            return iter([])

    class _FakeColl:
        def find(self, *_a, **_kw):
            return _FakeCursor()

        def insert_one(self, _d):
            class _R:
                inserted_id = ObjectId()

            return _R()

        def update_many(self, *_a, **_kw):
            class _R:
                modified_count = 0

            return _R()

        def aggregate(self, *_a, **_kw):
            return iter([])

        def count_documents(self, *_a, **_kw):
            return 0

        def find_one(self, *_a, **_kw):
            return None

    class _FakeDB:
        def __init__(self):
            self.discovery_seeds = _FakeColl()
            self.candidates = _FakeColl()
            self.audit_records = _FakeColl()
            self.keyword_history = _FakeColl()
            self.exhaustion_ledger = _FakeColl()
            self.crustdata_inbox = _FakeColl()
            self.profiles = _FakeColl()

    return _FakeDB()


def test_simulation_ping_called_when_empty_inbox_and_enabled(monkeypatch):
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = _operator_with_keywords(op_id)
    cofounder = {
        "_id": cf_id,
        "display_name": "c",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {
            "tone_description": "t" * 30,
            "examples": [{"post": "p", "comment": "c"}],
        },
    }

    calls: list[tuple[tuple, dict]] = []

    def fake_register(*args, **kwargs):
        calls.append((args, kwargs))
        return {"watch_id": "sim-1"}

    monkeypatch.setattr(
        "app.services.crustdata.register_keyword_watch",
        fake_register,
    )
    monkeypatch.setattr(discovery, "_run_apidirect", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_exa", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_unipile", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_unipile_title_search", lambda **_k: 0)

    monkeypatch.setattr(
        discovery,
        "settings",
        discovery.settings.model_copy(
            update={
                "discovery_use_crustdata": True,
                "discovery_crustdata_simulation_ping_on_empty_inbox": True,
                "crustdata_api_key": "test-key",
                "crustdata_webhook_secret": "secret",
                "crustdata_webhook_base_url": "https://example.com",
                "discovery_use_apidirect": False,
                "discovery_use_exa": False,
                "discovery_use_unipile": False,
                "discovery_use_crustdata_screener": False,
            }
        ),
    )

    discovery.discover_for_operator(
        _fake_db_empty_inbox(),
        operator=operator,
        cofounders=[cofounder],
        slate_run_id=ObjectId(),
    )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ()
    assert kwargs["cofounder_id"] == str(cf_id)
    assert kwargs.get("simulation") is True


def test_simulation_ping_not_called_when_flag_false(monkeypatch):
    """Standalone discover calls can disable ping (e.g. daily_run top-up rounds)."""
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = _operator_with_keywords(op_id)
    cofounder = {
        "_id": cf_id,
        "display_name": "c",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {
            "tone_description": "t" * 30,
            "examples": [{"post": "p", "comment": "c"}],
        },
    }

    calls: list[tuple] = []

    def fake_register(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(
        "app.services.crustdata.register_keyword_watch",
        fake_register,
    )
    monkeypatch.setattr(discovery, "_run_apidirect", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_exa", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_unipile", lambda **_k: 0)

    monkeypatch.setattr(
        discovery,
        "settings",
        discovery.settings.model_copy(
            update={
                "discovery_use_crustdata": True,
                "discovery_crustdata_simulation_ping_on_empty_inbox": True,
                "crustdata_api_key": "test-key",
                "crustdata_webhook_secret": "secret",
                "crustdata_webhook_base_url": "https://example.com",
                "discovery_use_apidirect": False,
                "discovery_use_exa": False,
                "discovery_use_unipile": False,
                "discovery_use_crustdata_screener": False,
            }
        ),
    )

    discovery.discover_for_operator(
        _fake_db_empty_inbox(),
        operator=operator,
        cofounders=[cofounder],
        slate_run_id=ObjectId(),
        crustdata_simulation_ping=False,
    )

    assert calls == []


def test_simulation_ping_not_called_when_disabled(monkeypatch):
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = _operator_with_keywords(op_id)
    cofounder = {
        "_id": cf_id,
        "display_name": "c",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {
            "tone_description": "t" * 30,
            "examples": [{"post": "p", "comment": "c"}],
        },
    }

    calls: list[tuple] = []

    def fake_register(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(
        "app.services.crustdata.register_keyword_watch",
        fake_register,
    )
    monkeypatch.setattr(discovery, "_run_apidirect", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_exa", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_unipile", lambda **_k: 0)

    monkeypatch.setattr(
        discovery,
        "settings",
        discovery.settings.model_copy(
            update={
                "discovery_use_crustdata": True,
                "discovery_crustdata_simulation_ping_on_empty_inbox": False,
                "crustdata_api_key": "test-key",
                "crustdata_webhook_secret": "secret",
                "crustdata_webhook_base_url": "https://example.com",
                "discovery_use_apidirect": False,
                "discovery_use_exa": False,
                "discovery_use_unipile": False,
                "discovery_use_crustdata_screener": False,
            }
        ),
    )

    discovery.discover_for_operator(
        _fake_db_empty_inbox(),
        operator=operator,
        cofounders=[cofounder],
        slate_run_id=ObjectId(),
    )

    assert calls == []


def test_simulation_ping_not_called_when_inbox_nonempty(monkeypatch):
    op_id = ObjectId()
    cf_id = ObjectId()
    operator = _operator_with_keywords(op_id)
    cofounder = {
        "_id": cf_id,
        "display_name": "c",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {
            "tone_description": "t" * 30,
            "examples": [{"post": "p", "comment": "c"}],
        },
    }

    calls: list[tuple] = []

    def fake_register(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(
        "app.services.crustdata.register_keyword_watch",
        fake_register,
    )
    monkeypatch.setattr(discovery, "_drain_crustdata_inbox", lambda *_a, **_k: 1)
    monkeypatch.setattr(discovery, "_run_apidirect", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_exa", lambda **_k: 0)
    monkeypatch.setattr(discovery, "_run_unipile", lambda **_k: 0)

    monkeypatch.setattr(
        discovery,
        "settings",
        discovery.settings.model_copy(
            update={
                "discovery_use_crustdata": True,
                "discovery_crustdata_simulation_ping_on_empty_inbox": True,
                "crustdata_api_key": "test-key",
                "crustdata_webhook_secret": "secret",
                "crustdata_webhook_base_url": "https://example.com",
                "discovery_use_apidirect": False,
                "discovery_use_exa": False,
                "discovery_use_unipile": False,
                "discovery_use_crustdata_screener": False,
            }
        ),
    )

    discovery.discover_for_operator(
        _fake_db_empty_inbox(),
        operator=operator,
        cofounders=[cofounder],
        slate_run_id=ObjectId(),
    )

    assert calls == []
