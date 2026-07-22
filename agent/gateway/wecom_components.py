from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agent.gateway.models import InboundMessage


logger = logging.getLogger(__name__)


class WeComMessageMapper:
    @staticmethod
    def parse(payload: dict[str, Any]) -> InboundMessage:
        body = (
            payload.get('body')
            if isinstance(payload.get('body'), dict)
            else payload
        )
        sender_data = (
            body.get('from')
            if isinstance(body.get('from'), dict)
            else {}
        )
        sender = str(
            sender_data.get('userid')
            or body.get('from_user')
            or ''
        )
        text_data = (
            body.get('text')
            if isinstance(body.get('text'), dict)
            else {}
        )
        headers = (
            payload.get('headers')
            if isinstance(payload.get('headers'), dict)
            else {}
        )
        return InboundMessage(
            platform='wecom',
            message_id=str(body.get('msgid') or uuid.uuid4().hex),
            conversation_id=str(body.get('chatid') or sender),
            sender_id=sender,
            text=str(
                text_data.get('content')
                or body.get('content')
                or ''
            ).strip(),
            metadata={'request_id': headers.get('req_id')},
        )

    @classmethod
    def find_event_value(
        cls,
        value: object,
        *keys: str,
    ) -> object | None:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if candidate not in (None, ''):
                    return candidate
            for candidate in value.values():
                found = cls.find_event_value(candidate, *keys)
                if found not in (None, ''):
                    return found
        elif isinstance(value, list):
            for candidate in value:
                found = cls.find_event_value(candidate, *keys)
                if found not in (None, ''):
                    return found
        return None


class WeComApprovalPresenter:
    OPERATIONS = {
        '2': '2 商品新建上架',
        '3': '3 商品下架',
        '4': '4 商品更新',
    }

    @classmethod
    def build_card(cls, approval_request) -> dict[str, object]:
        arguments = approval_request.tool_input
        operate_type = str(arguments.get('operateType', '未提供'))
        operate_text = cls.OPERATIONS.get(operate_type, operate_type)
        sku_code = cls.card_value(
            'skuCode',
            arguments.get('skuCode'),
        )
        return {
            'card_type': 'button_interaction',
            'main_title': {
                'title': '商品信息接口调用',
                'desc': '请确认是否执行本次调用',
            },
            'horizontal_content_list': [
                {'keyname': '操作', 'value': operate_text[:26]},
                {'keyname': 'SKU', 'value': sku_code},
            ],
            'button_list': [
                {
                    'text': '确认',
                    'style': 1,
                    'key': 'approval.confirm',
                },
                {
                    'text': '拒绝',
                    'style': 2,
                    'key': 'approval.reject',
                },
            ],
            'task_id': approval_request.id,
        }

    @staticmethod
    def card_value(key: str, value: object) -> str:
        sensitive = ('password', 'secret', 'token')
        if any(token in key.lower() for token in sensitive):
            return '***'
        rendered = '未提供' if value is None else str(value)
        return rendered[:26]


class WeComMediaStore:
    def __init__(
        self,
        cache_dir: Path | None = None,
        max_bytes: int = 20 * 1024 * 1024,
    ):
        self.cache_dir = cache_dir or Path('.cache/wecom-images')
        self.max_bytes = max_bytes

    async def extract_images(
        self,
        body: dict[str, object],
    ) -> list[str]:
        image = (
            body.get('image')
            if isinstance(body.get('image'), dict)
            else None
        )
        if not image:
            return []
        path = await self.cache_image(image)
        return [path] if path else []

    async def cache_image(
        self,
        image: dict[str, object],
    ) -> str | None:
        data = image.get('base64') or image.get('data')
        if isinstance(data, str) and data:
            return self.write(base64.b64decode(data), '.png')

        url = str(image.get('url') or '').strip()
        if not url:
            return None
        try:
            raw, headers = await self.download(url)
            aes_key = str(
                image.get('aeskey')
                or image.get('aes_key')
                or ''
            ).strip()
            if aes_key:
                raw = self.decrypt(raw, aes_key)
            content_type = str(
                headers.get('content-type') or ''
            ).split(';', 1)[0].strip()
            extension = self.guess_extension(
                url,
                content_type,
                raw,
            )
            return self.write(raw, extension)
        except Exception:
            logger.exception('wecom_image_cache_failed url=%s', url)
            return None

    def write(self, data: bytes, extension: str) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = (
            extension
            if extension.startswith('.')
            else f'.{extension}'
        )
        path = self.cache_dir / f'{uuid.uuid4().hex}{suffix}'
        path.write_bytes(data)
        logger.info(
            'wecom_image_cached path=%s bytes=%s',
            path,
            len(data),
        )
        return str(path)

    async def download(
        self,
        url: str,
    ) -> tuple[bytes, dict[str, str]]:
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'}:
            raise ValueError(
                f'Unsupported media URL scheme: {parsed.scheme}'
            )

        def fetch() -> tuple[bytes, dict[str, str]]:
            http_request = request.Request(
                url,
                headers={
                    'User-Agent': 'AgentRuntime/0.1',
                    'Accept': '*/*',
                },
                method='GET',
            )
            with request.urlopen(http_request, timeout=30) as response:
                headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                }
                content_length = headers.get('content-length')
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > self.max_bytes
                ):
                    raise ValueError(
                        f'Remote image exceeds limit: '
                        f'{content_length} bytes'
                    )
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise ValueError(
                        f'Remote image exceeds limit: '
                        f'{self.max_bytes} bytes'
                    )
                return data, headers

        return await asyncio.to_thread(fetch)

    @classmethod
    def decrypt(cls, encrypted_data: bytes, aes_key: str) -> bytes:
        if not encrypted_data:
            raise ValueError('encrypted_data is empty')
        key = cls.decode_aes_key(aes_key)
        if len(key) != 32:
            raise ValueError(
                'Invalid WeCom AES key length: '
                f'expected 32 bytes, got {len(key)}'
            )
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        decrypted = (
            decryptor.update(encrypted_data)
            + decryptor.finalize()
        )
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(
                f'Invalid PKCS#7 padding value: {pad_len}'
            )
        if any(byte != pad_len for byte in decrypted[-pad_len:]):
            raise ValueError(
                'Invalid PKCS#7 padding: padding bytes mismatch'
            )
        return decrypted[:-pad_len]

    @staticmethod
    def decode_aes_key(aes_key: str) -> bytes:
        normalized = str(aes_key or '').strip()
        if not normalized:
            raise ValueError('aes_key is required')
        normalized += '=' * (-len(normalized) % 4)
        try:
            return base64.b64decode(normalized)
        except Exception:
            return base64.urlsafe_b64decode(normalized)

    @staticmethod
    def guess_extension(
        url: str,
        content_type: str,
        data: bytes,
    ) -> str:
        extension = (
            mimetypes.guess_extension(content_type)
            if content_type
            else None
        )
        if extension:
            return extension
        path_extension = Path(urlparse(url).path).suffix
        if path_extension:
            return path_extension
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return '.png'
        if data.startswith(b'\xff\xd8\xff'):
            return '.jpg'
        if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
            return '.gif'
        if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            return '.webp'
        return '.jpg'
