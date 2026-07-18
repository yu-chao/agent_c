import pytest
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import get_type_hints

from agent_runtime.approval import (
    ApprovalAction,
    RuntimeIdentity,
    SQLiteApprovalStore,
)
from agent_runtime.application import AssistantService
from agent_runtime.core import AgentRuntime, InProgress
from agent_runtime.models import ModelResponse, TextBlock, ToolCall
from agent_runtime.sessions import RunStatus, SQLiteSessionStore
from agent_runtime.security import PermissionPolicy
from agent_runtime.tools import ToolRegistry, ToolSpec


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def identity(message_id):
    return RuntimeIdentity("wecom", "chat-1", "user-1", message_id)


def runtime(model, store, registry=None):
    return AgentRuntime(
        model,
        registry or ToolRegistry(),
        session_store=store,
        recent_message_limit=10,
    )


def test_runtime_result_annotations_include_in_progress():
    assert InProgress in get_type_hints(AgentRuntime.run_turn)["return"].__args__
    assert InProgress in get_type_hints(AgentRuntime.resume_run)["return"].__args__


def test_runtime_exposes_store_owner_and_renews_run(tmp_path):
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    agent = runtime(FakeModel([]), store)
    run = store.begin_inbound(
        platform="wecom", conversation_id="chat-1", sender_id="user-1",
        message_id="message-1",
    ).run

    assert agent.owner_id == store.owner_id
    assert agent.renew_run(run.id)


def test_short_term_memory_survives_runtime_restart(tmp_path):
    path = tmp_path / "runtime.db"
    first_model = FakeModel([ModelResponse([TextBlock("你叫张三")])])
    runtime(first_model, SQLiteSessionStore(path)).run_turn(
        "我叫张三", identity("message-1")
    )
    second_model = FakeModel([ModelResponse([TextBlock("张三")])])

    answer = runtime(second_model, SQLiteSessionStore(path)).run_turn(
        "我叫什么？", identity("message-2")
    )

    assert answer == "张三"
    assert second_model.requests[0].messages == [
        {"role": "user", "content": "我叫张三"},
        {"role": "assistant", "content": "你叫张三"},
        {"role": "user", "content": "我叫什么？"},
    ]


def test_duplicate_message_returns_cached_response_without_calling_model(tmp_path):
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    first_model = FakeModel([ModelResponse([TextBlock("only once")])])
    first = runtime(first_model, store).run_turn("hello", identity("same-message"))
    duplicate_model = FakeModel([])

    duplicate = runtime(duplicate_model, store).run_turn(
        "hello", identity("same-message")
    )

    assert first == duplicate == "only once"
    assert duplicate_model.requests == []


def test_duplicate_running_message_returns_in_progress_result(tmp_path):
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    started = store.start_inbound(
        platform="wecom",
        conversation_id="chat-1",
        sender_id="user-1",
        message_id="same-message",
        user_content="hello",
        initial_checkpoint={"action": "model", "messages": []},
    )
    duplicate_model = FakeModel([])

    result = runtime(duplicate_model, store).run_turn(
        "hello", identity("same-message")
    )

    assert isinstance(result, InProgress)
    assert result.run_id == started.run.id
    assert duplicate_model.requests == []


