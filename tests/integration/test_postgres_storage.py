from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_runtime.approval import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStatus,
    PostgresApprovalStore,
    RuntimeIdentity,
    SQLiteApprovalStore,
)
from agent_runtime.migrations import postgres_migrations
from agent_runtime.memory import (
    MemoryService,
    PostgresMemoryStore,
    SQLiteMemoryStore,
)
from agent_runtime.sessions import (
    PostgresSessionStore,
    RunStatus,
    SQLiteSessionStore,
)
from agent_runtime.tasks import PostgresRunQueue


def _postgres_dsn() -> str:
    dsn = os.getenv("AGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AGENT_TEST_POSTGRES_DSN is not configured")
    pytest.importorskip("psycopg")
    return dsn


@pytest.fixture(params=("sqlite", "postgres"))
def session_store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteSessionStore(tmp_path / "sessions.db", owner_id="owner-a")
    return PostgresSessionStore(_postgres_dsn(), owner_id="owner-a")


@pytest.fixture(params=("sqlite", "postgres"))
def approval_store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteApprovalStore(tmp_path / "approvals.db")
    return PostgresApprovalStore(_postgres_dsn())


@pytest.fixture(params=("sqlite", "postgres"))
def memory_service(request, tmp_path):
    if request.param == "sqlite":
        store = SQLiteMemoryStore(tmp_path / "memory.db")
    else:
        store = PostgresMemoryStore(_postgres_dsn())
    return MemoryService(store)


def test_explicit_migrations_are_contiguous_and_include_queue():
    migrations = postgres_migrations()
    assert [migration.version for migration in migrations] == [1, 2, 3]
    assert "CREATE TABLE run_queue" in migrations[0].sql
    assert "CREATE TABLE memories" in migrations[1].sql
    assert "CREATE TABLE admin_operations" in migrations[2].sql
    assert "CREATE TABLE audit_events" in migrations[2].sql


def test_session_storage_contract(session_store):
    key = uuid.uuid4().hex
    started = session_store.start_inbound(
        platform="contract", conversation_id=key, sender_id="user",
        message_id=key, user_content="hello",
        initial_checkpoint={"action": "model", "messages": []},
    )
    duplicate = session_store.start_inbound(
        platform="contract", conversation_id=key, sender_id="user",
        message_id=key, user_content="ignored",
        initial_checkpoint={"action": "model", "messages": []},
    )
    assert started.is_new and not duplicate.is_new
    assert duplicate.run.id == started.run.id

    checkpoint = session_store.save_checkpoint(
        started.run.id, "before_tool", {"step": 1}
    )
    assert checkpoint.sequence == 2
    assert session_store.latest_checkpoint(started.run.id).state == {"step": 1}

    claim = session_store.claim_tool(
        started.run.id, "call-1", "lookup", {"b": 2, "a": 1}
    )
    assert claim.should_execute
    assert session_store.complete_tool(started.run.id, "call-1", "value")
    replay = session_store.claim_tool(
        started.run.id, "call-1", "lookup", {"a": 1, "b": 2}
    )
    assert not replay.should_execute and replay.output == "value"

    assert session_store.complete_run(started.run.id, "done")
    assert session_store.complete_run(started.run.id, "done")
    assert session_store.cached_response(started.run.id) == "done"
    assert [item.content for item in session_store.recent_messages(
        started.run.session_id, 10
    )] == ["hello", "done"]


def test_run_claim_is_compare_and_swap_across_instances(session_store, tmp_path):
    key = uuid.uuid4().hex
    run = session_store.begin_inbound(
        platform="claim", conversation_id=key, sender_id="user", message_id=key
    ).run
    assert session_store.transition_run(run.id, RunStatus.INTERRUPTED)
    if isinstance(session_store, SQLiteSessionStore):
        contenders = [
            SQLiteSessionStore(session_store.path, owner_id=f"owner-{index}")
            for index in range(2)
        ]
    else:
        contenders = [
            PostgresSessionStore(
                session_store.dsn, owner_id=f"owner-{index}", migrate=False
            )
            for index in range(2)
        ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda store: store.claim_run(run.id, {RunStatus.INTERRUPTED}),
            contenders,
        ))
    assert sum(results) == 1


def test_approval_storage_contract(approval_store):
    key = uuid.uuid4().hex
    identity = RuntimeIdentity("contract", key, "user", key)
    first = ApprovalRequest.create(
        identity=identity, tool_call_id="call-1", tool_name="deploy",
        tool_input={"target": "prod"},
        continuation={"run_id": f"run-{key}"}, timeout_seconds=60,
    )
    duplicate = ApprovalRequest.create(
        identity=identity, tool_call_id="call-1", tool_name="deploy",
        tool_input={"target": "prod"},
        continuation={"run_id": f"run-{key}"}, timeout_seconds=60,
    )
    assert approval_store.create(first).id == first.id
    assert approval_store.create(duplicate).id == first.id
    decision = approval_store.decide(
        first.id, ApprovalAction.CONFIRM, identity, f"event-{key}"
    )
    assert decision.accepted
    assert approval_store.claim_execution(first.id) is not None
    assert approval_store.claim_execution(first.id) is None
    assert approval_store.complete(first.id)
    assert approval_store.get(first.id).status is ApprovalStatus.COMPLETED


def test_postgres_queue_only_transports_run_ids():
    dsn = _postgres_dsn()
    queue = PostgresRunQueue(dsn)
    run_id = f"run-{uuid.uuid4().hex}"
    assert queue.enqueue(run_id)
    assert not queue.enqueue(run_id)
    claimed = queue.claim("worker-a")
    assert claimed and claimed.run_id == run_id and claimed.attempts == 1
    assert not queue.acknowledge(run_id, "worker-b")
    assert queue.acknowledge(run_id, "worker-a")


def test_memory_storage_contract(memory_service):
    key = uuid.uuid4().hex
    owner = RuntimeIdentity("contract", "chat", key, f"message-{key}")
    original = memory_service.remember_user_statement(owner, "pump preference")
    corrected = memory_service.correct(
        RuntimeIdentity("contract", "chat", key, f"correction-{key}"),
        original.id,
        "meter preference",
    )
    assert [
        item.content for item in memory_service.what_is_remembered(owner)
    ] == ["meter preference"]
    assert memory_service.store.get(original.id).superseded_by_id == corrected.id
    assert memory_service.forget(owner, corrected.id)
    assert memory_service.store.get(original.id) is None
    assert memory_service.store.get(corrected.id) is None
