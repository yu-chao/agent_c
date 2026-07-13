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

## MCP 工具审批

生产企业微信入口使用运行时级别的可恢复审批。命中
`approval.tools` 的工具不会立即执行：运行时把模型上下文和待执行调用写入
SQLite，网关发送 `button_interaction` 确认卡片，然后结束当前回调。

用户点击确认或拒绝后，网关校验原始用户和会话，使用条件更新原子变更审批
状态，先通过 `aibot_respond_update_msg` 更新卡片，再在后台恢复 Agent。
最终答复通过 `aibot_send_msg` 主动发送，不依赖原消息的 `req_id`。

默认配置如下：

```yaml
approval:
  enabled: true
  timeout_seconds: 600
  store_path: .runtime/approvals.db
  tools:
    - mcp__PlantMartBusiness__queryProductInfoUsingPOST
```

CLI 没有交互式审批渠道，遇到需审批工具时会生成“未执行”的工具结果，
不会自动放行。处于 `EXECUTING` 的记录表示进程可能在远程调用期间退出，
启动恢复不会重放这类调用，需人工核查远端结果。
