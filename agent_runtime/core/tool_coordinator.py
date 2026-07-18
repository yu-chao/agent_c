from __future__ import annotations

import json
from typing import Any

from agent_runtime.contracts import ToolCall
from agent_runtime.core.ports import SessionRepository
from agent_runtime.core.run_state import RunRecord


class ToolExecutionCoordinator:
    """Applies the durable tool ledger around side-effecting execution."""

    def __init__(self, executor: Any, repository: SessionRepository | None):
        self.executor = executor
        self.repository = repository

    def invoke(self, call: ToolCall, handlers: Any, run: RunRecord | None) -> Any:
        if self.repository is None or run is None:
            return self.executor.invoke(call, handlers)
        claim = self.repository.claim_tool(
            run.id,
            call.id,
            call.name,
            call.input,
            execution_token=run.execution_token,
        )
        if not claim.should_execute:
            if claim.output is not None:
                return claim.output
            reason = (
                "tool result is uncertain after interruption; automatic replay blocked"
                if claim.is_uncertain
                else "previous tool execution failed; automatic replay blocked"
            )
            return json.dumps(
                {"executed": False, "reason": reason}, ensure_ascii=False
            )
        try:
            output = self.executor.invoke(call, handlers)
        except Exception as exc:
            self.repository.fail_tool(
                run.id,
                call.id,
                str(exc),
                execution_token=run.execution_token,
            )
            raise
        if not self.repository.complete_tool(
            run.id,
            call.id,
            str(output),
            execution_token=run.execution_token,
        ):
            raise RuntimeError(f"Failed to persist tool result: {call.id}")
        return output
