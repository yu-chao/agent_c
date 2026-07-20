from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


_PROVIDERS = frozenset({'openai', 'anthropic'})
_TOP_LEVEL_FIELDS = frozenset(
    {'model', 'reliability', 'approval', 'mcp', 'session', 'context', 'skills',
     'storage', 'system_prompt'}
)
_SECTION_FIELDS = {
    'model': frozenset({'provider', 'name'}),
    'reliability': frozenset(
        {'request_timeout_seconds', 'max_attempts', 'fallback_provider',
         'fallback_model'}
    ),
    'approval': frozenset({'enabled', 'timeout_seconds', 'store_path', 'tools'}),
    'mcp': frozenset({'enabled_servers', 'servers'}),
    'session': frozenset({'enabled', 'store_path', 'lease_seconds'}),
    'context': frozenset(
        {'recent_message_limit', 'max_input_tokens',
         'summary_trigger_tokens', 'tool_result_max_tokens'}
    ),
    'skills': frozenset(
        {'enabled', 'paths', 'max_active', 'allowed_filesystem'}
    ),
    'storage': frozenset(
        {'backend', 'postgres_dsn', 'migrate_on_start', 'queue_enabled'}
    ),
}
_MCP_SERVER_FIELDS = frozenset(
    {'name', 'type', 'url', 'headers', 'timeout', 'sse_read_timeout'}
)


@dataclass(frozen=True)
class ModelSettings:
    provider: str = 'openai'
    name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str):
            raise ValueError('model.provider must be a string')
        _validate_provider(self.provider, 'model.provider')
        if self.name is not None and (
            not isinstance(self.name, str) or not self.name.strip()
        ):
            raise ValueError('model.name must be a non-empty string or null')


@dataclass(frozen=True)
class ReliabilitySettings:
    request_timeout_seconds: float = 60.0
    max_attempts: int = 3
    fallback_provider: str | None = None
    fallback_model: str | None = None

    def __post_init__(self) -> None:
        if self.request_timeout_seconds <= 0:
            raise ValueError(
                'reliability.request_timeout_seconds must be greater than zero'
            )
        if self.max_attempts < 1:
            raise ValueError(
                'reliability.max_attempts must be greater than zero'
            )
        if self.fallback_provider is not None:
            if not isinstance(self.fallback_provider, str):
                raise ValueError(
                    'reliability.fallback_provider must be a string or null'
                )
            _validate_provider(
                self.fallback_provider, 'reliability.fallback_provider'
            )
        if self.fallback_model is not None and self.fallback_provider is None:
            raise ValueError(
                'reliability.fallback_provider is required when fallback_model '
                'is configured'
            )
        if self.fallback_model is not None and (
            not isinstance(self.fallback_model, str)
            or not self.fallback_model.strip()
        ):
            raise ValueError(
                'reliability.fallback_model must be a non-empty string or null'
            )


@dataclass(frozen=True)
class ApprovalSettings:
    enabled: bool = True
    timeout_seconds: int = 600
    store_path: Path = Path('.runtime/approvals.db')
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError('approval.timeout_seconds must be greater than zero')


@dataclass(frozen=True)
class MCPSettings:
    enabled_servers: tuple[str, ...] = ()
    servers: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SessionSettings:
    enabled: bool = True
    store_path: Path = Path('.runtime/sessions.db')
    lease_seconds: int = 30

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError('session.lease_seconds must be greater than zero')


@dataclass(frozen=True)
class StorageSettings:
    backend: str = 'sqlite'
    postgres_dsn: str | None = None
    migrate_on_start: bool = True
    queue_enabled: bool = False

    def __post_init__(self) -> None:
        if self.backend not in {'sqlite', 'postgres'}:
            raise ValueError('storage.backend must be one of: sqlite, postgres')
        if self.backend == 'postgres' and not self.postgres_dsn:
            raise ValueError(
                'storage.postgres_dsn is required for the postgres backend'
            )
        if self.queue_enabled and self.backend != 'postgres':
            raise ValueError(
                'storage.queue_enabled requires the postgres backend'
            )


@dataclass(frozen=True)
class ContextSettings:
    recent_message_limit: int = 20
    max_input_tokens: int = 32000
    summary_trigger_tokens: int = 24000
    tool_result_max_tokens: int = 4000

    def __post_init__(self) -> None:
        if self.recent_message_limit < 0:
            raise ValueError(
                'context.recent_message_limit must not be negative'
            )
        if self.max_input_tokens <= 0:
            raise ValueError('context.max_input_tokens must be greater than zero')
        if not 0 < self.summary_trigger_tokens <= self.max_input_tokens:
            raise ValueError(
                'context.summary_trigger_tokens must be between 1 and '
                'max_input_tokens'
            )
        if not 0 < self.tool_result_max_tokens <= self.max_input_tokens:
            raise ValueError(
                'context.tool_result_max_tokens must be between 1 and '
                'max_input_tokens'
            )


