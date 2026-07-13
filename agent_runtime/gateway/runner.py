from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable

from agent_runtime.core import AgentRuntime
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
        async with self._session_locks[message.session_id]:
            answer = await asyncio.to_thread(self.runtime.run_turn, message.to_agent_input())
        return OutboundMessage(message.conversation_id, answer, message.message_id, message.metadata)

    async def run_forever(self) -> None:
        if not self.gateways:
            raise RuntimeError("At least one gateway is required")
        async with asyncio.TaskGroup() as group:
            for gateway in self.gateways:
                group.create_task(gateway.run_forever(), name=f"gateway:{gateway.platform}")
