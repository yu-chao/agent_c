from __future__ import annotations

from typing import Any

from agent.models.anthropic import AnthropicProvider
from agent.models.openai import OpenAIProvider
from agent.models.resilient import ResilientModelProvider, RetryPolicy


def create_model_provider(
    clients: dict[str, Any] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
    retry_policy: RetryPolicy | None = None,
):
    clients = clients or {}
    provider = provider or 'openai'
    if provider == 'openai':
        primary = OpenAIProvider(
            client=clients.get('openai'),
            model=model or 'gpt-5',
        )
    elif provider == 'anthropic':
        primary = AnthropicProvider(
            client=clients.get('anthropic'),
            model=model or 'claude-sonnet-4-20250514',
        )
    else:
        raise ValueError(f'Unknown model provider: {provider}')
    if retry_policy is None and fallback_provider is None:
        return primary
    fallback = None
    if fallback_provider is not None:
        fallback = create_model_provider(
            clients,
            provider=fallback_provider,
            model=fallback_model,
        )
    return ResilientModelProvider(
        primary, fallback=fallback, retry_policy=retry_policy
    )
