from __future__ import annotations

import json
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


class OpenAIProvider:
    """OpenAI Responses API adapter."""

    provider = 'openai'

    def __init__(self, client: Any | None = None, model: str = "gpt-5"):
        self.model = model
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client = client

    def convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        converted = []
        for tool in tools:
            converted.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": True,
                }
            )
        return converted

    def generate(self, request: ModelRequest) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": request.system,
            "input": self._convert_messages(request.messages),
            "tools": self.convert_tools(request.tools),
            "max_output_tokens": request.max_tokens,
        }
        if request.previous_response_id:
            kwargs["previous_response_id"] = request.previous_response_id

        try:
            response = self.client.responses.create(**kwargs)
        except Exception as error:
            raise classify_model_error(error) from error
        return ModelResponse(
            blocks=self._parse_output(getattr(response, "output", [])),
            response_id=getattr(response, "id", None),
            input_tokens=int(
                _get(getattr(response, "usage", None), "input_tokens") or 0
            ),
            output_tokens=int(
                _get(getattr(response, "usage", None), "output_tokens") or 0
            ),
        )

    def _convert_messages(self, messages: list[MessageBlock | dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for message in messages:
            if isinstance(message, ToolResult):
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content,
                    }
                )
            elif isinstance(message, ThinkingBlock):
                # OpenAI Responses API has no equivalent; preserve by skipping.
                continue
            elif isinstance(message, TextBlock):
                converted.append({"role": "assistant", "content": message.text})
            elif isinstance(message, ToolCall):
                converted.append(
                    {
                        "type": "function_call",
                        "call_id": message.id,
                        "name": message.name,
                        "arguments": json.dumps(message.input),
                    }
                )
            elif isinstance(message, dict):
                converted.append(message)
            else:
                converted.append({"role": "user", "content": str(message)})
        return converted

    def _parse_output(self, output: list[Any]) -> list[TextBlock | ThinkingBlock | ToolCall]:
        blocks: list[TextBlock | ThinkingBlock | ToolCall] = []
        for item in output:
            item_type = _get(item, "type")
            if item_type == "function_call":
                blocks.append(
                    ToolCall(
                        id=_get(item, "call_id") or _get(item, "id") or "",
                        name=_get(item, "name") or "",
                        input=_loads_json_object(_get(item, "arguments") or "{}"),
                    )
                )
                continue
            if item_type == "message":
                for content in _get(item, "content") or []:
                    if _get(content, "type") in ("output_text", "text"):
                        blocks.append(TextBlock(text=_get(content, "text") or ""))
        return blocks


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _loads_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