def test_interrupted_model_call_resumes_from_checkpoint(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteSessionStore(path)
    failing = runtime(FakeModel([RuntimeError("connection lost")]), store)

    with pytest.raises(RuntimeError, match="connection lost"):
        failing.run_turn("continue me", identity("message-1"))

    interrupted = store.list_recoverable_runs()
    assert len(interrupted) == 1
    assert interrupted[0].status is RunStatus.INTERRUPTED

    recovered_model = FakeModel([ModelResponse([TextBlock("recovered")])])
    answer = runtime(recovered_model, SQLiteSessionStore(path)).resume_run(
        interrupted[0].id
    )

    assert answer == "recovered"
    assert recovered_model.requests[0].messages == [
        {"role": "user", "content": "continue me"}
    ]


def test_recovery_reuses_completed_tool_result_instead_of_replaying(tmp_path):
    path = tmp_path / "runtime.db"
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec("create_order", "create", {"type": "object"}),
        lambda sku: calls.append(sku) or "order-1",
    )
    model = FakeModel(
        [
            ModelResponse([ToolCall("call-1", "create_order", {"sku": "A"})]),
            RuntimeError("model unavailable"),
        ]
    )
    store = SQLiteSessionStore(path)

    with pytest.raises(RuntimeError, match="model unavailable"):
        runtime(model, store, registry).run_turn("buy", identity("message-1"))
    recovered = FakeModel([ModelResponse([TextBlock("done")])])

    answer = runtime(recovered, SQLiteSessionStore(path), registry).resume_run(
        store.list_recoverable_runs()[0].id
    )

    assert answer == "done"
    assert calls == ["A"]


def test_assistant_startup_marks_abandoned_run_recoverable(tmp_path):
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    abandoned = store.begin_inbound(
        platform="wecom",
        conversation_id="chat-1",
        sender_id="user-1",
        message_id="message-1",
    ).run
    agent = runtime(FakeModel([]), store)

    service = AssistantService(runtime=agent)

    assert [item.id for item in service.recoverable_runs()] == [abandoned.id]


def test_uncertain_approved_tool_does_not_create_replacement_approval(tmp_path):
    tool_name = "create_order"
    registry = ToolRegistry()
    calls = []
    registry.register(
        ToolSpec(tool_name, "create", {"type": "object"}),
        lambda sku: calls.append(sku) or "order-1",
    )
    session_store = SQLiteSessionStore(tmp_path / "sessions.db")
    approval_store = SQLiteApprovalStore(tmp_path / "approvals.db")
    agent = AgentRuntime(
        FakeModel([
            ModelResponse([ToolCall("call-1", tool_name, {"sku": "A"})])
        ]),
        registry,
        permission_policy=PermissionPolicy(tmp_path, [tool_name]),
        approval_store=approval_store,
        session_store=session_store,
    )
    pending = agent.run_turn("buy", identity("message-1"))
    approval_store.decide(
        pending.request.id,
        ApprovalAction.CONFIRM,
        identity("message-1"),
        "approval-event-1",
    )
    approval_store.claim_execution(pending.request.id)
    run = session_store.get_run(pending.request.continuation["run_id"])
    assert session_store.claim_run(run.id, {RunStatus.WAITING_APPROVAL})
    run = session_store.get_run(run.id)
    assert session_store.transition_run(
        run.id, RunStatus.INTERRUPTED,
        execution_token=run.execution_token,
    )

    result = agent.resume_run(run.id)

    assert "approval" in result.lower()
    assert calls == []
    assert approval_store.list_resumable() == []
    assert session_store.get_run(run.id).status is RunStatus.FAILED
    assert "manual" in agent.resume_run(run.id).lower()


def test_resume_approval_checkpoint_reuses_existing_request(tmp_path):
    tool_name = "create_order"
    registry = ToolRegistry()
    registry.register(
        ToolSpec(tool_name, "create", {"type": "object"}),
        lambda sku: "order-1",
    )
    session_store = SQLiteSessionStore(tmp_path / "sessions.db")
    approval_store = SQLiteApprovalStore(tmp_path / "approvals.db")
    agent = AgentRuntime(
        FakeModel([
            ModelResponse([ToolCall("call-1", tool_name, {"sku": "A"})])
        ]),
        registry,
        permission_policy=PermissionPolicy(tmp_path, [tool_name]),
        approval_store=approval_store,
        session_store=session_store,
    )
    pending = agent.run_turn("buy", identity("message-1"))
    run_id = pending.request.continuation["run_id"]
    assert session_store.claim_run(run_id, {RunStatus.WAITING_APPROVAL})
    assert session_store.transition_run(run_id, RunStatus.INTERRUPTED)

    recovered = agent.resume_run(run_id)

    assert recovered.request.id == pending.request.id
    assert session_store.get_run(run_id).status is RunStatus.WAITING_APPROVAL


