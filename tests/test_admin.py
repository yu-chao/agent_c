import sqlite3

import pytest

from agent_runtime.admin import (
    AdminActor,
    AdminAuthorizationError,
    AdminConflictError,
    AdminNotFoundError,
    AdminScope,
    AdminService,
    SQLiteAdminRepository,
    ToolDisposition,
)
from agent_runtime.bootstrap import build_admin_service, build_retention_service
from agent_runtime.sessions import SQLiteSessionStore
from agent_runtime.settings import SessionSettings, Settings


def _actor(tenant="tenant-a", *scopes):
    return AdminActor(
        "operator-1", tenant,
        frozenset(scope.value for scope in scopes),
    )


def _run(store, message_id, tenant):
    return store.start_inbound(
        platform="wecom", conversation_id="chat", sender_id="user",
        message_id=message_id, user_content="question",
        initial_checkpoint={"action": "model", "messages": []},
        metadata={"tenant_id": tenant},
    ).run


def test_admin_queries_are_tenant_scoped_and_hide_foreign_runs(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    own = _run(sessions, "m-a", "tenant-a")
    foreign = _run(sessions, "m-b", "tenant-b")
    service = AdminService(SQLiteAdminRepository(path))
    actor = _actor("tenant-a", AdminScope.READ)

    assert [item.id for item in service.list_runs(actor).items] == [own.id]
    with pytest.raises(AdminNotFoundError):
        service.list_checkpoints(actor, foreign.id)


def test_admin_control_is_audited_and_idempotent(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    run = _run(sessions, "m-a", "tenant-a")
    service = AdminService(SQLiteAdminRepository(path))
    actor = _actor(
        "tenant-a", AdminScope.READ, AdminScope.CONTROL,
        AdminScope.AUDIT_EXPORT,
    )

    first = service.pause_run(
        actor, run.id, reason="人工排查", operation_id="operation-1"
    )
    repeated = service.pause_run(
        actor, run.id, reason="人工排查", operation_id="operation-1"
    )

    assert first == repeated
    assert first.changed
    assert sessions.get_run(run.id).status.value == "interrupted"
    events = service.export_audit(actor)
    assert len(events) == 1
    assert events[0].actor_id == "operator-1"
    assert events[0].reason == "人工排查"

    with pytest.raises(AdminConflictError):
        service.cancel_run(
            actor, run.id, reason="改变请求", operation_id="operation-1"
        )


def test_admin_control_requires_scope_reason_and_operation_id(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    run = _run(sessions, "m-a", "tenant-a")
    service = AdminService(SQLiteAdminRepository(path))

    with pytest.raises(AdminAuthorizationError):
        service.pause_run(
            _actor("tenant-a", AdminScope.READ), run.id,
            reason="reason", operation_id="operation-1",
        )
    with pytest.raises(ValueError, match="reason"):
        service.pause_run(
            _actor("tenant-a", AdminScope.CONTROL), run.id,
            reason="", operation_id="operation-1",
        )


def test_uncertain_tool_can_be_resolved_without_reexecution(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    run = _run(sessions, "m-a", "tenant-a")
    claim = sessions.claim_tool(run.id, "call-1", "deploy", {"value": 1})
    assert claim.should_execute
    service = AdminService(SQLiteAdminRepository(path))
    actor = _actor("tenant-a", AdminScope.RECONCILE, AdminScope.READ)

    result = service.resolve_uncertain_tool(
        actor, run.id, "call-1",
        disposition=ToolDisposition.CONFIRMED_SUCCEEDED,
        output="manually verified", reason="外部系统已确认成功",
        operation_id="resolve-1",
    )

    assert result.changed
    stored = sessions.get_tool(run.id, "call-1", "deploy", {"value": 1})
    assert not stored.should_execute
    assert stored.output == "manually verified"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id='resolve-1'"
        ).fetchone()[0] == 1


def test_confirming_tool_success_requires_manual_output(tmp_path):
    path = tmp_path / "sessions.db"
    sessions = SQLiteSessionStore(path)
    run = _run(sessions, "m-a", "tenant-a")
    sessions.claim_tool(run.id, "call-1", "deploy", {})
    service = AdminService(SQLiteAdminRepository(path))

    with pytest.raises(ValueError, match="output"):
        service.resolve_uncertain_tool(
            _actor("tenant-a", AdminScope.RECONCILE), run.id, "call-1",
            disposition="confirmed_succeeded", reason="checked",
            operation_id="resolve-1",
        )


def test_governance_services_use_configured_session_database(tmp_path):
    settings = Settings(
        session=SessionSettings(store_path=tmp_path / "sessions.db")
    )
    SQLiteSessionStore(settings.session.store_path)

    admin = build_admin_service(settings)
    retention = build_retention_service(settings)

    assert admin.repository.path == settings.session.store_path
    assert retention.repository.path == settings.session.store_path
