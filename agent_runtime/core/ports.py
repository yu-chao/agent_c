from __future__ import annotations

from typing import Any, Callable, Protocol

from agent_runtime.contracts import ModelRequest, ModelResponse, ToolCall


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


class NullHooks:
    def trigger(self, event: str, *args: Any) -> None:
        return None
