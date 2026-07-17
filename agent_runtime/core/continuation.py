from __future__ import annotations

from typing import Any

from agent_runtime.contracts import TextBlock, ThinkingBlock, ToolCall, ToolResult


def encode_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    encoded = []
    for block in blocks:
        if isinstance(block, TextBlock):
            encoded.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingBlock):
            encoded.append(
                {"type": "thinking", "thinking": block.thinking,
                 "signature": block.signature}
            )
        elif isinstance(block, ToolCall):
            encoded.append(
                {"type": "tool_call", "id": block.id, "name": block.name,
                 "input": block.input}
            )
        elif isinstance(block, ToolResult):
            encoded.append(
                {"type": "tool_result", "tool_call_id": block.tool_call_id,
                 "content": block.content}
            )
        elif isinstance(block, dict):
            encoded.append({"type": "dict", "value": block})
        else:
            raise TypeError(f"Unsupported continuation block: {type(block).__name__}")
    return encoded


def decode_blocks(values: list[dict[str, Any]]) -> list[Any]:
    factories = {
        "text": lambda value: TextBlock(value["text"]),
        "thinking": lambda value: ThinkingBlock(
            value["thinking"], value.get("signature", "")
        ),
        "tool_call": lambda value: ToolCall(
            value["id"], value["name"], value.get("input", {})
        ),
        "tool_result": lambda value: ToolResult(
            value["tool_call_id"], value["content"]
        ),
        "dict": lambda value: value["value"],
    }
    return [factories[value["type"]](value) for value in values]
