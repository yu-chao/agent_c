from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ThinkingBlock:
    thinking: str
    signature: str = ''


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str


MessageBlock = TextBlock | ThinkingBlock | ToolCall | ToolResult


@dataclass
class ModelRequest:
    messages: list[MessageBlock | dict[str, Any]]
    system: str
    tools: list[Any] = field(default_factory=list)
    max_tokens: int = 8000
    previous_response_id: str | None = None
    on_fallback: Callable[[str, str], None] | None = None


@dataclass
class ModelResponse:
    blocks: list[TextBlock | ThinkingBlock | ToolCall]
    response_id: str | None = None
    provider: str | None = None
    model: str | None = None
    attempts: int = 1
    used_fallback: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    @property
    def text(self) -> str:
        return '\n'.join(
            block.text
            for block in self.blocks
            if isinstance(block, TextBlock)
        )

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            block
            for block in self.blocks
            if isinstance(block, ToolCall)
        ]


class ModelProvider(Protocol):
    provider: str
    model: str

    def generate(self, request: ModelRequest) -> ModelResponse: ...
