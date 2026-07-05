from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


ToolHandler = Callable[..., str]


class ToolRegistry:
    def __init__(self):
        self._tools: list[ToolSpec] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], str]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler):
        self._tools.append(spec)
        self._handlers[spec.name] = lambda args, h=handler: str(h(**args))

    def register_mcp_tools(
        self,
        server_name: str,
        tool_defs: list[dict[str, Any]],
        call_tool: Callable[[str, dict[str, Any]], str],
    ):
        safe_server = normalize_name(server_name)
        for tool_def in tool_defs:
            original_name = tool_def["name"]
            prefixed = f"mcp__{safe_server}__{normalize_name(original_name)}"
            self._tools.append(
                ToolSpec(
                    name=prefixed,
                    description=tool_def.get("description", ""),
                    input_schema=tool_def.get("inputSchema", {}),
                )
            )
            self._handlers[prefixed] = (
                lambda args, name=original_name: str(call_tool(name, args))
            )

    def assemble(self) -> tuple[list[ToolSpec], dict[str, Callable[[dict[str, Any]], str]]]:
        return list(self._tools), dict(self._handlers)


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)
