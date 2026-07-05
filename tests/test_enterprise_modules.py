from datetime import datetime

from agent_runtime.mcp.mock import MockMCPHub
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


def test_mock_mcp_hub_registers_docs_tools_with_registry():
    hub = MockMCPHub()
    registry = hub.connect("docs")

    tools, handlers = registry.assemble()

    assert tools[0].name == "mcp__docs__search"
    assert handlers["mcp__docs__search"]({"query": "agent"}) == "[docs] Found results for 'agent'"
