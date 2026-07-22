from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    EVENT = "event"


@dataclass(frozen=True)
class InboundMessage:
    """Platform-neutral message passed from a gateway to the agent loop."""

    platform: str
    message_id: str
    conversation_id: str
    sender_id: str
    text: str = ""
    message_type: MessageType = MessageType.TEXT
    media_paths: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        return f"{self.platform}:{self.conversation_id}"

    def to_agent_input(self) -> str:
        parts = [self.text.strip()]
        if self.media_paths:
            parts.append("Attachments:\n" + "\n".join(self.media_paths))
        return "\n\n".join(part for part in parts if part)


@dataclass(frozen=True)
class OutboundMessage:
    conversation_id: str
    text: str
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
