from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from agent.approval import (
    ApprovalAction,
    ApprovalCoordinator,
    ApprovalRequest,
    ApprovalStatus,
    RuntimeIdentity,
    SQLiteApprovalStore,
)
from agent.core import AgentRuntime, Completed, PendingApproval
from agent.models import ModelResponse, TextBlock, ToolCall
from agent.security import PermissionAction, PermissionPolicy
from agent.tools import ToolRegistry, ToolSpec


TOOL = "mcp__PlantMartBusiness__queryProductInfoUsingPOST"


def test_store_upgrade_backfills_approval_links_without_duplicates(tmp_path):
    path = tmp_path / "approvals.db"
    SQLiteApprovalStore(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO approvals(id,platform,conversation_id,sender_id,message_id,"
            "metadata_json,tool_call_id,tool_name,tool_input_json,arguments_hash,"
            "continuation_json,status,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old-1", "wecom", "chat", "user", "message", "{}", "call-1",
             "lookup", "{}", "hash", json.dumps({"run_id": "run-1"}),
             "pending", "2025-01-01T00:00:00+00:00",
             "2025-01-01T01:00:00+00:00"),
        )
        db.execute("PRAGMA user_version=0")

    SQLiteApprovalStore(path)
    SQLiteApprovalStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT run_id,tool_call_id,approval_id FROM approval_links"
        ).fetchall() == [("run-1", "call-1", "old-1")]


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def make_runtime(tmp_path, calls, timeout=600):
    model = FakeModel([
        ModelResponse([
            ToolCall("call-1", TOOL, {"operateType": 3, "skuCode": "SKU-1"}),
            ToolCall("call-2", "echo", {"text": "later"}),
        ], "response-1"),
        ModelResponse([TextBlock("done")], "response-2"),
    ])
    registry = ToolRegistry()
    registry.register(
        ToolSpec(TOOL, "business", {"type": "object", "properties": {}}),
        lambda **values: calls.append(values) or "business-result",
    )
    registry.register(
        ToolSpec("echo", "echo", {"type": "object", "properties": {}}),
        lambda text: calls.append(text) or text,
    )
    store = SQLiteApprovalStore(tmp_path / "approvals.db")
    runtime = AgentRuntime(
        model,
        registry,
        permission_policy=PermissionPolicy(tmp_path, [TOOL]),
        approval_store=store,
        approval_timeout_seconds=timeout,
    )
    return runtime, store, model


def identity(sender="user-1", conversation="chat-1"):
    return RuntimeIdentity("wecom", conversation, sender, "message-1")


def test_policy_distinguishes_allow_deny_and_approval(tmp_path):
    policy = PermissionPolicy(tmp_path, [TOOL])
    assert policy.check(ToolCall("1", "echo", {})).action is PermissionAction.ALLOW
    assert policy.check(ToolCall("2", TOOL, {})).action is PermissionAction.REQUIRE_APPROVAL
    denied = policy.check(ToolCall("3", "write_file", {"path": "../bad"}))
    assert denied.action is PermissionAction.DENY


def test_approval_executes_once_then_continues_remaining_calls(tmp_path):
    calls = []
    runtime, store, model = make_runtime(tmp_path, calls)
    pending = runtime.run_turn("query", identity())
    assert isinstance(pending, PendingApproval)
    assert calls == []
    decision = store.decide(
        pending.request.id, ApprovalAction.CONFIRM, identity(), "event-1"
    )
    assert decision.accepted

    result = runtime.resume(pending.request.id)

    assert result == "done"
    assert calls == [
        {"operateType": 3, "skuCode": "SKU-1"},
        "later",
    ]
    assert store.get(pending.request.id).status is ApprovalStatus.COMPLETED
    assert runtime.resume(pending.request.id).startswith("Approval was already")
    assert len(calls) == 2
    assert model.requests[1].previous_response_id == "response-1"


def test_rejection_and_timeout_never_execute_tool(tmp_path):
    for timeout, action in ((600, ApprovalAction.REJECT), (0, None)):
        calls = []
        runtime, store, _ = make_runtime(tmp_path / str(timeout), calls, timeout)
        pending = runtime.run_turn("query", identity())
        if action:
            store.decide(pending.request.id, action, identity(), f"event-{timeout}")
        result = runtime.resume(pending.request.id)
        assert isinstance(result, Completed)
        assert calls == ["later"]
        assert '"executed": false' in runtime.model.requests[1].messages[-2].content


def test_store_rejects_wrong_identity_and_deduplicates_concurrent_clicks(tmp_path):
    path = tmp_path / "approvals.db"
    store = SQLiteApprovalStore(path)
    request = ApprovalRequest.create(
        identity=identity(),
        tool_call_id="call",
        tool_name=TOOL,
        tool_input={"skuCode": "A"},
        continuation={},
        timeout_seconds=600,
    )
    store.create(request)
    store = SQLiteApprovalStore(path)
    assert store.get(request.id).tool_input == {"skuCode": "A"}
    wrong = store.decide(
        request.id, ApprovalAction.CONFIRM, identity("other"), "wrong-event"
    )
    assert not wrong.accepted
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda event: store.decide(
                request.id, ApprovalAction.CONFIRM, identity(), event
            ),
            ("event-a", "event-b"),
        ))
    assert sum(result.accepted for result in results) == 1
    assert [item.id for item in store.list_resumable()] == [request.id]
    assert store.claim_execution(request.id) is not None
    assert store.claim_execution(request.id) is None
    assert [item.id for item in store.list_uncertain()] == [request.id]


def test_store_deduplicates_approval_for_same_run_and_tool_call(tmp_path):
    store = SQLiteApprovalStore(tmp_path / "approvals.db")
    coordinator = ApprovalCoordinator(store)
    call = ToolCall("call-1", "deploy", {"environment": "prod"})
    continuation = {"run_id": "run-1", "remaining_calls": []}

    first = coordinator.create_request(
        identity=identity(), call=call, continuation=continuation
    )
    second = coordinator.create_request(
        identity=identity(), call=call, continuation=continuation
    )

    assert second.id == first.id


def test_store_expires_pending_requests(tmp_path):
    store = SQLiteApprovalStore(tmp_path / "approvals.db")
    now = datetime.now(timezone.utc)
    request = ApprovalRequest.create(
        identity=identity(),
        tool_call_id="call",
        tool_name=TOOL,
        tool_input={},
        continuation={},
        timeout_seconds=1,
        now=now,
    )
    store.create(request)
    expired = store.expire_pending(now=now + timedelta(seconds=2))
    assert [item.id for item in expired] == [request.id]
    assert store.get(request.id).status is ApprovalStatus.EXPIRED
    assert [item.id for item in store.list_resumable()] == [request.id]
