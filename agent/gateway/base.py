from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from .models import InboundMessage, OutboundMessage

MessageHandler = Callable[[InboundMessage], Awaitable[OutboundMessage]]


class MessageGateway(ABC):
    platform: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def handle_message(self, message: InboundMessage) -> OutboundMessage:
        if self._handler is None:
            raise RuntimeError(f"{self.platform} gateway has no message handler")
        return await self._handler(message)

    @abstractmethod
    async def run_forever(self) -> None: ...

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None: ...
