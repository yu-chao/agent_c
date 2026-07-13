from __future__ import annotations

import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

from .base import MessageGateway
from .models import InboundMessage, OutboundMessage

ReplySender = Callable[[OutboundMessage], Awaitable[None]]


class WeComGateway(MessageGateway):
    """Enterprise WeChat adapter for long-connection or callback event sources."""

    platform = "wecom"

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
        body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        sender_data = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender = str(sender_data.get("userid") or body.get("from_user") or "")
        text_data = body.get("text") if isinstance(body.get("text"), dict) else {}
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        return InboundMessage(
            platform="wecom",
            message_id=str(body.get("msgid") or uuid.uuid4().hex),
            conversation_id=str(body.get("chatid") or sender),
            sender_id=sender,
            text=str(text_data.get("content") or body.get("content") or "").strip(),
            metadata={"request_id": headers.get("req_id")},
        )
