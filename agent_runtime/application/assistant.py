from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agent_runtime.approval import RuntimeIdentity
from agent_runtime.bootstrap import build_runtime
from agent_runtime.core import AgentRuntime
from agent_runtime.gateway.models import InboundMessage
from agent_runtime.settings import Settings


logger = logging.getLogger(__name__)


class AssistantService:
    def __init__(
        self,
        runtime: AgentRuntime | None = None,
        *,
        settings: Settings | None = None,
        workdir: Path | None = None,
    ):
        self.runtime = runtime or build_runtime(
            settings=settings,
            workdir=workdir,
        )
        store = self.runtime.approval_store
        if store:
            for request in store.list_uncertain():
                logger.error(
                    'approval_result_unknown id=%s tool=%s status=%s',
                    request.id,
                    request.tool_name,
                    request.status,
                )
        session_store = self.runtime.session_store
        if session_store:
            for run in session_store.interrupt_incomplete_runs():
                logger.warning(
                    'run_interrupted_on_startup id=%s session=%s',
                    run.id,
                    run.session_id,
                )

    async def handle(self, message: InboundMessage):
        identity = RuntimeIdentity(
            message.platform,
            message.conversation_id,
            message.sender_id,
            message.message_id,
            message.metadata,
        )
        return await asyncio.to_thread(
            self.runtime.run_turn,
            message.to_agent_input(),
            identity,
        )

    async def decide_approval(
        self,
        approval_id,
        action,
        identity,
        event_message_id,
    ):
        store = self.runtime.approval_store
        if store is None:
            raise RuntimeError('Approval storage is disabled')
        return await asyncio.to_thread(
            store.decide,
            approval_id,
            action,
            identity,
            event_message_id,
        )

    async def resume_approval(self, approval_id):
        return await asyncio.to_thread(self.runtime.resume, approval_id)

    async def cancel_approval(self, request):
        store = self.runtime.approval_store
        if store is None:
            raise RuntimeError('Approval storage is disabled')
        decision = await asyncio.to_thread(
            store.decide,
            request.id,
            'approval.reject',
            request.identity,
            f'card_send_failed_{request.id}',
        )
        if decision.accepted:
            await asyncio.to_thread(store.mark_consumed, request.id)
        return decision

    def recoverable_approvals(self):
        return self.runtime.recoverable_approvals()

    def recoverable_runs(self):
        store = self.runtime.session_store
        return store.list_recoverable_runs() if store else []

    async def resume_run(self, run_id):
        return await asyncio.to_thread(self.runtime.resume_run, run_id)