def test_approved_tool_execution_is_recorded_in_session_ledger(tmp_path):
    tool_name = "create_order"
    registry = ToolRegistry()
    calls = []
    registry.register(
        ToolSpec(tool_name, "create", {"type": "object"}),
        lambda sku: calls.append(sku) or "order-1",
    )
    session_store = SQLiteSessionStore(tmp_path / "sessions.db")
    approval_store = SQLiteApprovalStore(tmp_path / "approvals.db")
    agent = AgentRuntime(
        FakeModel([
            ModelResponse([ToolCall("call-1", tool_name, {"sku": "A"})]),
            ModelResponse([TextBlock("done")]),
        ]),
        registry,
        permission_policy=PermissionPolicy(tmp_path, [tool_name]),
        approval_store=approval_store,
        session_store=session_store,
    )
    pending = agent.run_turn("buy", identity("message-1"))
    approval_store.decide(
        pending.request.id,
        ApprovalAction.CONFIRM,
        identity("message-1"),
        "approval-event-1",
    )

    result = agent.resume(pending.request.id)
    claim = session_store.claim_tool(
        pending.request.continuation["run_id"],
        "call-1",
        tool_name,
        {"sku": "A"},
    )

    assert result == "done"
    assert calls == ["A"]
    assert not claim.should_execute
    assert claim.output == "order-1"


def test_completed_approval_recovers_remaining_continuation_after_crash(tmp_path):
    tool_name = "create_order"
    calls = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(tool_name, "create", {"type": "object"}),
        lambda sku: calls.append(sku) or "must-not-run",
    )
    session_store = SQLiteSessionStore(tmp_path / "sessions.db")
    approval_store = SQLiteApprovalStore(tmp_path / "approvals.db")
    agent = AgentRuntime(
        FakeModel([
            ModelResponse([ToolCall("call-1", tool_name, {"sku": "A"})]),
            ModelResponse([TextBlock("continued")]),
        ]),
        registry,
        permission_policy=PermissionPolicy(tmp_path, [tool_name]),
        approval_store=approval_store,
        session_store=session_store,
    )
    pending = agent.run_turn("buy", identity("message-1"))
    approval_store.decide(
        pending.request.id, ApprovalAction.CONFIRM,
        identity("message-1"), "approval-event-1",
    )
    run_id = pending.request.continuation["run_id"]
    assert session_store.claim_run(run_id, {RunStatus.WAITING_APPROVAL})
    claimed_run = session_store.get_run(run_id)
    assert approval_store.claim_execution(pending.request.id)
    assert session_store.claim_tool(
        run_id, "call-1", tool_name, {"sku": "A"},
        execution_token=claimed_run.execution_token,
    ).should_execute
    assert session_store.complete_tool(
        run_id, "call-1", "order-1",
        execution_token=claimed_run.execution_token,
    )
    assert approval_store.complete(pending.request.id)
    assert session_store.transition_run(
        run_id, RunStatus.INTERRUPTED,
        execution_token=claimed_run.execution_token,
    )

    result = agent.resume_run(run_id)

    assert result == "continued"
    assert calls == []
    assert session_store.get_run(run_id).status is RunStatus.COMPLETED


def test_runtime_heartbeat_prevents_live_run_from_being_recovered(tmp_path):
    started = Event()

    class SlowModel:
        def generate(self, request):
            started.set()
            time.sleep(0.45)
            return ModelResponse([TextBlock("done")])

    path = tmp_path / "sessions.db"
    owner = SQLiteSessionStore(path, lease_seconds=0.15)
    agent = runtime(SlowModel(), owner)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            agent.run_turn, "slow", identity("message-heartbeat")
        )
        assert started.wait(timeout=1)
        time.sleep(0.3)
        contender = SQLiteSessionStore(path, lease_seconds=0.15)
        assert contender.interrupt_incomplete_runs() == []
        assert future.result(timeout=2) == "done"
