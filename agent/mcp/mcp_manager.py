from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from agent.tools import ToolRegistry


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    type: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 50
    sse_read_timeout: float = 50

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MCPServerConfig":
        headers = {
            str(key): _expand_env(str(header_value))
            for key, header_value in value.get("headers", {}).items()
        }
        return cls(
            name=value["name"],
            type=value.get("type", "streamable-http"),
            url=_expand_env(value["url"]),
            headers=headers,
            timeout=float(value.get("timeout", 50)),
            sse_read_timeout=float(value.get("sse_read_timeout", 50)),
        )


class StreamableHTTPMCPClient:
    """Synchronous adapter for an MCP Streamable HTTP server."""

    def __init__(self, config: MCPServerConfig):
        if config.type != "streamable-http":
            raise ValueError(f"Unsupported MCP transport: {config.type}")
        self.config = config
        self._session_id: str | None = None
        self._protocol_version = "2025-06-18"
        self._request_id = 0
        self._initialized = False
        self._lock = threading.RLock()

    def list_tools(self) -> list[dict[str, Any]]:
        with self._lock:
            self._initialize()
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            while True:
                params = {"cursor": cursor} if cursor else {}
                result = self._rpc("tools/list", params)
                tools.extend(result.get("tools", []))
                cursor = result.get("nextCursor")
                if not cursor:
                    return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        with self._lock:
            self._initialize()
            result = self._rpc(
                "tools/call", {"name": tool_name, "arguments": arguments}
            )
            return _render_tool_result(result)

    def _initialize(self) -> None:
        if self._initialized:
            return
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "agent-runtime", "version": "0.1.0"},
            },
            include_protocol_header=False,
        )
        self._protocol_version = result.get("protocolVersion", self._protocol_version)
        self._notification("notifications/initialized")
        self._initialized = True

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        include_protocol_header: bool = True,
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response = asyncio.run(self._post(message, include_protocol_header))
        if response.get("error"):
            error = response["error"]
            raise RuntimeError(
                f"MCP {method} failed ({error.get('code')}): {error.get('message')}"
            )
        if response.get("id") != request_id:
            raise RuntimeError(f"MCP {method} returned an unexpected response id")
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    def _notification(self, method: str) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        asyncio.run(self._post(message, True, expect_response=False))

    async def _post(
        self,
        message: dict[str, Any],
        include_protocol_header: bool,
        expect_response: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.config.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if include_protocol_header:
            headers["MCP-Protocol-Version"] = self._protocol_version

        timeout = aiohttp.ClientTimeout(
            total=self.config.timeout, sock_read=self.config.sse_read_timeout
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.config.url, headers=headers, json=message
            ) as response:
                response.raise_for_status()
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                body = await response.text()
                if not expect_response:
                    return {}
                if not body.strip():
                    raise RuntimeError("MCP server returned an empty response")
                return _decode_response(
                    body, response.headers.get("Content-Type", "")
                )


class MCPHub:
    def __init__(self, servers: list[MCPServerConfig]):
        self._servers = {server.name: server for server in servers}
        self._clients: dict[str, StreamableHTTPMCPClient] = {}

    @classmethod
    def from_servers(cls, servers: tuple[dict[str, Any], ...]) -> 'MCPHub':
        return cls([MCPServerConfig.from_dict(server) for server in servers])

    @classmethod
    def from_config(cls, path: Path | None = None) -> "MCPHub":
        config_path = path or Path(__file__).resolve().parents[2] / "config" / "default.yaml"
        with config_path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
        servers = config.get("mcp", {}).get("servers", [])
        return cls([MCPServerConfig.from_dict(server) for server in servers])

    def connect(self, name: str) -> ToolRegistry:
        try:
            config = self._servers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown MCP server: {name}") from exc
        client = self._clients.setdefault(name, StreamableHTTPMCPClient(config))
        registry = ToolRegistry()
        registry.register_mcp_tools(name, client.list_tools(), client.call_tool)
        return registry

    def connect_enabled(self, names: tuple[str, ...]) -> ToolRegistry:
        registry = ToolRegistry()
        for name in names:
            registry.extend(self.connect(name))
        return registry


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        result = os.getenv(name)
        if result is None:
            raise ValueError(f"Missing environment variable for MCP config: {name}")
        return result

    return _ENV_PATTERN.sub(replace, value)


def _decode_response(body: str, content_type: str) -> dict[str, Any]:
    if "text/event-stream" not in content_type.lower():
        value = json.loads(body)
        if not isinstance(value, dict):
            raise RuntimeError("MCP server returned a non-object JSON-RPC response")
        return value

    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line and data_lines:
            value = json.loads("\n".join(data_lines))
            if isinstance(value, dict):
                events.append(value)
            data_lines = []
    if not events:
        raise RuntimeError("MCP server returned no JSON-RPC event")
    return events[-1]


def _render_tool_result(result: dict[str, Any]) -> str:
    content = result.get("content", [])
    text_parts = [
        item["text"]
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and "text" in item
    ]
    if text_parts:
        rendered = "\n".join(text_parts)
    elif "structuredContent" in result:
        rendered = json.dumps(result["structuredContent"], ensure_ascii=False)
    else:
        rendered = json.dumps(result, ensure_ascii=False)
    if result.get("isError"):
        raise RuntimeError(f"MCP tool returned an error: {rendered}")
    return rendered
