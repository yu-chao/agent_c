import asyncio
from datetime import datetime, timezone

from agent.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RuntimeIdentity,
)
from agent.core import Completed, PendingApproval
from agent.gateway.wecom_gateway import WeComGateway


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def request():
    return ApprovalRequest.create(
        identity=RuntimeIdentity("wecom", "chat-1", "user-1", "message-1"),
        tool_call_id="call-1",
        tool_name="mcp__PlantMartBusiness__queryProductInfoUsingPOST",
        tool_input={"operateType": 3, "skuCode": "SKU-123"},
        continuation={},
        timeout_seconds=600,
        now=datetime.now(timezone.utc),
    )


def test_approval_card_uses_fixed_keys_and_business_meaning():
    item = request()
    card = WeComGateway.build_approval_card(item)
    assert card["card_type"] == "button_interaction"
    assert card["task_id"] == item.id
    assert card["button_list"] == [
        {"text": "确认", "style": 1, "key": "approval.confirm"},
        {"text": "拒绝", "style": 2, "key": "approval.reject"},
    ]
    assert card["horizontal_content_list"][0]["value"] == "3 商品下架"
    assert card["horizontal_content_list"][1]["value"] == "SKU-123"


def test_message_callback_sends_approval_card_without_tool_arguments_in_keys():
    item = request()

    async def handler(_message):
        return PendingApproval(item)

    gateway = WeComGateway(handler, bot_id="bot", secret="secret")
    gateway._ws = FakeWebSocket()
    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "request-1"},
        "body": {
            "msgid": "message-1",
            "chatid": "chat-1",
            "from": {"userid": "user-1"},
            "text": {"content": "query"},
        },
    }
    asyncio.run(gateway._dispatch(payload))
    sent = gateway._ws.sent[0]
    assert sent["cmd"] == "aibot_respond_msg"
    assert sent["headers"]["req_id"] == "request-1"
    assert sent["body"]["template_card"]["task_id"] == item.id


def test_template_card_event_updates_immediately_then_schedules_resume():
    item = request()
    decisions = []
    resumed = []

    async def handler(_message):
        return Completed("unused")

    async def decide(approval_id, action, identity, event_id):
        decisions.append((approval_id, action, identity, event_id))
        return ApprovalDecision(True, ApprovalStatus.APPROVED, "approved", item)

    async def resume(_approval_id):
        resumed.append(_approval_id)
        return Completed("finished")

    gateway = WeComGateway(
        handler,
        bot_id="bot",
        secret="secret",
        approval_decider=decide,
        approval_resumer=resume,
    )
    gateway._ws = FakeWebSocket()
    payload = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "event-request"},
        "body": {
            "msgid": "event-1",
            "chatid": "chat-1",
            "from": {"userid": "user-1"},
            "event": {
                "eventtype": "template_card_event",
                "event_key": "approval.confirm",
                "task_id": item.id,
            },
        },
    }
    async def dispatch_and_yield():
        await gateway._dispatch(payload)
        await asyncio.sleep(0)

    asyncio.run(dispatch_and_yield())
    update = gateway._ws.sent[0]
    assert update["cmd"] == "aibot_respond_update_msg"
    assert update["body"]["response_type"] == "update_template_card"
    assert update["body"]["template_card"]["task_id"] == item.id
    assert decisions[0][2].conversation_id == "chat-1"
    assert decisions[0][2].sender_id == "user-1"
    assert resumed == [item.id]


def test_nested_camel_case_card_event_reaches_approval_store():
    item = request()
    decisions = []

    async def handler(_message):
        return Completed("unused")

    async def decide(approval_id, action, identity, event_id):
        decisions.append((approval_id, action, identity, event_id))
        return ApprovalDecision(True, ApprovalStatus.REJECTED, "rejected", item)

    gateway = WeComGateway(
        handler, bot_id="bot", secret="secret", approval_decider=decide
    )
    gateway._ws = FakeWebSocket()
    payload = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "event-request"},
        "body": {
            "msgid": "event-nested",
            "chatid": "chat-1",
            "from": {"userid": "user-1"},
            "event": {
                "eventType": "template_card_event",
                "templateCardEvent": {
                    "eventKey": "approval.reject",
                    "taskId": item.id,
                },
            },
        },
    }
    asyncio.run(gateway._dispatch(payload))
    assert decisions[0][0:2] == (item.id, "approval.reject")
    assert gateway._ws.sent[0]["cmd"] == "aibot_respond_update_msg"


def test_unauthorized_click_only_updates_clicking_user():
    item = request()

    async def handler(_message):
        return Completed("unused")

    async def decide(*_args):
        return ApprovalDecision(
            False, ApprovalStatus.PENDING, "unauthorized", item
        )

    gateway = WeComGateway(
        handler, bot_id="bot", secret="secret", approval_decider=decide
    )
    gateway._ws = FakeWebSocket()
    payload = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "event-request"},
        "body": {
            "msgid": "event-2",
            "chatid": "chat-1",
            "from": {"userid": "other-user"},
            "event": {
                "eventtype": "template_card_event",
                "event_key": "approval.confirm",
                "task_id": item.id,
            },
        },
    }
    asyncio.run(gateway._dispatch(payload))
    assert gateway._ws.sent[0]["body"]["userids"] == ["other-user"]
