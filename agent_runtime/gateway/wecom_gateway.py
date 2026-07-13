from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib import request
from urllib.parse import urlparse
import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from agent_runtime.logging_utils import safe_preview
from .service import InboundMessage



logger = logging.getLogger(__name__)

MessageHandler = Callable[[InboundMessage], Awaitable[str]]


class WeComGateway:
    """Minimal Enterprise WeChat AI Bot WebSocket client for this prototype."""

    def __init__(
        self,
        handler: MessageHandler,
        bot_id: Optional[str] = None,
        secret: Optional[str] = None,
        websocket_url: Optional[str] = None,
    ):
        self.bot_id = (bot_id or os.getenv("WECOM_BOT_ID", "")).strip()
        self.secret = (secret or os.getenv("WECOM_BOT_SECRET", "")).strip()
        self.websocket_url = (
            websocket_url or os.getenv("WECOM_WEBSOCKET_URL", "wss://openws.work.weixin.qq.com")
        ).strip()
        self.handler = handler
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

    async def _dispatch(self, payload: Dict[str, object]) -> None:
        command = str(payload.get("command") or payload.get("cmd") or "")
        if command not in {"aibot_msg_callback", "aibot_callback"}:
            return
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        msg_id = str(body.get("msgid") or uuid.uuid4().hex)
        req_id = ""
        headers = payload.get("headers")
        if isinstance(headers, dict):
            req_id = str(headers.get("req_id") or "")
        self._reply_req_ids[msg_id] = req_id
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "")
        chat_id = str(body.get("chatid") or sender_id)
        text = self._extract_text(body)
        image_paths = await self._extract_images(body)
        logger.info(
            "wecom_message_received chat_id=%s sender=%s msg_id=%s req_id=%s text=%s images=%s",
            chat_id,
            sender_id,
            msg_id,
            req_id,
            text,
            len(image_paths),
        )
        answer = await self.handler(InboundMessage(session_id=chat_id, text=text, image_paths=image_paths))
        await self._reply(req_id, chat_id, answer)

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

