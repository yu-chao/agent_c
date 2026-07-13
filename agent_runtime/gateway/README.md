# Multi-platform gateway

The gateway package separates messaging transports from the agent loop:

```text
WeCom / DingTalk / future adapters
              | parse
              v
       InboundMessage
              |
       GatewayRunner.process
              |
       AgentRuntime.run_turn
              |
       OutboundMessage
              | send
              v
        platform reply API
```

Adapters receive an `AsyncIterable` event source and an async reply sender. The
event source may be implemented with a vendor Stream SDK, an HTTP callback
server, or a message broker without coupling those dependencies to the runtime.

```python
runner = GatewayRunner(runtime, [
    WeComGateway(wecom_events, send_wecom_reply),
    DingTalkGateway(dingtalk_events, send_dingtalk_reply),
])
await runner.run_forever()
```

To add a platform, subclass `MessageGateway`, normalize callbacks into
`InboundMessage`, and translate `OutboundMessage` in `send`. No changes to the
agent loop or other platform adapters are required.
