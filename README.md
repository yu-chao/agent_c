# 通用 Agent Runtime

## 持久化会话与安全恢复

运行时默认使用 `.runtime/sessions.db` 保存会话状态。企业网关以
`platform + conversation_id` 标识会话，以 `platform + message_id` 保证入站消息
幂等；CLI 可使用 `--session` 选择持久化会话：

```sh
uv run python -m agent_runtime --provider openai --session project-a
```

每个用户请求对应一个独立 Run。运行时只把最近
`context.recent_message_limit` 条用户和助手消息装入模型上下文，工具调用的完整
中间状态保存在当前 Run 的检查点中。模型调用前、工具执行前和最终响应落库前
都会保存检查点。

进程启动时，只有当前 owner 遗留或租约已经过期的 `running` Run 会转换为
`interrupted`，其他实例仍持有有效租约的 Run 不会被误回收。长任务可通过
运行时会在模型和工具调用期间自动发送 heartbeat（心跳）续租；`execution_token`
会阻止租约过期后的旧 worker 继续保存 checkpoint 或完成 Run。应用层可以通过
`AssistantService.recoverable_runs()` 查询，再调用 `resume_run(run_id)` 恢复。
已经完成的工具结果会直接复用；崩溃时仍处于 `running` 的工具被视为结果未知，
运行时会禁止自动重放，并把该状态反馈给模型或交由人工处理。

配置示例：

```yaml
session:
  enabled: true
  store_path: .runtime/sessions.db
  lease_seconds: 30
context:
  recent_message_limit: 20
```

这是一个可扩展的多供应商 Agent 运行时。项目采用“入口与装配层 → 应用层 → 核心协议与能力端口 ← 基础设施适配器”的单向依赖结构。

## 运行

```sh
uv run --extra test python -m pytest -q
uv run python -m agent_runtime --provider openai --model gpt-5
uv run python -m agent_runtime --provider anthropic
```

企业微信长连接入口：

```sh
uv run agent-runtime-wecom
```

## 架构

```text
user input
  -> AgentRuntime.run_turn
  -> HookManager.UserPromptSubmit
  -> ModelProvider.generate
      -> AnthropicProvider | OpenAIProvider
  -> ToolCall?
      no  -> HookManager.Stop -> final text
      yes -> HookManager.PreToolUse -> PermissionPolicy
          -> ToolRegistry handler
          -> HookManager.PostToolUse
          -> ToolResult
          -> next model call
```

核心模块：

- `agent_runtime.contracts`: 与供应商无关的消息、模型和工具协议。
- `agent_runtime.models`: OpenAI、Anthropic 等模型供应商适配器。
- `agent_runtime.models.openai`: OpenAI Responses API 适配器，使用 function tools 和 `function_call_output` 回传工具结果。
- `agent_runtime.models.anthropic`: Anthropic Messages API 适配器。
- `agent_runtime.tools`: 内置工具和 MCP 工具统一注册。
- `agent_runtime.core`: provider 无关的 agent loop、能力端口和工具执行。
- `agent_runtime.application`: 渠道无关的应用服务与审批用例。
- `agent_runtime.bootstrap`: 统一依赖装配。
- `agent_runtime.settings`: YAML 与环境变量配置入口。
- `agent_runtime.hooks`: `UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop` 扩展点。
- `agent_runtime.security`: 命令、路径和 MCP 破坏性工具权限策略。
- `agent_runtime.storage`: 文件存储接口和本地实现。
- `agent_runtime.tasks`: 可持久化 task graph。
- `agent_runtime.scheduler`: cron 表达式校验与匹配。
- `agent_runtime.mcp`: MCP Streamable HTTP 客户端和动态工具接入。

## 多供应商模型层

运行时内部只认识三类 block：

- `TextBlock`
- `ToolCall`
- `ToolResult`

供应商协议差异只存在 adapter 内部：

- Anthropic adapter 把内部工具 schema 转为 `input_schema`，解析 `tool_use`。
- OpenAI adapter 把内部工具 schema 转为 Responses API function tool，解析 `function_call`，并把 `ToolResult` 转为 `function_call_output`。

配置示例见 [config/default.yaml](config/default.yaml)。

## 扩展一个工具

```python
from agent_runtime.tools import ToolRegistry, ToolSpec

registry = ToolRegistry()
registry.register(
    ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    lambda text: text,
)
```

同一个 `ToolRegistry` 可被 Anthropic 和 OpenAI provider 共用。

## 企业运行时边界

首版实现的是可扩展骨架，不绑定数据库或 Web 服务：

- 存储先使用 `FileStore`，后续可替换 SQLite/Postgres 实现。
- 权限策略集中在 `PermissionPolicy`，避免散落在工具 handler 中。
- 模型供应商通过 `create_model_provider` 创建，后续可增加本地模型或其他云供应商。
- MCP 当前为 mock hub，工具命名遵循 `mcp__{server}__{tool}`。

## 测试

```sh
uv run --extra test python -m pytest -q
```

测试覆盖：

- OpenAI tool schema 转换。
- OpenAI function call 和 function output 适配。
- provider factory。
- tool registry 和 MCP 工具合并。
- permission policy。
- file storage 路径边界。
- task graph 依赖。
- cron 校验与匹配。
- provider 无关 runtime loop。
