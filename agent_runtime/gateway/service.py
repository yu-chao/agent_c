from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent_runtime.approval import RuntimeIdentity, SQLiteApprovalStore
from agent_runtime.core import AgentRuntime
from agent_runtime.gateway.models import InboundMessage
from agent_runtime.hooks import HookManager
from agent_runtime.mcp import MCPHub
from agent_runtime.models import create_model_provider
from agent_runtime.security import PermissionPolicy


logger = logging.getLogger(__name__)


class BusinessAssistantService:
    def __init__(self, runtime: AgentRuntime | None = None):
        load_dotenv()
        config = _load_config()
        approval = config.get("approval", {})
        if runtime is None:
            enabled = bool(approval.get("enabled", True))
            store = (
                SQLiteApprovalStore(approval.get("store_path", ".runtime/approvals.db"))
                if enabled
                else None
            )
            tools = approval.get("tools", []) if enabled else []
            runtime = AgentRuntime(
                model=create_model_provider(),
                tools=create_default_registry(),
                hooks=HookManager(),
                permission_policy=PermissionPolicy(Path.cwd(), tools),
                system_prompt="You are a coding agent. Use tools when useful.",
                approval_store=store,
                approval_timeout_seconds=int(
                    approval.get("timeout_seconds", 600)
                ),
            )
        self.runtime = runtime
        store = getattr(self.runtime, "approval_store", None)
        if store:
            for request in store.list_uncertain():
                logger.error(
                    "approval_result_unknown id=%s tool=%s status=%s",
                    request.id,
                    request.tool_name,
                    request.status,
                )

    async def handle(self, message: InboundMessage):
        logger.info(
            "message_received platform=%s conversation=%s sender=%s message=%s",
            message.platform,
            message.conversation_id,
            message.sender_id,
            message.message_id,
        )
        identity = RuntimeIdentity(
            message.platform,
            message.conversation_id,
            message.sender_id,
            message.message_id,
            message.metadata,
        )
        return await asyncio.to_thread(
            self.runtime.run_turn, message.to_agent_input(), identity
        )

    async def decide_approval(
        self, approval_id, action, identity, event_message_id
    ):
        store = self.runtime.approval_store
        if store is None:
            raise RuntimeError("Approval storage is disabled")
        decision = await asyncio.to_thread(
            store.decide, approval_id, action, identity, event_message_id
        )
        tool_name = decision.request.tool_name if decision.request else "unknown"
        logger.info(
            "approval_decision id=%s tool=%s status=%s actor=%s accepted=%s",
            approval_id,
            tool_name,
            decision.status,
            identity.sender_id,
            decision.accepted,
        )
        return decision

    async def resume_approval(self, approval_id):
        return await asyncio.to_thread(self.runtime.resume, approval_id)

    async def cancel_approval(self, request):
        store = self.runtime.approval_store
        decision = await asyncio.to_thread(
            store.decide,
            request.id,
            "approval.reject",
            request.identity,
            f"card_send_failed_{request.id}",
        )
        if decision.accepted:
            await asyncio.to_thread(store.mark_consumed, request.id)
        return decision

    def recoverable_approvals(self):
        return self.runtime.recoverable_approvals()


def create_default_registry():
    return MCPHub.from_config().connect("PlantMartBusiness")


def _load_config() -> dict:
    path = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}
