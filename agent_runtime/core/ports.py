from __future__ import annotations

from typing import Any, Callable, Protocol

from agent_runtime.contracts import ModelRequest, ModelResponse, ToolCall
from agent_runtime.core.run_state import (
    Checkpoint,
    InboundStart,
    RunRecord,
    RunStatus,
    StoredMessage,
    ToolClaim,
)


class ModelPort(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class ToolCatalog(Protocol):
    def assemble(
        self,
    ) -> tuple[list[Any], dict[str, Callable[[dict[str, Any]], str]]]: ...


class HookDispatcher(Protocol):
    def trigger(self, event: str, *args: Any) -> Any: ...


class PermissionChecker(Protocol):
    def check(self, call: ToolCall) -> Any: ...


class RunRepository(Protocol):
    owner_id: str
    lease_refresh_interval: float

    def start_inbound(
        self,
        *,
        platform: str,
        conversation_id: str,
        sender_id: str,
        message_id: str,
        user_content: str,
        initial_checkpoint: dict[str, Any],
        recent_message_limit: int = 20,
        metadata: dict[str, Any] | None = None,
    ) -> InboundStart: ...
    def get_run(self, run_id: str) -> RunRecord | None: ...
    def transition_run(
        self,
        run_id: str,
        status: RunStatus,
        error: str | None = None,
        *,
        execution_token: int | None = None,
    ) -> bool: ...
    def claim_run(
        self, run_id: str, expected_statuses: set[RunStatus]
    ) -> bool: ...
    def complete_run(
        self, run_id: str, response: str, *, execution_token: int | None = None
    ) -> bool: ...
    def cached_response(self, run_id: str) -> str | None: ...
    def interrupt_incomplete_runs(self) -> list[RunRecord]: ...
    def list_recoverable_runs(self) -> list[RunRecord]: ...
    def renew_run(self, run_id: str, execution_token: int | None = None) -> bool: ...


class MessageRepository(Protocol):
    def append_message(
        self, session_id: str, run_id: str, role: str, content: str
    ) -> StoredMessage: ...
    def recent_messages(self, session_id: str, limit: int) -> list[StoredMessage]: ...


class CheckpointRepository(Protocol):
    def save_checkpoint(
        self,
        run_id: str,
        phase: str,
        state: dict[str, Any],
        *,
        execution_token: int | None = None,
    ) -> Checkpoint: ...
    def latest_checkpoint(self, run_id: str) -> Checkpoint | None: ...


class ToolExecutionRepository(Protocol):
    def get_tool(
        self, run_id: str, call_id: str, tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolClaim | None: ...
    def claim_tool(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_token: int | None = None,
    ) -> ToolClaim: ...
    def complete_tool(
        self,
        run_id: str,
        call_id: str,
        output: str,
        *,
        execution_token: int | None = None,
    ) -> bool: ...
    def fail_tool(
        self,
        run_id: str,
        call_id: str,
        error: str,
        *,
        execution_token: int | None = None,
    ) -> bool: ...


class SessionRepository(
    RunRepository,
    MessageRepository,
    CheckpointRepository,
    ToolExecutionRepository,
    Protocol,
):
    """Composite convenience port implemented by the SQLite unit of work."""


class NullHooks:
    def trigger(self, event: str, *args: Any) -> None:
        return None
