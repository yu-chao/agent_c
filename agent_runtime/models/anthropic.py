from __future__ import annotations

import os
from typing import Any

from agent_runtime.models.base import (
    MessageBlock,
    ModelRequest,
    ModelResponse,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
)
from agent_runtime.models.errors import classify_model_error


class AnthropicProvider:
    """Anthropic Messages API adapter."""

    provider = 'anthropic'

    def __init__(self, client: Any | None = None, model: str = "claude-sonnet-4-20250514"):
        self.model = model
        if client is None:
            from anthropic import Anthropic

            client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
        self.client = client

    def convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]

    def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            response = self.client.messages.create(
                model=self.model,
                system=request.system,
                messages=self._convert_messages(request.messages),
                tools=self.convert_tools(request.tools),
                max_tokens=request.max_tokens,
            )
        except Exception as error:
            raise classify_model_error(error) from error
        return ModelResponse(blocks=self._parse_content(response.content))

    def _convert_messages(self, messages: list[MessageBlock | dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        assistant_buffer: list[dict[str, Any]] = []

        def flush_assistant() -> None:
            nonlocal assistant_buffer
            if assistant_buffer:
                converted.append({"role": "assistant", "content": assistant_buffer})
                assistant_buffer = []

        for message in messages:
            if isinstance(message, ToolResult):
                flush_assistant()
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content,
                            }
                        ],
                    }
                )
            elif isinstance(message, ThinkingBlock):
                thinking_block: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": message.thinking,
                }
                if message.signature:
                    thinking_block["signature"] = message.signature
                assistant_buffer.append(thinking_block)
            elif isinstance(message, TextBlock):
                assistant_buffer.append({"type": "text", "text": message.text})
            elif isinstance(message, ToolCall):
                assistant_buffer.append(
                    {
                        "type": "tool_use",
                        "id": message.id,
                        "name": message.name,
                        "input": message.input,
                    }
                )
            elif isinstance(message, dict):
                flush_assistant()
                converted.append(message)
            else:
                flush_assistant()
                converted.append({"role": "user", "content": str(message)})

        flush_assistant()
        return converted

    def _parse_content(self, content: list[Any]) -> list[TextBlock | ThinkingBlock | ToolCall]:
        blocks: list[TextBlock | ThinkingBlock | ToolCall] = []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                blocks.append(TextBlock(getattr(block, "text", "")))
            elif block_type == "thinking":
                blocks.append(
                    ThinkingBlock(
                        thinking=getattr(block, "thinking", ""),
                        signature=getattr(block, "signature", "") or "",
                    )
                )
            elif block_type == "tool_use":
                blocks.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=getattr(block, "input", {}) or {},
                    )
                )
        return blocks
