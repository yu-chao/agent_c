import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from agent_runtime.gateway.wecom_gateway import WeComGateway


async def _handler(message):
    return message.text


class FakeWebSocket:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(self.payload))


class FakeSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.connect_kwargs = None

    async def ws_connect(self, url, **kwargs):
        self.connect_kwargs = kwargs
        return self.websocket

    async def close(self):
        return None


def test_subscribe_waits_for_successful_acknowledgement():
    gateway = WeComGateway(_handler, bot_id="bot", secret="secret")
    gateway._ws = FakeWebSocket({"body": {"errcode": 0}})

    asyncio.run(gateway._subscribe())

    assert gateway._ws.sent[0]["cmd"] == "aibot_subscribe"
    assert gateway._ws.sent[0]["body"] == {"bot_id": "bot", "secret": "secret"}


def test_subscribe_rejects_authentication_error():
    gateway = WeComGateway(_handler, bot_id="bot", secret="bad")
    gateway._ws = FakeWebSocket({"body": {"errcode": 40001, "errmsg": "invalid credential"}})

    with pytest.raises(ConnectionError, match="errcode=40001"):
        asyncio.run(gateway._subscribe())


def test_application_heartbeat_sends_ping(monkeypatch):
    gateway = WeComGateway(_handler, bot_id="bot", secret="secret")
    commands = []

    async def no_wait(_seconds):
        return None

    async def send_once(command, body, req_id=None):
        commands.append((command, body))
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    monkeypatch.setattr(gateway, "_send", send_once)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gateway._heartbeat_loop())

    assert commands == [("ping", {})]


def test_run_forever_does_not_enable_websocket_protocol_heartbeat(monkeypatch):
    websocket = FakeWebSocket({"body": {"errcode": 0}})
    session = FakeSession(websocket)
    gateway = WeComGateway(_handler, bot_id="bot", secret="secret")

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    async def stop_after_subscribe():
        raise asyncio.CancelledError

    monkeypatch.setattr(gateway, "_recover_approvals", stop_after_subscribe)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gateway.run_forever())

    assert session.connect_kwargs == {}


def test_disconnected_event_is_logged(caplog):
    gateway = WeComGateway(_handler, bot_id="bot", secret="secret")

    asyncio.run(
        gateway._dispatch(
            {
                "cmd": "aibot_event_callback",
                "body": {"event": {"eventtype": "disconnected_event"}},
            }
        )
    )

    assert "another instance" in caplog.text



def test_credentials_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("WECOM_BOT_ID", "env-bot")
    monkeypatch.setenv("WECOM_BOT_SECRET", "env-secret")

    gateway = WeComGateway(_handler)

    assert gateway.bot_id == "env-bot"
    assert gateway.secret == "env-secret"
