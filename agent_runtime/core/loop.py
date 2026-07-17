from __future__ import annotations

import json
import logging

from agent_runtime.approval import (
    ApprovalRequest,
    ApprovalRepository,
    ApprovalStatus,
    RuntimeIdentity,
    ApprovalCoordinator,
)
from agent_runtime.contracts import ModelRequest, ToolCall, ToolResult
from agent_runtime.core.continuation import decode_blocks, encode_blocks
from agent_runtime.core.ports import (
    HookDispatcher,
    ModelPort,
    NullHooks,
    PermissionChecker,
    ToolCatalog,
)
from agent_runtime.core.results import Completed, PendingApproval
from agent_runtime.core.tool_execution import ToolExecutor
from agent_runtime.security import PermissionAction, PermissionDecision


logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        model: ModelPort,
        tools: ToolCatalog,
        hooks: HookDispatcher | None = None,
        permission_policy: PermissionChecker | None = None,
        system_prompt: str = "You are a coding agent.",
        max_turns: int = 30,
        approval_store: ApprovalRepository | None = None,
        approval_timeout_seconds: int = 600,
    ):
        self.model = model
        self.tools = tools
        self.hooks = hooks or NullHooks()
        self.permission_policy = permission_policy
        self.tool_executor = ToolExecutor(
            self.hooks,
            permission_policy,
        )
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.approval_store = approval_store
        self.approval_timeout_seconds = approval_timeout_seconds
        self.approvals = (
            ApprovalCoordinator(
                approval_store,
                approval_timeout_seconds,
            )
            if approval_store is not None
            else None
        )

    def run_turn(
        self, user_input: str, identity: RuntimeIdentity | None = None
    ) -> Completed | PendingApproval:
        self.hooks.trigger("UserPromptSubmit", user_input)
        return self._run(
            [{"role": "user", "content": user_input}], None, identity, 0
        )

    def _run(self, messages, previous_response_id, identity, start_turn):
        for turn in range(start_turn, self.max_turns):
            tool_specs, handlers = self.tools.assemble()
            response = self.model.generate(
                ModelRequest(
                    messages=messages,
                    system=self.system_prompt,
                    tools=tool_specs,
                    previous_response_id=previous_response_id,
                )
            )
            previous_response_id = response.response_id or previous_response_id
            if not response.tool_calls:
                self.hooks.trigger("Stop", messages)
                return Completed(response.text)
            messages.extend(response.blocks)
            pending = self._process_calls(
                response.tool_calls,
                messages,
                handlers,
                identity,
                previous_response_id,
                turn + 1,
            )
            if pending is not None:
                return pending
        return Completed("Agent stopped after reaching max_turns.")

    def _decision(self, call: ToolCall) -> PermissionDecision:
        return self.tool_executor.decision(call)

    def _process_calls(
        self, calls, messages, handlers, identity, previous_response_id, next_turn
    ):
        results = []
        for index, call in enumerate(calls):
            decision = self._decision(call)
            if decision.action is PermissionAction.REQUIRE_APPROVAL:
                if self.approvals is None or identity is None:
                    results.append(
                        self._not_executed(call, "approval channel unavailable")
                    )
                    continue
                continuation = {
                    "messages": encode_blocks(messages + results),
                    "remaining_calls": encode_blocks(calls[index:]),
                    "previous_response_id": previous_response_id,
                    "next_turn": next_turn,
                }
                request = self.approvals.create_request(
                    identity=identity,
                    call=call,
                    continuation=continuation,
                )
                logger.info(
                    "approval_created id=%s tool=%s status=%s",
                    request.id,
                    request.tool_name,
                    request.status,
                )
                return PendingApproval(request)
            if decision.action is PermissionAction.DENY:
                output = decision.reason
            else:
                output = self.tool_executor.invoke(call, handlers)
            results.append(ToolResult(call.id, str(output)))
        messages.extend(results)
        return None

    def resume(self, approval_id: str) -> Completed | PendingApproval:
        if self.approvals is None:
            return Completed("Approval storage is unavailable.")
        request = self.approvals.load_for_resume(approval_id)
        if request is None:
            return Completed("Approval request was not found.")
        if request.status is ApprovalStatus.PENDING:
            return PendingApproval(request)
        context = request.continuation
        messages = decode_blocks(context["messages"])
        calls = decode_blocks(context["remaining_calls"])
        current = calls[0]
        if not isinstance(current, ToolCall):
            return Completed("Invalid approval continuation.")
        result = self._resume_result(request, current)
        if result is None:
            return Completed("Approval was already processed; tool was not replayed.")
        messages.append(result)
        _, handlers = self.tools.assemble()
        pending = self._process_calls(
            calls[1:],
            messages,
            handlers,
            request.identity,
            context.get("previous_response_id"),
            int(context.get("next_turn", 0)),
        )
        if pending is not None:
            return pending
        return self._run(
            messages,
            context.get("previous_response_id"),
            request.identity,
            int(context.get("next_turn", 0)),
        )

    def _resume_result(self, request: ApprovalRequest, call: ToolCall):
        if request.status in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED):
            if not self.approval_store.mark_consumed(request.id):
                return None
            reason = (
                "user rejected the tool call"
                if request.status is ApprovalStatus.REJECTED
                else "approval expired"
            )
            return self._not_executed(call, reason)
        if request.status is not ApprovalStatus.APPROVED:
            return None
        decision = self._decision(call)
        valid = (
            decision.action is PermissionAction.REQUIRE_APPROVAL
            and self.approvals is not None
            and self.approvals.matches(request, call)
        )
        claimed = self.approval_store.claim_execution(request.id)
        if claimed is None:
            return None
        logger.info(
            "approval_claimed id=%s tool=%s status=%s",
            request.id,
            request.tool_name,
            ApprovalStatus.EXECUTING,
        )
        if not valid:
            self.approval_store.fail(request.id, "approval validation failed")
            return self._not_executed(call, "approval validation failed")
        _, handlers = self.tools.assemble()
        try:
            output = self.tool_executor.invoke(call, handlers)
        except Exception as exc:
            self.approval_store.fail(request.id, str(exc))
            logger.warning(
                "approval_failed id=%s tool=%s status=%s",
                request.id,
                request.tool_name,
                ApprovalStatus.FAILED,
            )
            return ToolResult(call.id, f"Tool execution failed: {exc}")
        self.approval_store.complete(request.id)
        logger.info(
            "approval_completed id=%s tool=%s status=%s",
            request.id,
            request.tool_name,
            ApprovalStatus.COMPLETED,
        )
        return ToolResult(call.id, str(output))

    @staticmethod
    def _not_executed(call: ToolCall, reason: str) -> ToolResult:
        content = json.dumps(
            {"executed": False, "reason": reason}, ensure_ascii=False
        )
        return ToolResult(call.id, content)

    def recoverable_approvals(self) -> list[ApprovalRequest]:
        if self.approvals is None:
            return []
        return self.approvals.recoverable()
