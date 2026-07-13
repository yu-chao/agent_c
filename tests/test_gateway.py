import asyncio

from agent_runtime.gateway.dingtalk_gateway import DingTalkGateway
from agent_runtime.gateway.models import InboundMessage
from agent_runtime.gateway.runner import GatewayRunner
from agent_runtime.gateway.wecom import WeComGateway


class FakeRuntime:
    def __init__(self):
        self.inputs = []

    def run_turn(self, value):
        self.inputs.append(value)
        return f"answer: {value}"


def test_runner_routes_message_through_agent_loop():
    runtime = FakeRuntime()
    runner = GatewayRunner(runtime, [])
    response = asyncio.run(runner.process(InboundMessage("wecom", "m1", "chat1", "user1", "hello")))
    assert runtime.inputs == ["hello"]
    assert response.text == "answer: hello"
    assert response.reply_to == "m1"


def test_wecom_parser_normalizes_callback():
    message = WeComGateway.parse({"headers": {"req_id": "r1"}, "body": {"msgid": "m1", "chatid": "c1", "from": {"userid": "u1"}, "text": {"content": "hi"}}})
    assert message.session_id == "wecom:c1"
    assert message.text == "hi"
    assert message.metadata["request_id"] == "r1"


def test_dingtalk_parser_normalizes_stream_callback():
    message = DingTalkGateway.parse({"msgId": "m2", "conversationId": "c2", "senderStaffId": "u2", "text": {"content": "hello"}, "sessionWebhook": "https://example.invalid/reply"})
    assert message.session_id == "dingtalk:c2"
    assert message.text == "hello"
