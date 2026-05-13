"""
Typed wrapper that talks to either direct OpenAI or Azure OpenAI.

Spec maps "primary" / "cheap" tiers onto:
  - direct OpenAI: settings.openai_model_primary / openai_model_cheap (model ids)
  - Azure OpenAI:  settings.azure_openai_deployment_primary / _cheap (deployment names)

Selection: Azure wins if AZURE_OPENAI_KEY + AZURE_OPENAI_ENDPOINT are set, else
direct OpenAI if OPENAI_API_KEY is set, else OpenAINotConfigured.

Structured outputs go through `client.beta.chat.completions.parse(...)` because
that path is supported on both surfaces; the Responses API parity on Azure is
still preview-only and version-gated.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal, TypeVar

import time

from openai import (
    APIConnectionError,
    APIError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    AzureOpenAI,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class OpenAINotConfigured(RuntimeError):
    """Neither Azure nor direct OpenAI credentials are configured."""


_client: AsyncOpenAI | AsyncAzureOpenAI | None = None
_provider: Literal["azure", "openai"] | None = None


def _get_client() -> tuple[AsyncOpenAI | AsyncAzureOpenAI, Literal["azure", "openai"]]:
    global _client, _provider
    if _client is not None and _provider is not None:
        return _client, _provider

    if settings.azure_openai_key and settings.azure_openai_endpoint:
        _client = AsyncAzureOpenAI(
            api_key=settings.azure_openai_key,
            api_version=settings.azure_openai_api_version,
            azure_endpoint=settings.azure_openai_endpoint,
        )
        _provider = "azure"
        log.info("using Azure OpenAI (%s)", settings.azure_openai_endpoint)
    elif settings.openai_api_key:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
        _provider = "openai"
        log.info("using direct OpenAI")
    else:
        raise OpenAINotConfigured(
            "No OpenAI credentials configured. Set AZURE_OPENAI_KEY+AZURE_OPENAI_ENDPOINT "
            "or OPENAI_API_KEY."
        )
    return _client, _provider


def _model_for(tier: str) -> str:
    if tier not in ("primary", "cheap", "drafter"):
        raise ValueError(f"tier must be 'primary', 'cheap', or 'drafter', got {tier!r}")
    _, provider = _get_client()
    # Drafter falls back to primary's deployment/model when not overridden;
    # operators get identical behavior without setting the new knob.
    if tier == "drafter":
        if provider == "azure":
            # Azure model knob isn't split per-callsite — drafter uses the
            # primary deployment unless openai_model_drafter is set
            # explicitly (rare; only useful when the operator wants Claude
            # via OpenAI-compatible proxy on a different model).
            return (
                settings.openai_model_drafter
                or settings.azure_openai_deployment_primary
            )
        return (
            settings.openai_model_drafter
            or settings.openai_model_primary
        )
    if provider == "azure":
        return (
            settings.azure_openai_deployment_primary
            if tier == "primary"
            else settings.azure_openai_deployment_cheap
        )
    return (
        settings.openai_model_primary
        if tier == "primary"
        else settings.openai_model_cheap
    )


async def parse_structured(
    *,
    model_tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_attempts: int = 3,
) -> T:
    """
    Call chat.completions.parse with a Pydantic schema and return a parsed instance.
    """
    client, _ = _get_client()
    model = _model_for(model_tier)

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
            )
            choice = response.choices[0]
            if choice.message.refusal:
                raise RuntimeError(f"Model refused: {choice.message.refusal}")
            parsed = choice.message.parsed
            if parsed is None:
                raise RuntimeError("chat.completions.parse returned no parsed output.")
            return parsed
        except (APIConnectionError, RateLimitError) as err:
            last_err = err
            backoff = 0.5 * (2 ** (attempt - 1))
            log.warning(
                "openai transient error (%s), retrying in %.1fs (attempt %d/%d)",
                err.__class__.__name__,
                backoff,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(backoff)
        except APIError as err:
            log.error("openai api error (non-retryable): %s", err)
            raise

    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------- sync variant

_sync_client: OpenAI | AzureOpenAI | None = None
_sync_provider: Literal["azure", "openai"] | None = None


def _get_sync_client() -> tuple[OpenAI | AzureOpenAI, Literal["azure", "openai"]]:
    global _sync_client, _sync_provider
    if _sync_client is not None and _sync_provider is not None:
        return _sync_client, _sync_provider

    if settings.azure_openai_key and settings.azure_openai_endpoint:
        _sync_client = AzureOpenAI(
            api_key=settings.azure_openai_key,
            api_version=settings.azure_openai_api_version,
            azure_endpoint=settings.azure_openai_endpoint,
        )
        _sync_provider = "azure"
    elif settings.openai_api_key:
        _sync_client = OpenAI(api_key=settings.openai_api_key)
        _sync_provider = "openai"
    else:
        raise OpenAINotConfigured(
            "No OpenAI credentials configured. Set AZURE_OPENAI_KEY+AZURE_OPENAI_ENDPOINT "
            "or OPENAI_API_KEY."
        )
    return _sync_client, _sync_provider


def _model_for_sync(tier: str) -> str:
    if tier not in ("primary", "cheap", "drafter"):
        raise ValueError(f"tier must be 'primary', 'cheap', or 'drafter', got {tier!r}")
    _, provider = _get_sync_client()
    # Drafter falls back to primary's deployment/model when not overridden.
    if tier == "drafter":
        if provider == "azure":
            return (
                settings.openai_model_drafter
                or settings.azure_openai_deployment_primary
            )
        return (
            settings.openai_model_drafter
            or settings.openai_model_primary
        )
    if provider == "azure":
        return (
            settings.azure_openai_deployment_primary
            if tier == "primary"
            else settings.azure_openai_deployment_cheap
        )
    return (
        settings.openai_model_primary
        if tier == "primary"
        else settings.openai_model_cheap
    )


def parse_structured_sync(
    *,
    model_tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_attempts: int = 3,
) -> T:
    """Sync version for use inside Celery tasks."""
    client, _ = _get_sync_client()
    model = _model_for_sync(model_tier)

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
            )
            choice = response.choices[0]
            if choice.message.refusal:
                raise RuntimeError(f"Model refused: {choice.message.refusal}")
            parsed = choice.message.parsed
            if parsed is None:
                raise RuntimeError("chat.completions.parse returned no parsed output.")
            return parsed
        except (APIConnectionError, RateLimitError) as err:
            last_err = err
            backoff = 0.5 * (2 ** (attempt - 1))
            log.warning(
                "openai transient error (%s), retrying in %.1fs (attempt %d/%d)",
                err.__class__.__name__,
                backoff,
                attempt,
                max_attempts,
            )
            time.sleep(backoff)
        except APIError as err:
            log.error("openai api error (non-retryable): %s", err)
            raise

    assert last_err is not None
    raise last_err
