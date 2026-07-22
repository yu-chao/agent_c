import asyncio
import json
import logging

import pytest

from agent.approval import RuntimeIdentity, SQLiteApprovalStore
from agent.contracts import ModelResponse, TextBlock, ToolCall
from agent.core import AgentRuntime
from agent.logging_utils import CorrelationFilter, StructuredJsonFormatter
from agent.gateway.models import InboundMessage
from agent.gateway.runner import GatewayRunner
from agent.observability import (
    InMemorySpanExporter,
    MetricsRegistry,
    Observability,
    Tracer,
    bind_context,
)
from agent.sessions import SQLiteSessionStore
from agent.security import PermissionPolicy
from agent.tools import ToolRegistry, ToolSpec


class FakeModel:
    provider = "fake"
    model = "fake-1"

    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, request):
        return self.responses.pop(0)


def _counter(snapshot, name, **labels):
    expected = tuple(sorted((key, str(value)) for key, value in labels.items()))
    return snapshot.counters.get((name, expected), 0)


def test_structured_log_contains_correlation_and_redacts_sensitive_fields():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "handled", (), None
    )
    record.details = {
        "Authorization": "Bearer secret",
        "nested": {"api_token": "secret", "allowed": "visible"},
    }
    with bind_context(
        trace_id="trace-1",
        run_id="run-1",
        message_id="message-1",
    ):
        assert CorrelationFilter().filter(record)
        payload = json.loads(StructuredJsonFormatter().format(record))

    assert payload["trace_id"] == "trace-1"
    assert payload["run_id"] == "run-1"
    assert payload["message_id"] == "message-1"
    assert payload["fields"]["details"] == {
        "Authorization": "***",
        "nested": {"api_token": "***", "allowed": "visible"},
    }


def test_run_can_be_queried_as_complete_trace_and_records_metrics(tmp_path):
    exporter = InMemorySpanExporter()
    metrics = MetricsRegistry()
    observability = Observability(metrics, Tracer(exporter))
    model = FakeModel(
        [
            ModelResponse(
                [ToolCall("call-1", "echo", {"value": "hello"})],
                provider="fake",
                model="fake-1",
                attempts=2,
                input_tokens=11,
                output_tokens=3,
                cost_usd=0.25,
            ),
            ModelResponse(
                [TextBlock("done")],
                provider="fake",
                model="fake-1",
                input_tokens=7,
                output_tokens=2,
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec("echo", "echo input", {"type": "object"}),
        lambda value: value,
    )
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    runtime = AgentRuntime(
        model,
        registry,
        session_store=store,
        observability=observability,
    )
    identity = RuntimeIdentity("wecom", "chat-1", "user-1", "message-1")

    assert runtime.run_turn("start", identity) == "done"

    run_id = store.get_inbound("wecom", "message-1").run.id
    spans = exporter.spans(run_id=run_id)
    assert {span.name for span in spans} >= {
        "agent.run",
        "agent.model",
        "agent.tool",
    }
    assert len({span.trace_id for span in spans}) == 1
    assert all(span.attributes["session_id"] == "wecom:chat-1" for span in spans)
    assert any(span.attributes.get("model_request_id") for span in spans)
    assert any(span.attributes.get("tool_execution_id") for span in spans)

    snapshot = metrics.snapshot()
    assert _counter(
        snapshot,
        "agent_model_tokens_total",
        direction="input",
        provider="fake",
        model="fake-1",
    ) == 18
    assert _counter(
        snapshot,
        "agent_model_retries_total",
        provider="fake",
        model="fake-1",
    ) == 1
    assert _counter(
        snapshot,
        "agent_model_cost_usd_total",
        provider="fake",
        model="fake-1",
    ) == pytest.approx(0.25)


def test_observability_failures_do_not_interrupt_business_run():
    class BrokenMetrics:
        def increment(self, *args, **kwargs):
            raise RuntimeError("metrics unavailable")

        def observe(self, *args, **kwargs):
            raise RuntimeError("metrics unavailable")

    class BrokenExporter:
        def export(self, span):
            raise RuntimeError("trace unavailable")

    runtime = AgentRuntime(
        FakeModel([ModelResponse([TextBlock("ok")])]),
        ToolRegistry(),
        observability=Observability(BrokenMetrics(), Tracer(BrokenExporter())),
    )

    assert runtime.run_turn("hello") == "ok"

    class BrokenTracer:
        def span(self, *args, **kwargs):
            raise RuntimeError("tracer unavailable")

    runtime = AgentRuntime(
        FakeModel([ModelResponse([TextBlock("still ok")])]),
        ToolRegistry(),
        observability=Observability(BrokenMetrics(), BrokenTracer()),
    )
    assert runtime.run_turn("hello") == "still ok"


def test_gateway_and_approval_spans_include_required_identifiers(tmp_path):
    exporter = InMemorySpanExporter()
    observability = Observability(MetricsRegistry(), Tracer(exporter))
    registry = ToolRegistry()
    registry.register(
        ToolSpec("danger", "dangerous action", {"type": "object"}),
        lambda: "executed",
    )
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    runtime = AgentRuntime(
        FakeModel(
            [
                ModelResponse(
                    [ToolCall("call-approval", "danger", {})],
                    provider="fake",
                    model="fake-1",
                )
            ]
        ),
        registry,
        permission_policy=PermissionPolicy(tmp_path, ("danger",)),
        approval_store=SQLiteApprovalStore(tmp_path / "approvals.db"),
        session_store=store,
        observability=observability,
    )
    runner = GatewayRunner(runtime, [])
    message = InboundMessage(
        "wecom", "message-approval", "chat-1", "user-1", "approve"
    )

    response = asyncio.run(runner.process(message))

    assert response.reply_to == "message-approval"
    run_id = store.get_inbound("wecom", "message-approval").run.id
    spans = exporter.spans(run_id=run_id)
    assert {span.name for span in spans} >= {
        "agent.gateway",
        "agent.run",
        "agent.model",
        "agent.approval",
    }
    approval_span = next(
        span for span in spans if span.name == "agent.approval"
    )
    assert approval_span.attributes["approval_id"].startswith("approval_")
    assert len({span.trace_id for span in spans}) == 1
