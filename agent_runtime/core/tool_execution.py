from __future__ import annotations

from typing import Any, Callable

from agent_runtime.contracts import ToolCall
from agent_runtime.core.ports import HookDispatcher, PermissionChecker
from agent_runtime.security import (
    PermissionAction,
    PermissionDecision,
)


class ToolExecutor:
    def __init__(
        self,
        hooks: HookDispatcher,
        permission_policy: PermissionChecker | None = None,
    ):
        self.hooks = hooks
        self.permission_policy = permission_policy

    def decision(self, call: ToolCall) -> PermissionDecision:
        blocked = self.hooks.trigger('PreToolUse', call)
        if blocked is not None:
            return PermissionDecision(PermissionAction.DENY, str(blocked))
        if self.permission_policy:
            return self.permission_policy.check(call)
        return PermissionDecision(PermissionAction.ALLOW)

    def invoke(
        self,
        call: ToolCall,
        handlers: dict[str, Callable[[dict[str, Any]], str]],
    ) -> str:
        handler = handlers.get(call.name)
        if handler is None:
            return f'Unknown tool: {call.name}'
        output = str(handler(call.input))
        self.hooks.trigger('PostToolUse', call, output)
        return output
