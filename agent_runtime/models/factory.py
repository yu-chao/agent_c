from __future__ import annotations

import os
from typing import Any

from agent_runtime.models.anthropic import AnthropicProvider
from agent_runtime.models.openai import OpenAIProvider


def create_model_provider(
    clients: dict[str, Any] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
):
    clients = clients or {}
    provider = (
        provider
        or os.getenv('AGENT_MODEL_PROVIDER')
        or os.getenv('MODEL_PROVIDER')
        or os.getenv('provider')
        or 'openai'
    )
    model = model or os.getenv('AGENT_MODEL_ID') or os.getenv('MODEL_ID')
    if provider == 'openai':
        return OpenAIProvider(
            client=clients.get('openai'),
            model=model or 'gpt-5',
        )
    if provider == 'anthropic':
        return AnthropicProvider(
            client=clients.get('anthropic'),
            model=model or 'claude-sonnet-4-20250514',
        )
    raise ValueError(f'Unknown model provider: {provider}')
