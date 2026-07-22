import pytest

from agent_runtime.core.loop import AgentRuntime
from agent_runtime.hooks.manager import HookManager
from agent_runtime.models import ModelResponse, TextBlock, ToolCall, ToolResult
from agent_runtime.security.permissions import PermissionPolicy
from agent_runtime.storage.file_store import FileStore
from agent_runtime.tools.registry import ToolRegistry, ToolSpec


def test_tool_registry_merges_builtin_and_mcp_tools():
    registry = ToolRegistry()
    registry.register(
        ToolSpec("read_file", "Read", {"type": "object", "properties": {}}),
        lambda: "local",
    )
    registry.register_mcp_tools(
        "docs",
        [
            {
                "name": "search",
                "description": "Search docs",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        lambda tool_name, args: f"{tool_name}:{args}",
    )

    tools, handlers = registry.assemble()

    assert [tool.name for tool in tools] == ["read_file", "mcp__docs__search"]
    assert handlers["mcp__docs__search"]({"query": "agent"}) == "search:{'query': 'agent'}"


def test_permission_policy_blocks_denylisted_commands_without_prompting():
    policy = PermissionPolicy()
    call = ToolCall(id="1", name="bash", input={"command": "sudo reboot"})

    assert policy.check(call) == "Permission denied: 'sudo' is on the deny list"


def test_permission_policy_blocks_workspace_escape_for_writes(tmp_path):
    policy = PermissionPolicy(workdir=tmp_path)
    call = ToolCall(id="1", name="write_file", input={"path": "../outside.txt"})

    assert policy.check(call) == "Permission denied: path escapes workspace: ../outside.txt"


def test_file_store_rejects_paths_outside_root(tmp_path):
    store = FileStore(tmp_path)

    with pytest.raises(ValueError, match="escapes storage root"):
        store.write_text("../outside.txt", "bad")


def test_runtime_executes_tool_call_and_continues_until_text_response():
    model = FakeModel(
        [
            ModelResponse(blocks=[ToolCall(id="call_1", name="echo", input={"text": "hi"})], response_id="r1"),
            ModelResponse(blocks=[TextBlock("done")], response_id="r2"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "echo",
            "Echo text",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        lambda text: text,
    )
    hooks = HookManager()
    runtime = AgentRuntime(model=model, tools=registry, hooks=hooks, system_prompt="sys")

    answer = runtime.run_turn("hello")

    assert answer == "done"
    assert model.requests[1].messages[-2] == ToolCall(id="call_1", name="echo", input={"text": "hi"})
    assert model.requests[1].messages[-1].content == "hi"
    assert isinstance(model.requests[1].messages[-1], ToolResult)


def test_runtime_returns_tool_failure_to_model():
    model = FakeModel(
        [
            ModelResponse(
                blocks=[ToolCall(id="call_1", name="broken", input={})],
                response_id="r1",
            ),
            ModelResponse(blocks=[TextBlock("recovered")], response_id="r2"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec("broken", "Always fails", {"type": "object"}),
        _raise_tool_error,
    )
    runtime = AgentRuntime(model=model, tools=registry)

    answer = runtime.run_turn("run broken tool")

    assert answer == "recovered"
    assert model.requests[1].messages[-1] == ToolResult(
        "call_1", "Tool execution failed: boom"
    )


def _raise_tool_error():
    raise OSError("boom")


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return self.responses.pop(0)
