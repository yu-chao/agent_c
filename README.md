# 通用 Agent Runtime

这是一个可扩展的多供应商 Agent 运行时。旧版 `code.py` 中的教学型单文件 harness 已拆成包结构：模型适配、工具注册、hook、权限、存储、任务图、cron、MCP mock 和 CLI 入口各自独立。

## 运行

```sh
uv run --extra test python -m pytest -q
uv run python -m agent_runtime --provider openai --model gpt-5
uv run python -m agent_runtime --provider anthropic
```

也可以继续运行：

```sh
python code.py --provider openai
```

但推荐使用 `python -m agent_runtime`。

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

- `agent_runtime.models`: 统一模型接口和供应商适配器。
- `agent_runtime.models.openai`: OpenAI Responses API 适配器，使用 function tools 和 `function_call_output` 回传工具结果。
- `agent_runtime.models.anthropic`: Anthropic Messages API 适配器。
- `agent_runtime.tools`: 内置工具和 MCP 工具统一注册。
- `agent_runtime.core`: provider 无关的 agent loop。
- `agent_runtime.hooks`: `UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop` 扩展点。
- `agent_runtime.security`: 命令、路径和 MCP 破坏性工具权限策略。
- `agent_runtime.storage`: 文件存储接口和本地实现。
- `agent_runtime.tasks`: 可持久化 task graph。
- `agent_runtime.scheduler`: cron 表达式校验与匹配。
- `agent_runtime.mcp`: mock MCP hub，用于动态工具接入演示。

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
