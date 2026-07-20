from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from agent_runtime.approval import RuntimeIdentity
from agent_runtime.bootstrap import build_runtime
from agent_runtime.core import AgentRuntime
from agent_runtime.gateway.models import InboundMessage
from agent_runtime.observability import ensure_trace, trace_id_for_message
from agent_runtime.settings import Settings


logger = logging.getLogger(__name__)


class AssistantService:
    def __init__(
        self,
        runtime: AgentRuntime | None = None,
        *,
        settings: Settings | None = None,
        workdir: Path | None = None,
        task_service=None,
        scheduler_service=None,
    ):
        self.runtime = runtime or build_runtime(
            settings=settings,
            workdir=workdir,
        )
        self.task_service = task_service
        self.scheduler_service = scheduler_service
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
                if self.task_service is not None:
                    self.task_service.reconcile_run(run.id)

    async def handle(self, message: InboundMessage):
        identity = RuntimeIdentity(
            message.platform,
            message.conversation_id,
            message.sender_id,
            message.message_id,
            message.metadata,
        )
        with ensure_trace(
            trace_id=trace_id_for_message(
                message.platform, message.message_id
            ),
            session_id=message.session_id,
            message_id=message.message_id,
        ):
            with self.runtime.observability.span(
                "agent.gateway", platform=message.platform
            ) as span:
                started = time.monotonic()
                try:
                    result = await asyncio.to_thread(
                        self.runtime.run_turn,
                        message.to_agent_input(),
                        identity,
                    )
                except Exception:
                    self.runtime.observability.increment(
                        "agent_gateway_messages_total",
                        status="error",
                        platform=message.platform,
                    )
                    self.runtime.observability.observe(
                        "agent_gateway_duration_seconds",
                        time.monotonic() - started,
                        platform=message.platform,
                    )
                    raise
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute(
                        "run_id", self.runtime._identity_run_id(identity)
                    )
                self.runtime.observability.increment(
                    "agent_gateway_messages_total",
                    status="success",
                    platform=message.platform,
                )
                self.runtime.observability.observe(
                    "agent_gateway_duration_seconds",
                    time.monotonic() - started,
                    platform=message.platform,
                )
                return result

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
        store = self.runtime.approval_store
        request = store.get(approval_id) if store is not None else None
        result = await asyncio.to_thread(self.runtime.resume, approval_id)
        run_id = request.continuation.get("run_id") if request else None
        if self.task_service is not None and run_id:
            await asyncio.to_thread(self.task_service.reconcile_run, run_id)
        return result

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

    async def run_task(self, task_id, trigger_id=None):
        if self.task_service is None:
            raise RuntimeError("Task service is disabled")
        return await asyncio.to_thread(
            self.task_service.execute, task_id, trigger_id
        )

    async def resume_task(self, task_id):
        if self.task_service is None:
            raise RuntimeError("Task service is disabled")
        return await asyncio.to_thread(self.task_service.resume, task_id)

    async def pause_task(self, task_id):
        if self.task_service is None:
            raise RuntimeError("Task service is disabled")
        return await asyncio.to_thread(self.task_service.pause, task_id)

    async def cancel_task(self, task_id):
        if self.task_service is None:
            raise RuntimeError("Task service is disabled")
        return await asyncio.to_thread(self.task_service.cancel, task_id)

    async def dispatch_schedules(self, at):
        if self.scheduler_service is None:
            raise RuntimeError("Scheduler service is disabled")
        return await asyncio.to_thread(self.scheduler_service.dispatch_due, at)

    async def recover_tasks(self):
        if self.task_service is None:
            raise RuntimeError("Task service is disabled")
        return await asyncio.to_thread(self.task_service.recover)

    async def recover_schedules(self):
        if self.scheduler_service is None:
            raise RuntimeError("Scheduler service is disabled")
        return await asyncio.to_thread(self.scheduler_service.recover)

    async def remember(
        self, identity, content, *, session_id=None, visibility='private'
    ):
        service = self.runtime.memory_service
        if service is None:
            raise RuntimeError('Long-term memory is disabled')
        return await asyncio.to_thread(
            service.remember_user_statement,
            identity,
            content,
            session_id=session_id,
            visibility=visibility,
        )

    async def memories(self, identity):
        service = self.runtime.memory_service
        if service is None:
            raise RuntimeError('Long-term memory is disabled')
        return await asyncio.to_thread(service.what_is_remembered, identity)

    async def correct_memory(
        self, identity, memory_id, content, *, session_id=None
    ):
        service = self.runtime.memory_service
        if service is None:
            raise RuntimeError('Long-term memory is disabled')
        return await asyncio.to_thread(
            service.correct,
            identity,
            memory_id,
            content,
            session_id=session_id,
        )

    async def forget_memory(self, identity, memory_id):
        service = self.runtime.memory_service
        if service is None:
            raise RuntimeError('Long-term memory is disabled')
        return await asyncio.to_thread(service.forget, identity, memory_id)
