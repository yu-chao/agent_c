from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_runtime.approval import PostgresApprovalStore, SQLiteApprovalStore
from agent_runtime.admin import (
    AdminService,
    PostgresAdminRepository,
    SQLiteAdminRepository,
)
from agent_runtime.core import AgentRuntime
from agent_runtime.context import ContextManager, ContextWindow
from agent_runtime.hooks import HookManager
from agent_runtime.mcp import MCPHub
from agent_runtime.memory import (
    MemoryService,
    PostgresMemoryStore,
    SQLiteMemoryStore,
)
from agent_runtime.models import create_model_provider
from agent_runtime.models.resilient import RetryPolicy
from agent_runtime.retention import (
    PostgresRetentionRepository,
    RetentionService,
    SQLiteRetentionRepository,
)
from agent_runtime.security import PermissionPolicy
from agent_runtime.sessions import PostgresSessionStore, SQLiteSessionStore
from agent_runtime.skills import SkillLoader, SkillSelector
from agent_runtime.settings import Settings, load_settings
from agent_runtime.tools import ToolRegistry
from agent_runtime.tasks import PostgresRunQueue


def build_runtime(
    *,
    settings: Settings | None = None,
    workdir: Path | None = None,
    clients: dict[str, Any] | None = None,
) -> AgentRuntime:
    settings = settings or load_settings()
    workdir = (workdir or Path.cwd()).resolve()
    registry = build_tool_registry(settings)
    skill_loader = None
    skill_selector = None
    if settings.skills.enabled:
        skill_paths = tuple(
            path if path.is_absolute() else workdir / path
            for path in settings.skills.paths
        )
        tool_specs, _ = registry.assemble()
        skill_loader = SkillLoader(
            skill_paths,
            available_tools={spec.name for spec in tool_specs},
            allowed_permissions={
                'filesystem': settings.skills.allowed_filesystem
            },
        )
        # Fail at startup when an installed manifest is invalid.
        skill_loader.load()
        skill_selector = SkillSelector(settings.skills.max_active)
    approval_store = None
    if settings.approval.enabled:
        if settings.storage.backend == 'postgres':
            approval_store = PostgresApprovalStore(
                settings.storage.postgres_dsn or '',
                migrate=settings.storage.migrate_on_start,
            )
        else:
            store_path = settings.approval.store_path
            if not store_path.is_absolute():
                store_path = workdir / store_path
            approval_store = SQLiteApprovalStore(store_path)
    session_store = None
    if settings.session.enabled:
        if settings.storage.backend == 'postgres':
            session_store = PostgresSessionStore(
                settings.storage.postgres_dsn or '',
                lease_seconds=settings.session.lease_seconds,
                migrate=settings.storage.migrate_on_start,
            )
        else:
            session_path = settings.session.store_path
            if not session_path.is_absolute():
                session_path = workdir / session_path
            session_store = SQLiteSessionStore(
                session_path, lease_seconds=settings.session.lease_seconds
            )
    memory_service = None
    if settings.memory.enabled:
        if settings.storage.backend == 'postgres':
            memory_store = PostgresMemoryStore(
                settings.storage.postgres_dsn or '',
                migrate=settings.storage.migrate_on_start,
            )
        else:
            memory_path = settings.memory.store_path
            if not memory_path.is_absolute():
                memory_path = workdir / memory_path
            memory_store = SQLiteMemoryStore(memory_path)
        memory_service = MemoryService(
            memory_store,
            default_ttl_days=settings.memory.default_ttl_days,
            max_results=settings.memory.max_results,
        )
    context_options = {
        'max_input_tokens': settings.context.max_input_tokens,
        'tool_result_max_tokens': settings.context.tool_result_max_tokens,
    }
    if session_store is not None:
        context_manager = ContextManager(
            session_store,
            summary_trigger_tokens=(
                settings.context.summary_trigger_tokens
            ),
            recent_message_limit=settings.context.recent_message_limit,
            memory_service=memory_service,
            **context_options,
        )
    else:
        context_manager = ContextWindow(**context_options)

    return AgentRuntime(
        model=build_model_provider(settings, clients),
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
        context_manager=context_manager,
        skill_loader=skill_loader,
        skill_selector=skill_selector,
        memory_service=memory_service,
    )


def build_model_provider(
    settings: Settings,
    clients: dict[str, Any] | None = None,
):
    """仅根据已解析的强类型配置装配可靠模型。"""
    return create_model_provider(
        clients,
        provider=settings.model.provider,
        model=settings.model.name,
        fallback_provider=settings.reliability.fallback_provider,
        fallback_model=settings.reliability.fallback_model,
        retry_policy=RetryPolicy(
            request_timeout_seconds=(
                settings.reliability.request_timeout_seconds
            ),
            max_attempts=settings.reliability.max_attempts,
        ),
    )


def build_run_queue(settings: Settings):
    """按配置装配可选队列；队列不承载 Run 的真实状态。"""
    if not settings.storage.queue_enabled:
        return None
    return PostgresRunQueue(
        settings.storage.postgres_dsn or '',
        migrate=settings.storage.migrate_on_start,
    )


def build_admin_service(
    settings: Settings,
    *,
    runtime: AgentRuntime | None = None,
    workdir: Path | None = None,
) -> AdminService:
    """装配显式调用的管理服务；认证主体仍由可信入口注入。"""
    repository = _build_governance_repository(
        settings, workdir=workdir, retention=False
    )
    approval_repository = runtime.approval_store if runtime else None
    resume_callback = runtime.resume_run if runtime else None
    return AdminService(
        repository,
        approval_repository=approval_repository,
        resume_callback=resume_callback,
    )


def build_retention_service(
    settings: Settings, *, workdir: Path | None = None
) -> RetentionService:
    """装配手动触发的数据保留服务；不会自动执行清理。"""
    repository = _build_governance_repository(
        settings, workdir=workdir, retention=True
    )
    return RetentionService(repository)


def _build_governance_repository(settings, *, workdir, retention):
    if not settings.session.enabled:
        raise ValueError("session storage is required for governance services")
    if settings.storage.backend == "postgres":
        cls = (
            PostgresRetentionRepository if retention
            else PostgresAdminRepository
        )
        return cls(
            settings.storage.postgres_dsn or "",
            migrate=settings.storage.migrate_on_start,
        )
    path = settings.session.store_path
    if not path.is_absolute():
        path = (workdir or Path.cwd()).resolve() / path
    cls = SQLiteRetentionRepository if retention else SQLiteAdminRepository
    return cls(path)


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
