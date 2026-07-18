import ast
import importlib
import sys
from pathlib import Path

import pytest

from agent_runtime.bootstrap import build_runtime, build_tool_registry
from agent_runtime.gateway import WeComGateway
from agent_runtime.gateway.wecom_gateway import WeComGateway as ConcreteWeComGateway
from agent_runtime.settings import (
    MCPSettings,
    ApprovalSettings,
    ContextSettings,
    ModelSettings,
    SessionSettings,
    Settings,
    load_settings,
)
from agent_runtime.tools import ToolRegistry, ToolSpec


FORBIDDEN_CORE_IMPORTS = (
    'agent_runtime.gateway',
    'agent_runtime.mcp',
    'agent_runtime.models.openai',
    'agent_runtime.models.anthropic',
    'agent_runtime.approval.store',
    'agent_runtime.sessions',
)


def test_core_does_not_import_infrastructure_adapters():
    core = Path(__file__).parents[1] / 'agent_runtime' / 'core'
    violations = []
    for path in core.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(FORBIDDEN_CORE_IMPORTS):
                    violations.append(f'{path.name}: {node.module}')
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN_CORE_IMPORTS):
                        violations.append(f'{path.name}: {alias.name}')
    assert violations == []


def test_package_import_does_not_load_provider_adapters():
    for name in list(sys.modules):
        if name.startswith('agent_runtime.models'):
            sys.modules.pop(name)
    importlib.reload(importlib.import_module('agent_runtime'))
    assert 'agent_runtime.models.openai' not in sys.modules
    assert 'agent_runtime.models.anthropic' not in sys.modules


def test_wecom_has_one_public_gateway_class():
    assert WeComGateway is ConcreteWeComGateway


def test_settings_use_yaml_then_environment_override(tmp_path, monkeypatch):
    config = tmp_path / 'settings.yaml'
    config.write_text(
        'model:\n  provider: anthropic\n  name: configured\n'
        'approval:\n  enabled: false\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('AGENT_MODEL_PROVIDER', 'openai')
    monkeypatch.setenv('AGENT_MODEL_ID', 'environment-model')

    settings = load_settings(config)

    assert settings.model.provider == 'openai'
    assert settings.model.name == 'environment-model'
    assert not settings.approval.enabled


def test_tool_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    spec = ToolSpec('echo', 'Echo', {'type': 'object'})
    registry.register(spec, lambda: 'first')
    with pytest.raises(ValueError, match='Duplicate tool name'):
        registry.register(spec, lambda: 'second')


def test_bootstrap_rejects_unknown_enabled_mcp_server():
    settings = Settings(
        mcp=MCPSettings(
            enabled_servers=('missing',),
            servers=(),
        )
    )
    with pytest.raises(ValueError, match='Unknown enabled MCP servers'):
        build_tool_registry(settings)


def test_settings_parse_session_and_short_term_context(tmp_path):
    config = tmp_path / 'settings.yaml'
    config.write_text(
        'session:\n  enabled: true\n  store_path: data/sessions.db\n'
        'context:\n  recent_message_limit: 12\n',
        encoding='utf-8',
    )

    settings = load_settings(config)

    assert settings.session == SessionSettings(
        enabled=True, store_path=Path('data/sessions.db'), lease_seconds=30
    )
    assert settings.context == ContextSettings(recent_message_limit=12)


@pytest.mark.parametrize(
    ('content', 'message'),
    (
        ('session:\n  lease_seconds: 0\n', 'lease_seconds'),
        ('context:\n  recent_message_limit: -1\n', 'recent_message_limit'),
    ),
)
def test_settings_reject_invalid_session_runtime_values(
    tmp_path, content, message
):
    config = tmp_path / 'settings.yaml'
    config.write_text(content, encoding='utf-8')

    with pytest.raises(ValueError, match=message):
        load_settings(config)


def test_bootstrap_enables_persistent_sessions(tmp_path):
    settings = Settings(
        model=ModelSettings('openai', 'test-model'),
        approval=ApprovalSettings(enabled=False),
        session=SessionSettings(
            enabled=True, store_path=Path('state/sessions.db')
        ),
        context=ContextSettings(recent_message_limit=7),
    )

    runtime = build_runtime(
        settings=settings,
        workdir=tmp_path,
        clients={'openai': object()},
    )

    assert runtime.session_store.path == tmp_path / 'state/sessions.db'
    assert runtime.recent_message_limit == 7
