import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.admin import AdminActor, AdminScope, SQLiteAdminRepository
from agent.retention import (
    RetentionService,
    SQLiteRetentionRepository,
)
from agent.sessions import SQLiteSessionStore


def _actor(tenant="tenant-a"):
    return AdminActor(
        "steward-1", tenant,
        frozenset({AdminScope.RETENTION_EXECUTE.value}),
    )


def _run(store, tenant, conversation, message):
    return store.start_inbound(
        platform="wecom", conversation_id=conversation, sender_id="user",
        message_id=message, user_content="question",
        initial_checkpoint={"action": "model", "messages": []},
        metadata={"tenant_id": tenant},
    ).run


def _age(path, run_id, value):
    with sqlite3.connect(path) as db:
        db.execute("UPDATE runs SET created_at=?,updated_at=? WHERE id=?",
                   (value, value, run_id))
        db.execute("UPDATE messages SET created_at=? WHERE run_id=?",
                   (value, run_id))
        db.execute("UPDATE checkpoints SET created_at=? WHERE run_id=?",
                   (value, run_id))


def test_retention_deletes_terminal_data_and_protects_active_sessions(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    completed = _run(sessions, "tenant-a", "done", "message-done")
    sessions.complete_run(completed.id, "answer")
    active = _run(sessions, "tenant-a", "active", "message-active")
    foreign = _run(sessions, "tenant-b", "foreign", "message-foreign")
    sessions.complete_run(foreign.id, "answer")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    for run in (completed, active, foreign):
        _age(path, run.id, old)
    repository = SQLiteRetentionRepository(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO attachments VALUES(?,?,?,?,?)",
            ("attachment-done", "tenant-a", completed.session_id, "done.bin", old),
        )
        db.execute(
            "INSERT INTO attachments VALUES(?,?,?,?,?)",
            ("attachment-active", "tenant-a", active.session_id, "active.bin", old),
        )
    service = RetentionService(repository)

    result = service.purge(
        _actor(), "tenant-a", datetime.now(timezone.utc),
        reason="超过保留期限", operation_id="purge-1",
    )

    assert result.messages_deleted == 2
    assert result.checkpoints_deleted == 1
    assert result.attachments_deleted == 1
    assert sessions.recent_messages(completed.session_id, 10) == []
    assert len(sessions.recent_messages(active.session_id, 10)) == 1
    assert len(sessions.recent_messages(foreign.session_id, 10)) == 2
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT id FROM attachments ORDER BY id"
        ).fetchall() == [("attachment-active",)]


def test_retention_is_idempotent_and_shares_audit_schema_with_admin(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    run = _run(sessions, "tenant-a", "done", "message-done")
    sessions.complete_run(run.id, "answer")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    _age(path, run.id, old)
    SQLiteAdminRepository(path)
    service = RetentionService(SQLiteRetentionRepository(path))
    cutoff = datetime.now(timezone.utc)

    first = service.purge(
        _actor(), "tenant-a", cutoff,
        reason="超过保留期限", operation_id="purge-1",
    )
    repeated = service.purge(
        _actor(), "tenant-a", cutoff,
        reason="超过保留期限", operation_id="purge-1",
    )

    assert repeated == first
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id='purge-1'"
        ).fetchone()[0] == 1


def test_retention_rejects_cross_tenant_and_naive_cutoff(tmp_path):
    path = tmp_path / "sessions.db"
    SQLiteSessionStore(path)
    service = RetentionService(SQLiteRetentionRepository(path))

    with pytest.raises(PermissionError):
        service.purge(
            _actor("tenant-a"), "tenant-b", datetime.now(timezone.utc),
            reason="cleanup", operation_id="purge-1",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        service.purge(
            _actor(), "tenant-a", datetime.now(),
            reason="cleanup", operation_id="purge-2",
        )
