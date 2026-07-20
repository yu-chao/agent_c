from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_runtime.approval import RuntimeIdentity
from agent_runtime.context import ContextManager
from agent_runtime.memory import (
    MemoryRecord,
    MemoryService,
    MemorySource,
    MemoryVisibility,
    SQLiteMemoryStore,
)
from agent_runtime.sessions import SQLiteSessionStore
from agent_runtime.settings import MemorySettings, load_settings


def identity(
    sender: str = "user-1",
    conversation: str = "chat-1",
    message: str = "message-1",
    tenant: str | None = "tenant-1",
) -> RuntimeIdentity:
    metadata = {"tenant_id": tenant} if tenant else {}
    return RuntimeIdentity("wecom", conversation, sender, message, metadata)


def service(tmp_path, *, ttl=365):
    return MemoryService(
        SQLiteMemoryStore(tmp_path / "memory.db"),
        default_ttl_days=ttl,
        max_results=3,
    )


def test_explicit_user_memory_records_source_scope_and_expiry(tmp_path):
    memories = service(tmp_path)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    memory = memories.remember_user_statement(
        identity(), "用户偏好离心泵", session_id="session-1", now=now
    )

    assert memory.subject.startswith("subject_")
    assert memory.source.message_id == "message-1"
    assert memory.source.session_id == "session-1"
    assert memory.visibility is MemoryVisibility.PRIVATE
    assert memory.confidence == 1
    assert memory.expires_at == now + timedelta(days=365)


def test_model_inference_cannot_be_saved_as_user_fact():
    with pytest.raises(ValueError, match="explicit user"):
        MemoryRecord.create(
            subject="wecom:user-1",
            content="模型猜测用户喜欢蓝色",
            source=MemorySource(
                kind="model_inference",  # type: ignore[arg-type]
                platform="wecom", message_id="model-output",
                subject="wecom:user-1",
            ),
        )


def test_visibility_is_filtered_before_retrieval(tmp_path):
    memories = service(tmp_path)
    owner = identity()
    other = identity(sender="user-2", message="message-2")
    same_conversation = identity(sender="user-3", message="message-3")
    same_tenant = identity(
        sender="user-4", conversation="chat-2", message="message-4"
    )
    outsider = identity(
        sender="user-5", conversation="chat-3", message="message-5",
        tenant="tenant-2",
    )
    memories.remember_user_statement(owner, "private-pump")
    memories.remember_user_statement(
        other, "conversation-pump", visibility="conversation"
    )
    memories.remember_user_statement(
        same_tenant, "tenant-pump", visibility="tenant"
    )

    assert {
        item.memory.content for item in memories.retrieve(owner, "pump")
    } == {"private-pump", "conversation-pump", "tenant-pump"}
    assert {
        item.memory.content
        for item in memories.retrieve(same_conversation, "pump")
    } == {"conversation-pump", "tenant-pump"}
    assert memories.retrieve(outsider, "pump") == []


def test_subject_identity_cannot_collide_on_separator_values(tmp_path):
    memories = service(tmp_path)
    first = RuntimeIdentity("a:b", "chat", "c", "m1")
    second = RuntimeIdentity("a", "chat", "b:c", "m2")

    one = memories.remember_user_statement(first, "first")
    two = memories.remember_user_statement(second, "second")

    assert one.subject != two.subject
    assert [item.content for item in memories.what_is_remembered(first)] == [
        "first"
    ]


def test_expired_memory_is_hidden_and_can_be_purged(tmp_path):
    memories = service(tmp_path, ttl=None)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    memory = memories.remember_user_statement(
        identity(), "temporary fact",
        expires_at=now + timedelta(seconds=1), now=now,
    )

    assert memories.what_is_remembered(
        identity(), now=now + timedelta(seconds=2)
    ) == []
    assert memories.purge_expired(now=now + timedelta(seconds=2)) == 1
    assert memories.store.get(memory.id) is None