@dataclass(frozen=True)
class SkillsSettings:
    enabled: bool = False
    paths: tuple[Path, ...] = (Path('skills'),)
    max_active: int = 3
    allowed_filesystem: str = 'read'

    def __post_init__(self) -> None:
        if self.max_active < 0:
            raise ValueError('skills.max_active must not be negative')
        if self.allowed_filesystem not in {'none', 'read', 'write'}:
            raise ValueError(
                'skills.allowed_filesystem must be one of: none, read, write'
            )


@dataclass(frozen=True)
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    reliability: ReliabilitySettings = field(
        default_factory=ReliabilitySettings
    )
    approval: ApprovalSettings = field(default_factory=ApprovalSettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    system_prompt: str = 'You are a coding agent. Use tools when useful.'

    def with_model(
        self, provider: str | None = None, name: str | None = None
    ) -> 'Settings':
        return replace(
            self,
            model=ModelSettings(
                provider=provider or self.model.provider,
                name=name or self.model.name,
            ),
        )


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv()
    config_path = path or _default_config_path()
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding='utf-8') as config_file:
            loaded = yaml.safe_load(config_file) or {}
        if not isinstance(loaded, dict):
            raise ValueError('configuration root must be a mapping')
        raw = loaded

    _validate_config_shape(raw)
    model_raw = _section(raw, 'model')
    reliability_raw = _section(raw, 'reliability')
    approval_raw = _section(raw, 'approval')
    mcp_raw = _section(raw, 'mcp')
    session_raw = _section(raw, 'session')
    storage_raw = _section(raw, 'storage')
    context_raw = _section(raw, 'context')
    skills_raw = _section(raw, 'skills')

    provider = _environment_text(
        'AGENT_MODEL_PROVIDER', model_raw.get('provider', 'openai')
    )
    model_name = _environment_optional_text(
        'AGENT_MODEL_ID', model_raw.get('name')
    )
    approval_enabled = _environment_bool(
        'AGENT_APPROVAL_ENABLED',
        _config_bool(approval_raw.get('enabled', True), 'approval.enabled'),
    )
    session_enabled = _environment_bool(
        'AGENT_SESSION_ENABLED',
        _config_bool(session_raw.get('enabled', True), 'session.enabled'),
    )
    fallback_provider = _environment_optional_text(
        'AGENT_MODEL_FALLBACK_PROVIDER',
        reliability_raw.get('fallback_provider'),
    )
    fallback_model = _environment_optional_text(
        'AGENT_MODEL_FALLBACK_ID', reliability_raw.get('fallback_model')
    )

    approval_tools = _string_list(
        approval_raw.get('tools', ()), 'approval.tools'
    )
    system_prompt = raw.get(
        'system_prompt', 'You are a coding agent. Use tools when useful.'
    )
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError('system_prompt must be a non-empty string')

    return Settings(
        model=ModelSettings(provider, model_name),
        reliability=ReliabilitySettings(
            request_timeout_seconds=float(
                os.getenv(
                    'AGENT_MODEL_REQUEST_TIMEOUT_SECONDS',
                    reliability_raw.get('request_timeout_seconds', 60),
                )
            ),
            max_attempts=int(
                os.getenv(
                    'AGENT_MODEL_MAX_ATTEMPTS',
                    reliability_raw.get('max_attempts', 3),
                )
            ),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        ),
        approval=ApprovalSettings(
            enabled=approval_enabled,
            timeout_seconds=int(
                os.getenv(
                    'AGENT_APPROVAL_TIMEOUT',
                    approval_raw.get('timeout_seconds', 600),
                )
            ),
            store_path=Path(
                os.getenv(
                    'AGENT_APPROVAL_STORE',
                    approval_raw.get('store_path', '.runtime/approvals.db'),
                )
            ),
            tools=approval_tools if approval_enabled else (),
        ),
        mcp=MCPSettings(
            enabled_servers=tuple(
                _string_list(mcp_raw.get('enabled_servers', ()),
                             'mcp.enabled_servers')
            ),
            servers=tuple(mcp_raw.get('servers', ())),
        ),
        session=SessionSettings(
            enabled=session_enabled,
            store_path=Path(
                os.getenv(
                    'AGENT_SESSION_STORE',
                    session_raw.get('store_path', '.runtime/sessions.db'),
                )
            ),
            lease_seconds=int(
                os.getenv(
                    'AGENT_SESSION_LEASE_SECONDS',
                    session_raw.get('lease_seconds', 30),
                )
            ),
        ),
        storage=StorageSettings(
            backend=str(
                os.getenv(
                    'AGENT_STORAGE_BACKEND',
                    storage_raw.get('backend', 'sqlite'),
                )
            ),
            postgres_dsn=_environment_optional_text(
                'AGENT_POSTGRES_DSN', storage_raw.get('postgres_dsn')
            ),
            migrate_on_start=_environment_bool(
                'AGENT_STORAGE_MIGRATE_ON_START',
                _config_bool(
                    storage_raw.get('migrate_on_start', True),
                    'storage.migrate_on_start',
                ),
            ),
            queue_enabled=_environment_bool(
                'AGENT_RUN_QUEUE_ENABLED',
                _config_bool(
                    storage_raw.get('queue_enabled', False),
                    'storage.queue_enabled',
                ),
            ),
        ),
        context=ContextSettings(
            recent_message_limit=int(
                os.getenv(
                    'AGENT_CONTEXT_RECENT_MESSAGE_LIMIT',
                    context_raw.get('recent_message_limit', 20),
                )
            ),
            max_input_tokens=int(
                os.getenv(
                    'AGENT_CONTEXT_MAX_INPUT_TOKENS',
                    context_raw.get('max_input_tokens', 32000),
                )
            ),
            summary_trigger_tokens=int(
                os.getenv(
                    'AGENT_CONTEXT_SUMMARY_TRIGGER_TOKENS',
                    context_raw.get('summary_trigger_tokens', 24000),
                )
            ),
            tool_result_max_tokens=int(
                os.getenv(
                    'AGENT_CONTEXT_TOOL_RESULT_MAX_TOKENS',
                    context_raw.get('tool_result_max_tokens', 4000),
                )
            ),
        ),
        skills=SkillsSettings(
            enabled=_environment_bool(
                'AGENT_SKILLS_ENABLED',
                _config_bool(skills_raw.get('enabled', False), 'skills.enabled'),
            ),
            paths=tuple(
                Path(value) for value in _string_list(
                    skills_raw.get('paths', ('skills',)), 'skills.paths'
                )
            ),
            max_active=int(
                os.getenv(
                    'AGENT_SKILLS_MAX_ACTIVE', skills_raw.get('max_active', 3)
                )
            ),
            allowed_filesystem=str(
                os.getenv(
                    'AGENT_SKILLS_ALLOWED_FILESYSTEM',
                    skills_raw.get('allowed_filesystem', 'read'),
                )
            ),
        ),
        system_prompt=system_prompt,
    )


