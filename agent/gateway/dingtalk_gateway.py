from __future__ import annotations

import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

from .base import MessageGateway
from .models import InboundMessage, OutboundMessage

ReplySender = Callable[[OutboundMessage], Awaitable[None]]


class DingTalkGateway(MessageGateway):
    """Accepts events from a DingTalk Stream, HTTP callback, or message broker."""

    platform = "dingtalk"

    def __init__(self, events: AsyncIterable[dict[str, Any]], reply_sender: ReplySender) -> None:
        super().__init__()
        self.events = events
        self.reply_sender = reply_sender

    async def run_forever(self) -> None:
        async for payload in self.events:
            await self.send(await self.handle_message(self.parse(payload)))

    async def send(self, message: OutboundMessage) -> None:
        await self.reply_sender(message)

    @staticmethod
    def parse(payload: dict[str, Any]) -> InboundMessage:
        content = payload.get("text") or payload.get("content") or {}
        text = str(content.get("content") or content.get("text") or "") if isinstance(content, dict) else str(content)
        sender = str(payload.get("senderStaffId") or payload.get("senderId") or "")
        conversation = str(payload.get("conversationId") or payload.get("conversation_id") or sender)
        return InboundMessage(
            platform="dingtalk",
            message_id=str(payload.get("msgId") or payload.get("messageId") or uuid.uuid4().hex),
            conversation_id=conversation,
            sender_id=sender,
            text=text.strip(),
            metadata={"session_webhook": payload.get("sessionWebhook"), "robot_code": payload.get("robotCode")},
        )
