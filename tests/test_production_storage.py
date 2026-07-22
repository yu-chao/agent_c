from pathlib import Path

import pytest

from agent_runtime.settings import StorageSettings, load_settings


def test_postgres_storage_configuration_is_parsed(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "storage:\n"
        "  backend: postgres\n"
        "  postgres_dsn: postgresql://agent@localhost/runtime\n"
        "  migrate_on_start: false\n"
        "  queue_enabled: true\n",
        encoding="utf-8",
    )
    for name in (
        "AGENT_STORAGE_BACKEND", "AGENT_POSTGRES_DSN",
        "AGENT_STORAGE_MIGRATE_ON_START", "AGENT_RUN_QUEUE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(config)

    assert settings.storage == StorageSettings(
        backend="postgres",
        postgres_dsn="postgresql://agent@localhost/runtime",
        migrate_on_start=False,
        queue_enabled=True,
    )


def test_postgres_backend_requires_dsn():
    with pytest.raises(ValueError, match="postgres_dsn"):
        StorageSettings(backend="postgres")


def test_queue_cannot_be_enabled_for_sqlite():
    with pytest.raises(ValueError, match="queue_enabled"):
        StorageSettings(queue_enabled=True)


def test_postgres_advisory_lock_key_has_no_nul_bytes():
    from agent_runtime.approval.postgres_store import (
        _advisory_lock_key as approval_lock_key,
    )
    from agent_runtime.sessions.postgres_store import _advisory_lock_key

    for key in (
        _advisory_lock_key("wecom", "msg\x00id"),
        approval_lock_key("run_abc", "call\x001"),
    ):
        assert "\x00" not in key
