from __future__ import annotations

from typing import Any

from agent_runtime.tools import ToolRegistry


class MockMCPHub:
    def connect(self, name: str) -> ToolRegistry:
        registry = ToolRegistry()
        if name == "docs":
            registry.register_mcp_tools(
                "docs",
                [
                    {
                        "name": "search",
                        "description": "Search documentation. (readOnly)",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    }
                ],
                self._call_docs,
            )
            return registry
        if name == "deploy":
            registry.register_mcp_tools(
                "deploy",
                [
                    {
                        "name": "status",
                        "description": "Check deployment status. (readOnly)",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"service": {"type": "string"}},
                            "required": ["service"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "trigger",
                        "description": "Trigger a deployment. (destructive)",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"service": {"type": "string"}},
                            "required": ["service"],
                            "additionalProperties": False,
                        },
                    },
                ],
                self._call_deploy,
            )
            return registry
        raise ValueError("Unknown MCP server: " + name)

    def _call_docs(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "search":
            return f"[docs] Found results for '{args['query']}'"
        return "MCP error: unknown docs tool"

    def _call_deploy(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "status":
            return f"[deploy] {args['service']}: running"
        if tool_name == "trigger":
            return f"[deploy] Triggered: {args['service']}"
        return "MCP error: unknown deploy tool"
