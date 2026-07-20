from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Iterable

from agent_runtime.approval import RuntimeIdentity
from agent_runtime.core import AgentRuntime
from agent_runtime.observability import (
    DEFAULT_OBSERVABILITY,
    ensure_trace,
    trace_id_for_message,
)
from .base import MessageGateway
from .models import InboundMessage, OutboundMessage


class GatewayRunner:
    """Runs all transports and routes normalized messages through the agent loop."""

    def __init__(self, runtime: AgentRuntime, gateways: Iterable[MessageGateway]) -> None:
        self.runtime = runtime
        self.gateways = list(gateways)
        self._session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        for gateway in self.gateways:
            gateway.set_message_handler(self.process)

    async def process(self, message: InboundMessage) -> OutboundMessage:
        identity = RuntimeIdentity(
            message.platform,
            message.conversation_id,
            message.sender_id,
            message.message_id,
            message.metadata,
        )
        observability = getattr(
            self.runtime, "observability", DEFAULT_OBSERVABILITY
        )
        started = time.monotonic()
        with ensure_trace(
            trace_id=trace_id_for_message(
                message.platform, message.message_id
            ),
            session_id=message.session_id,
            message_id=message.message_id,
        ):
            with observability.span(
                "agent.gateway", platform=message.platform
            ) as span:
                try:
                    async with self._session_locks[message.session_id]:
                        answer = await asyncio.to_thread(
                            self.runtime.run_turn,
                            message.to_agent_input(),
                            identity,
                        )
                except Exception:
                    observability.increment(
                        "agent_gateway_messages_total",
                        status="error",
                        platform=message.platform,
                    )
                    observability.observe(
                        "agent_gateway_duration_seconds",
                        time.monotonic() - started,
                        platform=message.platform,
                    )
                    raise
                run_id = (
                    self.runtime._identity_run_id(identity)
                    if hasattr(self.runtime, "_identity_run_id") else None
                )
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("run_id", run_id)
                observability.increment(
                    "agent_gateway_messages_total",
                    status="success",
                    platform=message.platform,
                )
                observability.observe(
                    "agent_gateway_duration_seconds",
                    time.monotonic() - started,
                    platform=message.platform,
                )
        return OutboundMessage(message.conversation_id, answer, message.message_id, message.metadata)

    async def run_forever(self) -> None:
        if not self.gateways:
            raise RuntimeError("At least one gateway is required")
        async with asyncio.TaskGroup() as group:
            for gateway in self.gateways:
                group.create_task(gateway.run_forever(), name=f"gateway:{gateway.platform}")
