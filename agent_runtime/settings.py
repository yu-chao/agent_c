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
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    approval: ApprovalSettings = field(default_factory=ApprovalSettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)
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
