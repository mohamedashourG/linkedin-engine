"""Unit tests for the per-callsite `drafter` LLM tier.

These cover the routing decision in `app.services.llm._resolve_provider`
and the model-id selection in both provider clients. They do NOT hit any
external API — we patch `parse_structured_sync` in each underlying
provider module to capture the args it receives.

Why this matters: a stray edit that re-collapses `drafter` back into
`primary` would silently route ALL Claude-bound traffic to OpenAI (or
vice-versa). The test pins the contract so a regression is caught at CI
time, not when an operator finds their drafter still on GPT after
flipping LLM_PROVIDER_DRAFTER=anthropic.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest


# ─── _resolve_provider routing ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_llm_module():
    """Reload llm.py between tests so module-level settings caching is
    flushed. Most settings fields the router reads are evaluated on every
    call (not cached) but better to be explicit."""
    sys.modules.pop("app.services.llm", None)
    yield


def _set(settings, **kw):
    for k, v in kw.items():
        setattr(settings, k, v)


def test_drafter_tier_routes_to_openai_when_master_switch_off():
    """LLM_USE_ANTHROPIC=False short-circuits every tier to openai."""
    from app.config import settings
    from app.services import llm
    _set(
        settings,
        llm_use_anthropic=False,
        llm_provider_drafter="anthropic",  # even with anthropic forced
        llm_provider_primary="anthropic",
        anthropic_api_key="sk-test",
    )
    assert llm._resolve_provider("drafter") == "openai"
    assert llm._resolve_provider("primary") == "openai"
    assert llm._resolve_provider("cheap") == "openai"


def test_drafter_tier_falls_back_to_primary_when_set_to_auto():
    """Operators who don't touch LLM_PROVIDER_DRAFTER should see the same
    behavior as primary. This is the back-compat contract."""
    from app.config import settings
    from app.services import llm
    _set(
        settings,
        llm_use_anthropic=True,
        llm_provider_drafter="auto",
        llm_provider_primary="openai",
        anthropic_api_key="sk-test",
    )
    assert llm._resolve_provider("drafter") == "openai"
    _set(settings, llm_provider_primary="anthropic")
    assert llm._resolve_provider("drafter") == "anthropic"


def test_drafter_tier_overrides_primary_when_set_explicitly():
    """The key new capability: drafter -> Claude while primary stays on OpenAI."""
    from app.config import settings
    from app.services import llm
    # NOTE: must set every per-tier knob explicitly. The settings singleton
    # carries state across tests; leaving `llm_provider_cheap` unset would
    # inherit whatever a previous test left there.
    _set(
        settings,
        llm_use_anthropic=True,
        llm_provider_drafter="anthropic",
        llm_provider_primary="openai",
        llm_provider_cheap="openai",
        anthropic_api_key="sk-test",
    )
    assert llm._resolve_provider("drafter") == "anthropic"
    assert llm._resolve_provider("primary") == "openai"
    assert llm._resolve_provider("cheap") == "openai"


def test_drafter_tier_can_be_forced_to_openai_when_primary_is_anthropic():
    """Inverse case: drafter on OpenAI while primary on Claude."""
    from app.config import settings
    from app.services import llm
    _set(
        settings,
        llm_use_anthropic=True,
        llm_provider_drafter="openai",
        llm_provider_primary="anthropic",
        anthropic_api_key="sk-test",
    )
    assert llm._resolve_provider("drafter") == "openai"
    assert llm._resolve_provider("primary") == "anthropic"


# ─── _model_for tier handling in both clients ──────────────────────────


def test_anthropic_model_for_drafter_falls_back_to_primary():
    from app.config import settings
    from app.services import anthropic_client
    _set(
        settings,
        anthropic_model_primary="claude-opus-4-5-20251101",
        anthropic_model_cheap="claude-haiku-4-5-20251101",
        anthropic_model_drafter="",
    )
    assert anthropic_client._model_for("drafter") == "claude-opus-4-5-20251101"
    assert anthropic_client._model_for("primary") == "claude-opus-4-5-20251101"
    assert anthropic_client._model_for("cheap") == "claude-haiku-4-5-20251101"


def test_anthropic_model_for_drafter_honors_override():
    from app.config import settings
    from app.services import anthropic_client
    _set(
        settings,
        anthropic_model_primary="claude-opus-4-5-20251101",
        anthropic_model_drafter="claude-sonnet-4-5-20251101",
    )
    assert anthropic_client._model_for("drafter") == "claude-sonnet-4-5-20251101"
    # Primary is unaffected
    assert anthropic_client._model_for("primary") == "claude-opus-4-5-20251101"


def test_anthropic_model_for_rejects_unknown_tier():
    from app.services import anthropic_client
    with pytest.raises(ValueError, match="'primary', 'cheap', or 'drafter'"):
        anthropic_client._model_for("rogue")


def test_openai_model_for_sync_drafter_falls_back_and_overrides():
    from app.config import settings
    from app.services import openai_client
    _set(
        settings,
        openai_model_primary="gpt-5.4",
        openai_model_cheap="gpt-5.4-mini",
        openai_model_drafter="",
        azure_openai_endpoint="",  # force "openai" provider path
        openai_api_key="sk-test",
    )
    # Force the cached client onto the openai path. _get_sync_client returns
    # a tuple (client, provider_str); we only need provider correctness.
    with patch.object(
        openai_client, "_get_sync_client", return_value=(object(), "openai")
    ):
        assert openai_client._model_for_sync("drafter") == "gpt-5.4"

        _set(settings, openai_model_drafter="gpt-5.4-comments")
        assert openai_client._model_for_sync("drafter") == "gpt-5.4-comments"
        # primary unaffected
        assert openai_client._model_for_sync("primary") == "gpt-5.4"


# ─── Drafter callsite uses the new tier ────────────────────────────────


def test_drafter_callsite_uses_drafter_tier():
    """End-to-end: when draft_comment runs, the LLM router sees
    model_tier='drafter', not 'primary'. Patch parse_structured_sync so
    we can read what tier the drafter passed in."""
    from app.engine.stages import drafter as drafter_mod

    captured: dict[str, str] = {}

    class _FakeDraft:
        comment = "Stub comment from the fake LLM."

    def _capture(**kwargs):
        captured["model_tier"] = kwargs["model_tier"]
        return _FakeDraft()

    with patch.object(drafter_mod, "parse_structured_sync", side_effect=_capture):
        text, _formula = drafter_mod.draft_comment(
            cofounder_name="Nicolas",
            cofounder_tone="terse",
            cofounder_examples=[],
            post_text="Hypertension control is the cleanest signal of operator focus.",
            author_name="Test Author",
            author_title="CMO",
            author_company="Test Co",
            icp_score=8,
            comment_type="A",
            source_classification="A",
            reframe_formula="X is the cleanest test of Y",
        )

    assert captured["model_tier"] == "drafter"
    assert text == "Stub comment from the fake LLM."
