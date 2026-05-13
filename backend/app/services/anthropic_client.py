"""
Anthropic Claude wrapper that matches the openai_client.parse_structured*
surface so the LLM router can swap providers without callsite changes.

Claude doesn't have a native "structured output / response_format=Pydantic"
mode like OpenAI's `chat.completions.parse`. The portable pattern is to
declare a Pydantic schema as a single tool, instruct Claude to ALWAYS call
it, and parse the resulting tool_use input back into the Pydantic model.
This is the same approach the Anthropic team recommends for typed JSON
output and works across direct Anthropic, Azure AI Foundry, and Bedrock.

Configuration:
  - settings.anthropic_api_key OR anthropic_auth_token: credentials.
  - settings.anthropic_base_url: optional override (Azure AI Foundry sets
    this to a Foundry-hosted endpoint that proxies the Messages API).
  - settings.anthropic_model_primary / _cheap: deployment / model ids.

Retries: APIConnectionError / RateLimitError get exponential backoff,
mirroring the openai_client behavior so the router can treat the two
providers interchangeably.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, TypeVar

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)
from pydantic import BaseModel

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class AnthropicNotConfigured(RuntimeError):
    """Neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set."""


# Cached singleton clients. The Anthropic SDK is thread-safe; reuse the
# same client across worker threads to amortize connection setup.
_client: Anthropic | None = None
_async_client: AsyncAnthropic | None = None


def _client_kwargs() -> dict[str, Any]:
    """Build SDK kwargs from settings. Three credential modes (in priority
    order):

      1. Azure AI Foundry (AZURE_CLAUDE_API_KEY + AZURE_CLAUDE_ENDPOINT) —
         most common for our deployment. Points the SDK at the Foundry
         endpoint and injects Azure's `api-key` header. We still pass the
         Azure key as `api_key` so the SDK's `Authorization: Bearer` header
         is also set; some Foundry endpoints accept either, and having
         both makes us forwards-compatible with future header changes.

      2. Generic Anthropic-compatible base URL (ANTHROPIC_BASE_URL +
         ANTHROPIC_AUTH_TOKEN OR ANTHROPIC_API_KEY).

      3. Direct Anthropic (ANTHROPIC_API_KEY only — no base URL).
    """
    kwargs: dict[str, Any] = {}

    if settings.azure_claude_api_key and settings.azure_claude_endpoint:
        # Trim a trailing slash so the SDK can append /v1/messages cleanly.
        kwargs["base_url"] = settings.azure_claude_endpoint.rstrip("/")
        kwargs["api_key"] = settings.azure_claude_api_key
        # Azure-style header. The SDK passes through default_headers on
        # every request, so the Foundry gateway sees `api-key: <key>` in
        # addition to whatever the SDK adds for the underlying provider.
        kwargs["default_headers"] = {"api-key": settings.azure_claude_api_key}
        return kwargs

    if settings.anthropic_base_url:
        kwargs["base_url"] = settings.anthropic_base_url
    if settings.anthropic_api_key:
        kwargs["api_key"] = settings.anthropic_api_key
    elif settings.anthropic_auth_token:
        kwargs["auth_token"] = settings.anthropic_auth_token
    else:
        raise AnthropicNotConfigured(
            "Set AZURE_CLAUDE_API_KEY + AZURE_CLAUDE_ENDPOINT, or "
            "ANTHROPIC_API_KEY, or ANTHROPIC_AUTH_TOKEN to enable Claude."
        )
    return kwargs


def _get_sync_client() -> Anthropic:
    global _client
    if _client is not None:
        return _client
    _client = Anthropic(**_client_kwargs())
    log.info(
        "using Anthropic Claude (base_url=%s)",
        settings.anthropic_base_url or "api.anthropic.com",
    )
    return _client


def _get_async_client() -> AsyncAnthropic:
    global _async_client
    if _async_client is not None:
        return _async_client
    _async_client = AsyncAnthropic(**_client_kwargs())
    return _async_client


def _model_for(tier: str) -> str:
    if tier == "primary":
        return settings.anthropic_model_primary
    if tier == "cheap":
        return settings.anthropic_model_cheap
    raise ValueError(f"tier must be 'primary' or 'cheap', got {tier!r}")


def _schema_to_tool(schema: type[BaseModel]) -> dict[str, Any]:
    """Build a single Anthropic tool that wraps the Pydantic schema. The
    tool name and description are fixed; we only care about the input
    schema being the Pydantic model's JSON schema. Setting
    tool_choice={"type": "tool", "name": ...} forces Claude to call it.
    """
    json_schema = schema.model_json_schema()
    return {
        "name": "emit_result",
        "description": (
            "Emit the structured response. Always call this tool exactly "
            "once with arguments that match the schema. Do not return prose."
        ),
        "input_schema": json_schema,
    }


def _parse_tool_use(response: Any, schema: type[T]) -> T:
    """Walk Claude's content blocks for the tool_use call and validate
    its `input` dict against the Pydantic schema. Raises if Claude
    refused or didn't call the tool.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("claude refused the request")
    for block in response.content or []:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_result":
            data = block.input
            if isinstance(data, str):
                # Some Foundry endpoints serialize tool inputs as JSON strings.
                data = json.loads(data)
            return schema.model_validate(data)
    # Fallback: the model emitted prose. Try to coerce the first text
    # block as JSON for the schema — useful when Foundry strips tool_use.
    for block in response.content or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return schema.model_validate(json.loads(text))
        except Exception:
            continue
    raise RuntimeError(
        "claude did not emit a tool_use call for 'emit_result' "
        "and no parseable JSON text fallback was found"
    )


