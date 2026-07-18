from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_runtime.approval import SQLiteApprovalStore
from agent_runtime.core import AgentRuntime
from agent_runtime.hooks import HookManager
from agent_runtime.mcp import MCPHub
from agent_runtime.models import create_model_provider
from agent_runtime.security import PermissionPolicy
from agent_runtime.sessions import SQLiteSessionStore
from agent_runtime.settings import Settings, load_settings
from agent_runtime.tools import ToolRegistry


def build_runtime(
    *,
    settings: Settings | None = None,
    workdir: Path | None = None,
    clients: dict[str, Any] | None = None,
) -> AgentRuntime:
    settings = settings or load_settings()
    workdir = (workdir or Path.cwd()).resolve()
    registry = build_tool_registry(settings)
    approval_store = None
    if settings.approval.enabled:
        store_path = settings.approval.store_path
        if not store_path.is_absolute():
            store_path = workdir / store_path
        approval_store = SQLiteApprovalStore(store_path)
    session_store = None
    if settings.session.enabled:
        session_path = settings.session.store_path
        if not session_path.is_absolute():
            session_path = workdir / session_path
        session_store = SQLiteSessionStore(
            session_path, lease_seconds=settings.session.lease_seconds
        )

    return AgentRuntime(
        model=create_model_provider(
            clients,
            provider=settings.model.provider,
            model=settings.model.name,
        ),
        tools=registry,
        hooks=HookManager(),
        permission_policy=PermissionPolicy(
            workdir,
            settings.approval.tools,
        ),
        system_prompt=settings.system_prompt,
        approval_store=approval_store,
        approval_timeout_seconds=settings.approval.timeout_seconds,
        session_store=session_store,
        recent_message_limit=settings.context.recent_message_limit,
    )


def build_tool_registry(settings: Settings) -> ToolRegistry:
    if not settings.mcp.enabled_servers:
        return ToolRegistry()
    configured = {
        str(server.get('name')): server
        for server in settings.mcp.servers
    }
    missing = [
        name
        for name in settings.mcp.enabled_servers
        if name not in configured
    ]
    if missing:
        raise ValueError(
            f'Unknown enabled MCP servers: {missing}'
        )
    selected = tuple(
        configured[name]
        for name in settings.mcp.enabled_servers
    )
    hub = MCPHub.from_servers(selected)
    return hub.connect_enabled(settings.mcp.enabled_servers)
