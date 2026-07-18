from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class ModelSettings:
    provider: str = 'openai'
    name: str | None = None


@dataclass(frozen=True)
class ApprovalSettings:
    enabled: bool = True
    timeout_seconds: int = 600
    store_path: Path = Path('.runtime/approvals.db')
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class MCPSettings:
    enabled_servers: tuple[str, ...] = ()
    servers: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SessionSettings:
    enabled: bool = True
    store_path: Path = Path('.runtime/sessions.db')
    lease_seconds: int = 30

    def __post_init__(self):
        if self.lease_seconds <= 0:
            raise ValueError('session.lease_seconds must be greater than zero')


@dataclass(frozen=True)
class ContextSettings:
    recent_message_limit: int = 20

    def __post_init__(self):
        if self.recent_message_limit < 0:
            raise ValueError(
                'context.recent_message_limit must not be negative'
            )


@dataclass(frozen=True)
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    approval: ApprovalSettings = field(default_factory=ApprovalSettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
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
            raw = yaml.safe_load(config_file) or {}

    model_raw = raw.get('model', {})
    approval_raw = raw.get('approval', {})
    mcp_raw = raw.get('mcp', {})
    session_raw = raw.get('session', {})
    context_raw = raw.get('context', {})
    provider = (
        os.getenv('AGENT_MODEL_PROVIDER')
        or os.getenv('MODEL_PROVIDER')
        or os.getenv('provider')
        or model_raw.get('provider')
        or 'openai'
    )
    model_name = (
        os.getenv('AGENT_MODEL_ID')
        or os.getenv('MODEL_ID')
        or model_raw.get('name')
    )
    enabled = _environment_bool(
        'AGENT_APPROVAL_ENABLED', bool(approval_raw.get('enabled', True))
    )
    return Settings(
        model=ModelSettings(str(provider), model_name),
        approval=ApprovalSettings(
            enabled=enabled,
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
            tools=tuple(approval_raw.get('tools', ())) if enabled else (),
        ),
        mcp=MCPSettings(
            enabled_servers=tuple(mcp_raw.get('enabled_servers', ())),
            servers=tuple(mcp_raw.get('servers', ())),
        ),
        session=SessionSettings(
            enabled=_environment_bool(
                'AGENT_SESSION_ENABLED',
                bool(session_raw.get('enabled', True)),
            ),
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
        context=ContextSettings(
            recent_message_limit=int(
                os.getenv(
                    'AGENT_CONTEXT_RECENT_MESSAGE_LIMIT',
                    context_raw.get('recent_message_limit', 20),
                )
            ),
        ),
        system_prompt=str(
            raw.get(
                'system_prompt',
                'You are a coding agent. Use tools when useful.',
            )
        ),
    )


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


def _default_config_path() -> Path:
    source_path = (
        Path(__file__).resolve().parents[1]
        / 'config'
        / 'default.yaml'
    )
    if source_path.exists():
        return source_path
    return Path(__file__).with_name('default.yaml')
