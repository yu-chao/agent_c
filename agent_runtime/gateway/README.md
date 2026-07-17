# 多平台消息网关

`gateway` 只负责平台协议与内部消息之间的转换。业务处理位于
`agent_runtime.application`，运行时装配位于 `agent_runtime.bootstrap`。

```text
企业微信 / 钉钉 / 其他平台
           │
           ▼
  InboundMessage
           │
           ▼
  AssistantService
           │
           ▼
  AgentRuntime
           │
           ▼
  OutboundMessage / PendingApproval
           │
           ▼
      平台回复接口
```

企业微信只有一个公开的 `WeComGateway`。其内部职责拆分如下：

- `WeComMessageMapper`：解析消息和事件字段。
- `WeComApprovalPresenter`：生成审批卡片并隐藏敏感字段。
- `WeComMediaStore`：下载、解密、校验并缓存图片。
- `WeComGateway`：维护连接、订阅、心跳和消息调度。

新增平台时，应实现 `MessageGateway` 协议，并将平台消息转换为
`InboundMessage`。不得从网关直接创建模型、MCP 客户端或数据库。

## MCP 工具审批

命中 `approval.tools` 的工具不会立即执行。运行时持久化模型上下文和待执行
调用，网关发送确认卡片，然后结束当前回调。用户确认或拒绝后，应用服务更新
审批状态并恢复 Agent。处于 `EXECUTING` 的记录不会自动重放，避免重复执行
具有外部副作用的工具。
