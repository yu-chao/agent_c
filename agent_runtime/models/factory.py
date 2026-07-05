from __future__ import annotations

from typing import Any

from agent_runtime.models.anthropic import AnthropicProvider
from agent_runtime.models.openai import OpenAIProvider


def create_model_provider(config: dict[str, Any], clients: dict[str, Any] | None = None):
    clients = clients or {}
    model_config = config.get("model", {})
    provider = model_config.get("provider", "anthropic").lower()
    model = model_config.get("name")
    if provider == "openai":
        return OpenAIProvider(client=clients.get("openai"), model=model or "gpt-5")
    if provider == "anthropic":
        return AnthropicProvider(client=clients.get("anthropic"), model=model or "claude-sonnet-4-20250514")
    raise ValueError(f"Unknown model provider: {provider}")
