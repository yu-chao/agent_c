from datetime import datetime

from agent_runtime.mcp import MCPHub, MCPServerConfig, StreamableHTTPMCPClient
from agent_runtime.scheduler.cron import cron_matches, validate_cron
from agent_runtime.tasks.graph import TaskGraph


def test_task_graph_blocks_claim_until_dependencies_complete(tmp_path):
    graph = TaskGraph(tmp_path)
    first = graph.create("design")
    second = graph.create("build", blocked_by=[first.id])

    assert graph.claim(second.id, "agent") == f"Cannot start: blocked by: ['{first.id}']"

    graph.claim(first.id, "agent")
    graph.complete(first.id)

    assert graph.claim(second.id, "agent") == f"Claimed {second.id} (build)"


def test_cron_validator_rejects_out_of_range_fields():
    assert validate_cron("61 * * * *") == "cron field 1 value 61 outside 0-59"
    assert validate_cron("*/15 * * * *") is None


def test_cron_matches_step_expression():
    assert cron_matches("*/15 * * * *", datetime(2026, 7, 5, 12, 30))
    assert not cron_matches("*/15 * * * *", datetime(2026, 7, 5, 12, 31))


def test_mcp_hub_loads_and_calls_remote_tools():
    config = MCPServerConfig(
        name="business",
        type="streamable-http",
        url="https://mcp.example.invalid",
    )
    client = StreamableHTTPMCPClient(config)
    requests = []

    async def fake_post(message, include_protocol_header, expect_response=True):
        requests.append((message, include_protocol_header, expect_response))
        if message["method"] == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"protocolVersion": "2025-06-18"},
            }
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search products",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ]
                },
            }
        if message["method"] == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": "matched"}]},
            }
        return {}

    client._post = fake_post
    hub = MCPHub([config])
    hub._clients["business"] = client
    registry = hub.connect("business")

    tools, handlers = registry.assemble()

    assert tools[0].name == "mcp__business__search"
    assert handlers["mcp__business__search"]({"query": "pump"}) == "matched"
    assert [request[0]["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
