from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str


MessageBlock = TextBlock | ToolCall | ToolResult


@dataclass
class ModelRequest:
    messages: list[MessageBlock | dict[str, Any]]
    system: str
    tools: list[Any] = field(default_factory=list)
    max_tokens: int = 8000
    previous_response_id: str | None = None


@dataclass
class ModelResponse:
    blocks: list[TextBlock | ToolCall]
    response_id: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks if isinstance(block, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [block for block in self.blocks if isinstance(block, ToolCall)]


class ModelProvider(Protocol):
    model: str

    def generate(self, request: ModelRequest) -> ModelResponse:
        ...
