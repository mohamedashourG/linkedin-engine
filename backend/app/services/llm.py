"""
LLM router. Picks an underlying provider (Anthropic Claude or OpenAI)
per model_tier and delegates `parse_structured*` calls there.

This is the only module the rest of the codebase should import. Direct
imports of `openai_client` or `anthropic_client` are an anti-pattern —
they leak provider choice into business logic. The router keeps both
backends installed so removing/adding a provider only touches config
(`.env`) without code edits.

Provider selection per tier:
  settings.llm_provider_<tier> ∈ {"anthropic", "openai", "auto"}
  - "anthropic": force Claude; raise AnthropicNotConfigured if no key.
  - "openai":    force OpenAI; raise OpenAINotConfigured if no key.
  - "auto":      Claude if ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is
                  set, otherwise OpenAI.

This file re-exports `parse_structured` (async) and `parse_structured_sync`
with the same kwargs the existing callsites already use, so the switch
is mechanical:

  -from app.services.openai_client import parse_structured_sync
  +from app.services.llm import parse_structured_sync
"""
from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _has_anthropic_creds() -> bool:
    """True when at least one Anthropic credential mode is configured.

    Mirrors the priority in anthropic_client._client_kwargs():
      - Azure AI Foundry (AZURE_CLAUDE_API_KEY + AZURE_CLAUDE_ENDPOINT)
      - Generic base URL + token (ANTHROPIC_BASE_URL + auth_token)
      - Direct Anthropic API key (ANTHROPIC_API_KEY)
    """
    if settings.azure_claude_api_key and settings.azure_claude_endpoint:
        return True
    if settings.anthropic_api_key:
        return True
    if settings.anthropic_auth_token:
        return True
    return False


def _resolve_provider(tier: str) -> str:
    """Resolve the configured provider for a tier into a concrete name.

    The master `LLM_USE_ANTHROPIC` switch short-circuits everything:
    when False, every tier resolves to "openai" regardless of the
    per-tier knobs or whether Anthropic credentials are configured.
    This is the one-line kill switch the operator can flip while
    debugging or while a Claude deployment is being provisioned.
    """
    if not settings.llm_use_anthropic:
        return "openai"

    raw = (
        settings.llm_provider_primary if tier == "primary"
        else settings.llm_provider_cheap if tier == "cheap"
        else "auto"
    )
    pref = (raw or "auto").strip().lower()
    if pref == "auto":
        return "anthropic" if _has_anthropic_creds() else "openai"
    if pref in ("anthropic", "claude", "azure-claude"):
        return "anthropic"
    if pref in ("openai", "azure", "gpt", "azure-openai"):
        return "openai"
    log.warning(
        "unknown llm_provider_%s=%r, falling back to openai", tier, raw,
    )
    return "openai"


def parse_structured_sync(
    *,
    model_tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_attempts: int = 3,
) -> T:
    """Synchronous structured-output call, routed by tier.

    Same signature as the previous `openai_client.parse_structured_sync`
    so switching the import is a no-op for callers.
    """
    provider = _resolve_provider(model_tier)
    if provider == "anthropic":
        from app.services.anthropic_client import parse_structured_sync as _anthropic
        return _anthropic(
            model_tier=model_tier,
            system=system,
            user=user,
            schema=schema,
            max_attempts=max_attempts,
        )
    from app.services.openai_client import parse_structured_sync as _openai
    return _openai(
        model_tier=model_tier,
        system=system,
        user=user,
        schema=schema,
        max_attempts=max_attempts,
    )


async def parse_structured(
    *,
    model_tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_attempts: int = 3,
) -> T:
    """Async sibling for non-Celery callers."""
    provider = _resolve_provider(model_tier)
    if provider == "anthropic":
        from app.services.anthropic_client import parse_structured as _anthropic
        return await _anthropic(
            model_tier=model_tier,
            system=system,
            user=user,
            schema=schema,
            max_attempts=max_attempts,
        )
    from app.services.openai_client import parse_structured as _openai
    return await _openai(
        model_tier=model_tier,
        system=system,
        user=user,
        schema=schema,
        max_attempts=max_attempts,
    )