def parse_structured_sync(
    *,
    model_tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_attempts: int = 3,
    max_tokens: int = 4096,
) -> T:
    """Sync structured-output call to Claude. Mirrors openai_client's
    signature so the LLM router can delegate without rewriting callsites.
    """
    client = _get_sync_client()
    model = _model_for(model_tier)
    tool = _schema_to_tool(schema)

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_result"},
            )
            return _parse_tool_use(response, schema)
        except (APIConnectionError, RateLimitError) as err:
            last_err = err
            backoff = 0.5 * (2 ** (attempt - 1))
            log.warning(
                "anthropic transient error (%s), retrying in %.1fs (attempt %d/%d)",
                err.__class__.__name__,
                backoff,
                attempt,
                max_attempts,
            )
            time.sleep(backoff)
        except APIStatusError as err:
            # 5xx is transient; 4xx is a contract failure we shouldn't retry.
            if err.status_code and 500 <= err.status_code < 600:
                last_err = err
                backoff = 0.5 * (2 ** (attempt - 1))
                log.warning(
                    "anthropic 5xx (%d), retrying in %.1fs",
                    err.status_code,
                    backoff,
                )
                time.sleep(backoff)
                continue
            log.error("anthropic api error (non-retryable): %s", err)
            raise
        except APIError as err:
            log.error("anthropic api error (non-retryable): %s", err)
            raise

    assert last_err is not None
    raise last_err


async def parse_structured(
    *,
    model_tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_attempts: int = 3,
    max_tokens: int = 4096,
) -> T:
    """Async sibling for routes / non-Celery callsites."""
    import asyncio

    client = _get_async_client()
    model = _model_for(model_tier)
    tool = _schema_to_tool(schema)

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_result"},
            )
            return _parse_tool_use(response, schema)
        except (APIConnectionError, RateLimitError) as err:
            last_err = err
            backoff = 0.5 * (2 ** (attempt - 1))
            log.warning(
                "anthropic transient error (%s), retrying in %.1fs (attempt %d/%d)",
                err.__class__.__name__,
                backoff,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(backoff)
        except APIStatusError as err:
            if err.status_code and 500 <= err.status_code < 600:
                last_err = err
                backoff = 0.5 * (2 ** (attempt - 1))
                await asyncio.sleep(backoff)
                continue
            raise
        except APIError as err:
            raise

    assert last_err is not None
    raise last_err
