from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib import request
from urllib.parse import urlparse
import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from agent_runtime.logging_utils import safe_preview
from agent_runtime.approval import ApprovalAction, RuntimeIdentity
from agent_runtime.core import PendingApproval
from .models import InboundMessage, MessageType



logger = logging.getLogger(__name__)

MessageHandler = Callable[[InboundMessage], Awaitable[Any]]
ApprovalDecider = Callable[..., Awaitable[Any]]
ApprovalResumer = Callable[[str], Awaitable[Any]]


class WeComGateway:
    """Minimal Enterprise WeChat AI Bot WebSocket client for this prototype."""

    def __init__(
        self,
        handler: MessageHandler,
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

    async def run_forever(self) -> None:
        if not self.bot_id or not self.secret:
            raise ValueError("WECOM_BOT_ID and WECOM_BOT_SECRET are required")
        while True:
            heartbeat_task = None
            try:
                self._session = aiohttp.ClientSession()
                logger.info("wecom_connecting url=%s bot_id=%s", self.websocket_url, self.bot_id)
                self._ws = await self._session.ws_connect(self.websocket_url, heartbeat=30)
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
        await self._reply(req_id, chat_id, str(outcome))

    async def _dispatch_event(self, payload: Dict[str, object]) -> None:
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        event_type = self._find_event_value(
            body, "eventtype", "event_type", "eventType"
        )
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
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return candidate
            for candidate in value.values():
                found = WeComGateway._find_event_value(candidate, *keys)
                if found not in (None, ""):
                    return found
        elif isinstance(value, list):
            for candidate in value:
                found = WeComGateway._find_event_value(candidate, *keys)
                if found not in (None, ""):
                    return found
        return None

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
        arguments = request.tool_input
        operate_type = str(arguments.get("operateType", "未提供"))
        meanings = {
            "2": "2 商品新建上架",
            "3": "3 商品下架",
            "4": "4 商品更新",
        }
        operate_text = meanings.get(operate_type, operate_type)
        sku_code = WeComGateway._card_value("skuCode", arguments.get("skuCode"))
        return {
            "card_type": "button_interaction",
            "main_title": {
                "title": "商品信息接口调用",
                "desc": "请确认是否执行本次调用",
            },
            "horizontal_content_list": [
                {"keyname": "操作", "value": operate_text[:26]},
                {"keyname": "SKU", "value": sku_code},
            ],
            "button_list": [
                {"text": "确认", "style": 1, "key": "approval.confirm"},
                {"text": "拒绝", "style": 2, "key": "approval.reject"},
            ],
            "task_id": request.id,
        }

    @staticmethod
    def _card_value(key: str, value: object) -> str:
        if any(token in key.lower() for token in ("password", "secret", "token")):
            return "***"
        rendered = "未提供" if value is None else str(value)
        return rendered[:26]

    def _extract_text(self, body: Dict[str, object]) -> str:
        text = body.get("text") if isinstance(body.get("text"), dict) else {}
        return str(text.get("content") or "").strip()

    async def _extract_images(self, body: Dict[str, object]) -> List[str]:
        images: List[str] = []
        image = body.get("image") if isinstance(body.get("image"), dict) else None
        if image:
            path = await self._cache_image(image)
            if path:
                images.append(path)
        return images

    async def _cache_image(self, image: Dict[str, object]) -> Optional[str]:
        data = image.get("base64") or image.get("data")
        if isinstance(data, str) and data:
            return self._write_cached_image(base64.b64decode(data), ".png")

        url = str(image.get("url") or "").strip()
        if not url:
            return None
        try:
            raw, headers = await self._download_remote_bytes(url)
            aes_key = str(image.get("aeskey") or image.get("aes_key") or "").strip()
            if aes_key:
                raw = self._decrypt_file_bytes(raw, aes_key)
            content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip()
            ext = self._guess_image_extension(url, content_type, raw)
            return self._write_cached_image(raw, ext)
        except Exception:
            logger.exception("wecom_image_cache_failed url=%s", url)
            return None

    def _write_cached_image(self, data: bytes, extension: str) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = extension if extension.startswith(".") else f".{extension}"
        path = self.cache_dir / f"{uuid.uuid4().hex}{suffix}"
        path.write_bytes(data)
        logger.info("wecom_image_cached path=%s bytes=%s", path, len(data))
        return str(path)

    async def _download_remote_bytes(self, url: str, max_bytes: int = 20 * 1024 * 1024) -> Tuple[bytes, Dict[str, str]]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported media URL scheme: {parsed.scheme}")

        def download() -> Tuple[bytes, Dict[str, str]]:
            req = request.Request(
                url,
                headers={"User-Agent": "AgentX/0.1", "Accept": "*/*"},
                method="GET",
            )
            with request.urlopen(req, timeout=30) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                content_length = headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise ValueError(f"Remote image exceeds limit: {content_length} bytes")
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"Remote image exceeds limit: {max_bytes} bytes")
                return data, headers

        return await asyncio.to_thread(download)

    @staticmethod
    def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
        if not encrypted_data:
            raise ValueError("encrypted_data is empty")
        if not aes_key:
            raise ValueError("aes_key is required")

        key = WeComGateway._decode_aes_key(aes_key)
        if len(key) != 32:
            raise ValueError(f"Invalid WeCom AES key length: expected 32 bytes, got {len(key)}")

       
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(f"Invalid PKCS#7 padding value: {pad_len}")
        if any(byte != pad_len for byte in decrypted[-pad_len:]):
            raise ValueError("Invalid PKCS#7 padding: padding bytes mismatch")
        return decrypted[:-pad_len]

    @staticmethod
    def _decode_aes_key(aes_key: str) -> bytes:
        normalized = str(aes_key or "").strip()
        if not normalized:
            raise ValueError("aes_key is required")
        normalized += "=" * (-len(normalized) % 4)
        try:
            return base64.b64decode(normalized)
        except Exception:
            return base64.urlsafe_b64decode(normalized)

    @staticmethod
    def _guess_image_extension(url: str, content_type: str, data: bytes) -> str:
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if ext:
            return ext
        path_ext = Path(urlparse(url).path).suffix
        if path_ext:
            return path_ext
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

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