def _validate_config_shape(raw: dict[str, Any]) -> None:
    _reject_unknown(raw, _TOP_LEVEL_FIELDS, '')
    for name, allowed in _SECTION_FIELDS.items():
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f'{name} must be a mapping')
        _reject_unknown(value, allowed, f'{name}.')

    servers = _section(raw, 'mcp').get('servers', ())
    if not isinstance(servers, (list, tuple)):
        raise ValueError('mcp.servers must be a list')
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            raise ValueError(f'mcp.servers.{index} must be a mapping')
        _reject_unknown(
            server, _MCP_SERVER_FIELDS, f'mcp.servers.{index}.'
        )
        _validate_mcp_server(server, index)


def _validate_mcp_server(server: dict[str, Any], index: int) -> None:
    prefix = f'mcp.servers.{index}'
    for field_name in ('name', 'url'):
        value = server.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{prefix}.{field_name} is required')
    transport = server.get('type', 'streamable-http')
    if transport != 'streamable-http':
        raise ValueError(f'{prefix}.type must be streamable-http')
    headers = server.get('headers', {})
    if not isinstance(headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError(f'{prefix}.headers must be a string mapping')
    for field_name in ('timeout', 'sse_read_timeout'):
        value = server.get(field_name, 50)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{prefix}.{field_name} must be a number')
        if value <= 0:
            raise ValueError(f'{prefix}.{field_name} must be greater than zero')


def _reject_unknown(
    value: dict[str, Any], allowed: frozenset[str], prefix: str
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f'Unknown configuration field: {prefix}{unknown[0]}')


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    return raw.get(name, {})


def _validate_provider(value: str, field_name: str) -> None:
    if value not in _PROVIDERS:
        raise ValueError(
            f'{field_name} must be one of: {", ".join(sorted(_PROVIDERS))}'
        )


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f'{field_name} must be a list of non-empty strings')
    return tuple(value)


def _config_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f'{field_name} must be a boolean value')
    return value


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise ValueError(f'{name} must be a boolean value')


def _environment_text(name: str, default: Any) -> Any:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value


def _environment_optional_text(name: str, default: Any) -> Any:
    value = os.getenv(name)
    if value is None:
        return default
    if not value.strip():
        return None
    return value


def _default_config_path() -> Path:
    source_path = Path(__file__).resolve().parents[1] / 'config' / 'default.yaml'
    if source_path.exists():
        return source_path
    return Path(__file__).with_name('default.yaml')
