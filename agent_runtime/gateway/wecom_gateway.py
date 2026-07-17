from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
import aiohttp
from agent_runtime.logging_utils import safe_preview
from agent_runtime.approval import ApprovalAction, RuntimeIdentity
from agent_runtime.core import PendingApproval
from .models import InboundMessage, MessageType, OutboundMessage
from .wecom_components import (
    WeComApprovalPresenter,
    WeComMediaStore,
    WeComMessageMapper,
)



logger = logging.getLogger(__name__)

MessageHandler = Callable[[InboundMessage], Awaitable[Any]]
ApprovalDecider = Callable[..., Awaitable[Any]]
ApprovalResumer = Callable[[str], Awaitable[Any]]


class WeComGateway:
    """Minimal Enterprise WeChat AI Bot WebSocket client for this prototype."""

    platform = "wecom"

    def __init__(
        self,
        handler: MessageHandler | None = None,
        bot_id: Optional[str] = None,
        secret: Optional[str] = None,
        websocket_url: Optional[str] = None,
        approval_decider: ApprovalDecider | None = None,
        approval_resumer: ApprovalResumer | None = None,
        approval_canceller: Callable[[Any], Awaitable[Any]] | None = None,
        recovery_provider: Callable[[], list[Any]] | None = None,
    ):
        self.bot_id = (bot_id or os.getenv("WECOM_BOT_ID", "")).strip()
        self.secret = (secret or os.getenv("WECOM_BOT_SECRET", "")).strip()
        self.websocket_url = (
            websocket_url or os.getenv("WECOM_WEBSOCKET_URL", "wss://openws.work.weixin.qq.com")
        ).strip()
        self.handler = handler
        self.approval_decider = approval_decider
        self.approval_resumer = approval_resumer
        self.approval_canceller = approval_canceller
        self.recovery_provider = recovery_provider
        self._ws = None
        self._session = None
        self._reply_req_ids: Dict[str, str] = {}
        self.cache_dir = Path(".cache/wecom-images")
        self.message_mapper = WeComMessageMapper()
        self.approval_presenter = WeComApprovalPresenter()
        self.media_store = WeComMediaStore(self.cache_dir)

    def set_message_handler(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def send(self, message: OutboundMessage) -> None:
        req_id = str(message.metadata.get("request_id") or "")
        await self._reply(req_id, message.conversation_id, message.text)

    @staticmethod
    def parse(payload: dict[str, Any]) -> InboundMessage:
        return WeComMessageMapper.parse(payload)

    async def run_forever(self) -> None:
        if not self.bot_id or not self.secret:
            raise ValueError("WECOM_BOT_ID and WECOM_BOT_SECRET are required")
        while True:
            heartbeat_task = None
            try:
                self._session = aiohttp.ClientSession()
                logger.info("wecom_connecting url=%s bot_id=%s", self.websocket_url, self.bot_id)
                # WeCom uses the application-level ping sent by _heartbeat_loop.
                # aiohttp heartbeat expects a WebSocket PONG and closes this
                # connection when WeCom does not send one.
                self._ws = await self._session.ws_connect(self.websocket_url)
                await self._subscribe()
                logger.info("wecom_subscribed")
                await self._recover_approvals()
                heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        logger.debug("wecom_frame_received raw=%s", safe_preview(msg.data, 1000))
                        await self._dispatch(json.loads(msg.data))
                    elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                        logger.warning("wecom_frame_closed type=%s data=%s", msg.type.name, msg.data)
                logger.warning(
                    "wecom_disconnected close_code=%s exception=%s",
                    self._ws.close_code,
                    self._ws.exception(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wecom_connection_error")
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                if self._session:
                    await self._session.close()
            await asyncio.sleep(5)

    async def _recover_approvals(self) -> None:
        if self.recovery_provider is None or self.approval_resumer is None:
            return
        for request in self.recovery_provider():
            asyncio.create_task(
                self._resume_and_send(
                    request.id, request.identity.conversation_id
                ),
                name=f"recover-approval:{request.id}",
            )

    async def _subscribe(self) -> None:
        request_id = await self._send(
            "aibot_subscribe",
            {
                "bot_id": self.bot_id,
                "secret": self.secret,
            },
        )
        msg = await asyncio.wait_for(self._ws.receive(), timeout=10)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f"WeCom subscription closed before acknowledgement: {msg.type.name}")
        payload = json.loads(msg.data)
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        if headers.get("req_id") not in {None, "", request_id}:
            raise ConnectionError("WeCom subscription acknowledgement request ID mismatch")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        errcode = payload.get("errcode", body.get("errcode", 0))
        if int(errcode or 0) != 0:
            errmsg = payload.get("errmsg") or body.get("errmsg") or "unknown error"
            raise ConnectionError(f"WeCom subscription failed: errcode={errcode}, errmsg={errmsg}")

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self._send("ping", {})
            logger.debug("wecom_heartbeat_sent")
            await self._recover_approvals()

    async def _dispatch(self, payload: Dict[str, object]) -> None:
        command = str(payload.get("command") or payload.get("cmd") or "")
        if command in {"aibot_msg_callback", "aibot_callback"}:
            await self._dispatch_message(payload)
        elif command == "aibot_event_callback":
            await self._dispatch_event(payload)

    async def _dispatch_message(self, payload: Dict[str, object]) -> None:
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        msg_id = str(body.get("msgid") or uuid.uuid4().hex)
        headers = payload.get("headers")
        req_id = str(headers.get("req_id") or "") if isinstance(headers, dict) else ""
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or body.get("from_user") or "")
        chat_id = str(body.get("chatid") or sender_id)
        message = InboundMessage(
            platform="wecom",
            message_id=msg_id,
            conversation_id=chat_id,
            sender_id=sender_id,
            text=self._extract_text(body),
            message_type=MessageType.TEXT,
            media_paths=tuple(await self._extract_images(body)),
            metadata={"request_id": req_id},
        )
        if self.handler is None:
            raise RuntimeError("WeCom gateway has no message handler")
        outcome = await self.handler(message)
        if isinstance(outcome, PendingApproval):
            try:
                await self._send_approval(req_id, chat_id, outcome)
            except Exception:
                logger.exception("wecom_approval_card_send_failed approval=%s",
                                 outcome.request.id)
                try:
                    await self._cancel_unsent(outcome.request)
                except Exception:
                    logger.exception(
                        "wecom_approval_cancel_failed approval=%s",
                        outcome.request.id,
                    )
                await self._reply(
                    req_id, chat_id, "无法发送确认卡片，工具未执行，请稍后重试。"
                )
            return
        content = outcome.text if isinstance(outcome, OutboundMessage) else str(outcome)
        await self._reply(req_id, chat_id, content)

    async def _dispatch_event(self, payload: Dict[str, object]) -> None:
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        event_type = self._find_event_value(
            body, "eventtype", "event_type", "eventType"
        )
        if event_type == "disconnected_event":
            logger.warning(
                "wecom_disconnected_event_received; check whether another instance "
                "is connected with the same bot_id"
            )
            return
        if event_type != "template_card_event":
            return
        msg_id = str(body.get("msgid") or uuid.uuid4().hex)
        headers = payload.get("headers")
        req_id = str(headers.get("req_id") or "") if isinstance(headers, dict) else ""
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or body.get("from_user") or "")
        chat_id = str(body.get("chatid") or sender_id)
        approval_id = str(
            self._find_event_value(
                body, "task_id", "taskId"
            ) or ""
        )
        action = str(
            self._find_event_value(
                body, "event_key", "eventKey"
            ) or ""
        )
        if not approval_id or action not in {
            ApprovalAction.CONFIRM,
            ApprovalAction.REJECT,
        }:
            logger.warning(
                "wecom_approval_event_invalid message=%s has_task_id=%s action=%s",
                msg_id,
                bool(approval_id),
                action,
            )
            return
        logger.info(
            "wecom_approval_event_received id=%s action=%s actor=%s",
            approval_id,
            action,
            sender_id,
        )
        if self.approval_decider is None:
            await self._update_approval_card(
                req_id, approval_id, "审批服务不可用", sender_id
            )
            return
        identity = RuntimeIdentity(
            "wecom", chat_id, sender_id, msg_id, {"request_id": req_id}
        )
        decision = await self.approval_decider(
            approval_id, action, identity, msg_id
        )
        if not decision.accepted:
            title = {
                "unauthorized": "无权操作",
                "approval expired": "已过期",
            }.get(decision.message, "审批已处理")
            await self._update_approval_card(
                req_id, approval_id, title, sender_id
            )
            return
        title = (
            "已确认，正在查询"
            if action == ApprovalAction.CONFIRM
            else "已拒绝"
        )
        await self._update_approval_card(req_id, approval_id, title)
        if self.approval_resumer is not None:
            asyncio.create_task(
                self._resume_and_send(approval_id, chat_id),
                name=f"approval:{approval_id}",
            )

    @staticmethod
    def _find_event_value(value: object, *keys: str) -> object | None:
        return WeComMessageMapper.find_event_value(value, *keys)

    async def _send_approval(
        self, req_id: str, chat_id: str, outcome: PendingApproval
    ) -> None:
        card = self.build_approval_card(outcome.request)
        body = {"msgtype": "template_card", "template_card": card}
        if req_id:
            await self._send("aibot_respond_msg", body, req_id=req_id)
        else:
            await self._send("aibot_send_msg", {"chatid": chat_id, **body})

    async def _cancel_unsent(self, request) -> None:
        if self.approval_canceller is not None:
            await self.approval_canceller(request)
            return
        if self.approval_decider is None:
            return
        await self.approval_decider(
            request.id,
            ApprovalAction.REJECT,
            request.identity,
            f"card_send_failed_{uuid.uuid4().hex}",
        )

    async def _resume_and_send(self, approval_id: str, chat_id: str) -> None:
        try:
            outcome = await self.approval_resumer(approval_id)
            if isinstance(outcome, PendingApproval):
                await self._send_approval("", chat_id, outcome)
            else:
                await self._reply("", chat_id, str(outcome))
        except Exception:
            logger.exception("wecom_approval_resume_failed approval=%s", approval_id)
            await self._reply("", chat_id, "审批任务执行失败，请联系管理员处理。")

    async def _update_approval_card(
        self, req_id: str, approval_id: str, title: str,
        userid: str | None = None,
    ) -> None:
        card = {
            "card_type": "button_interaction",
            "main_title": {"title": title[:26]},
            "task_id": approval_id,
        }
        body = {
            "response_type": "update_template_card",
            "template_card": card,
        }
        if userid:
            body["userids"] = [userid]
        await self._send("aibot_respond_update_msg", body, req_id=req_id)

    @staticmethod
    def build_approval_card(request) -> Dict[str, object]:
        return WeComApprovalPresenter.build_card(request)

    @staticmethod
    def _card_value(key: str, value: object) -> str:
        return WeComApprovalPresenter.card_value(key, value)

    def _extract_text(self, body: Dict[str, object]) -> str:
        return WeComMessageMapper.parse(body).text

    async def _extract_images(self, body: Dict[str, object]) -> list[str]:
        return await self.media_store.extract_images(body)

    @staticmethod
    def _guess_image_extension(url: str, content_type: str, data: bytes) -> str:
        return WeComMediaStore.guess_extension(url, content_type, data)

    async def _reply(self, req_id: str, chat_id: str, content: str) -> None:
        if req_id:
            await self._send(
                "aibot_respond_msg",
                {
                    "msgtype": "stream",
                    "stream": {
                        "id": uuid.uuid4().hex,
                        "finish": True,
                        "content": content[:4000],
                    },
                },
                req_id=req_id,
            )
        else:
            await self._send(
                "aibot_send_msg",
                {"chatid": chat_id, "msgtype": "markdown", "markdown": {"content": content[:4000]}},
            )

    async def _send(self, command: str, body: Dict[str, object], req_id: Optional[str] = None) -> str:
        if self._ws is None:
            raise RuntimeError("WeCom websocket is not connected")
        request_id = req_id or uuid.uuid4().hex
        payload = {
                "cmd": command,
                "headers": {"req_id": request_id},
                "body": body,
        }
        logger.debug("wecom_frame_send payload=%s", safe_preview(payload, 1000))
        await self._ws.send_json(payload)
        return request_id

