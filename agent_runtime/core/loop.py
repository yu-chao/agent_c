from __future__ import annotations

from agent_runtime.hooks.manager import HookManager
from agent_runtime.models import ModelRequest, TextBlock, ToolCall, ToolResult
from agent_runtime.security.permissions import PermissionPolicy
from agent_runtime.tools.registry import ToolRegistry


class AgentRuntime:
    def __init__(
        self,
        model,
        tools: ToolRegistry,
        hooks: HookManager | None = None,
        permission_policy: PermissionPolicy | None = None,
        system_prompt: str = "You are a coding agent.",
        max_turns: int = 30,
    ):
        self.model = model
        self.tools = tools
        self.hooks = hooks or HookManager()
        self.permission_policy = permission_policy
        self.system_prompt = system_prompt
        self.max_turns = max_turns

    def run_turn(self, user_input: str) -> str:
        self.hooks.trigger("UserPromptSubmit", user_input)
        messages = [{"role": "user", "content": user_input}]
        previous_response_id = None

        for _ in range(self.max_turns):
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

            tool_calls = response.tool_calls
            if not tool_calls:
                self.hooks.trigger("Stop", messages)
                return response.text

            results = []
            for call in tool_calls:
                blocked = self.hooks.trigger("PreToolUse", call)
                if blocked is None and self.permission_policy:
                    blocked = self.permission_policy.check(call)
                if blocked is not None:
                    output = str(blocked)
                else:
                    output = self._execute_tool(call, handlers)
                    self.hooks.trigger("PostToolUse", call, output)
                results.append(ToolResult(tool_call_id=call.id, content=str(output)))
            messages.extend(response.blocks)
            messages.extend(results)
        return "Agent stopped after reaching max_turns."

    def _execute_tool(self, call: ToolCall, handlers) -> str:
        handler = handlers.get(call.name)
        if handler is None:
            return f"Unknown tool: {call.name}"
        try:
            return str(handler(call.input))
        except TypeError as exc:
            return f"Error: {exc}"