def test_correction_preserves_provenance_and_forget_removes_entire_chain(tmp_path):
    memories = service(tmp_path)
    original = memories.remember_user_statement(identity(), "型号是 A")
    correction_identity = identity(message="message-correction")

    corrected = memories.correct(
        correction_identity, original.id, "型号是 B"
    )

    stored_original = memories.store.get(original.id)
    assert stored_original.deleted_at is not None
    assert stored_original.superseded_by_id == corrected.id
    assert corrected.root_id == original.root_id
    assert corrected.source.kind.value == "user_correction"
    assert [item.content for item in memories.what_is_remembered(identity())] == [
        "型号是 B"
    ]

    assert memories.forget(identity(), corrected.id)
    assert memories.store.get(original.id) is None
    assert memories.store.get(corrected.id) is None


def test_expired_correction_can_be_physically_purged(tmp_path):
    memories = service(tmp_path, ttl=None)
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    original = memories.remember_user_statement(
        identity(), "old", expires_at=now + timedelta(days=2), now=now
    )
    corrected = memories.correct(
        identity(message="correction"), original.id, "new",
        expires_at=now + timedelta(seconds=1), now=now,
    )

    assert memories.purge_expired(now=now + timedelta(seconds=2)) == 1
    assert memories.store.get(corrected.id) is None
    assert memories.store.get(original.id).superseded_by_id is None


def test_other_subject_cannot_correct_or_delete_memory(tmp_path):
    memories = service(tmp_path)
    memory = memories.remember_user_statement(identity(), "secret")
    attacker = identity(sender="other", message="attack")

    with pytest.raises(KeyError, match="not found"):
        memories.correct(attacker, memory.id, "tampered")
    assert not memories.forget(attacker, memory.id)
    assert memories.store.get(memory.id) is not None


def test_retrieval_is_relevant_and_always_carries_source(tmp_path):
    memories = service(tmp_path)
    memories.remember_user_statement(identity(message="pump-source"), "偏好离心泵")
    memories.remember_user_statement(identity(message="meter-source"), "偏好电磁流量计")

    result = memories.retrieve(identity(), "泵选型")

    assert [item.memory.content for item in result] == ["偏好离心泵"]
    assert result[0].citation == "wecom/pump-source"


def test_context_injects_memory_after_summary_with_source(tmp_path):
    session_store = SQLiteSessionStore(tmp_path / "sessions.db")
    memories = service(tmp_path)
    user = identity(message="memory-source")
    memories.remember_user_statement(user, "用户偏好离心泵")
    old = session_store.start_inbound(
        platform="wecom", conversation_id="chat-1", sender_id="user-1",
        message_id="old", user_content="old question",
        initial_checkpoint={"action": "model", "messages": []},
    )
    session_store.complete_run(old.run.id, "old answer")
    through = session_store.recent_messages(old.run.session_id, 10)[-1].id
    session_store.save_summary(old.run.session_id, "old summary", through)
    current = session_store.start_inbound(
        platform="wecom", conversation_id="chat-1", sender_id="user-1",
        message_id="current", user_content="泵选型",
        initial_checkpoint={"action": "model", "messages": []},
    )
    manager = ContextManager(session_store, memory_service=memories)

    built = manager.build(
        current.run.session_id, current.run.id,
        identity=identity(message="current"), query="泵选型",
    )

    assert built.messages[0]["summary_version"] == 1
    assert "Relevant long-term memory" in built.messages[1]["content"]
    assert "wecom/memory-source" in built.messages[1]["content"]
    assert built.messages[-1] == {"role": "user", "content": "泵选型"}


def test_memory_configuration_is_strongly_typed(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "memory:\n"
        "  enabled: true\n"
        "  store_path: state/memory.db\n"
        "  default_ttl_days: 30\n"
        "  max_results: 7\n",
        encoding="utf-8",
    )
    for name in (
        "AGENT_MEMORY_ENABLED", "AGENT_MEMORY_STORE",
        "AGENT_MEMORY_DEFAULT_TTL_DAYS", "AGENT_MEMORY_MAX_RESULTS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(config)

    assert settings.memory == MemorySettings(
        enabled=True,
        store_path=Path("state/memory.db"),
        default_ttl_days=30,
        max_results=7,
    )
