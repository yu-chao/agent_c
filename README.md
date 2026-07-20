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

### PostgreSQL 多实例部署

生产环境可让会话、Run、checkpoint、工具账本和审批共用 PostgreSQL：

```sh
uv sync --extra postgres
```

```yaml
storage:
  backend: postgres
  postgres_dsn: postgresql://agent:password@db.example.com/agent_runtime
  migrate_on_start: true
  queue_enabled: true
```

也可通过 `AGENT_STORAGE_BACKEND`、`AGENT_POSTGRES_DSN`、
`AGENT_STORAGE_MIGRATE_ON_START` 和 `AGENT_RUN_QUEUE_ENABLED` 覆盖。首次连接会按
`agent_runtime/migrations/postgres/` 中的显式版本迁移建表；生产发布也可以先由部署
流程调用 `agent_runtime.migrations.apply_postgres_migrations()`，再把
`migrate_on_start` 设为 `false`。

`PostgresRunQueue` 使用 `FOR UPDATE SKIP LOCKED` 领取任务，并且只传递 `run_id`。
worker 领取后仍须通过会话存储的 CAS 和租约确认 Run 可执行，不能把队列消息当作
真实状态。集成测试需设置专用测试库 `AGENT_TEST_POSTGRES_DSN`：

```sh
uv run --extra test --extra postgres python -m pytest tests/integration -q
```

### 长期记忆与检索

长期记忆默认关闭。启用后，SQLite 使用独立的 `.runtime/memory.db`，PostgreSQL
后端使用共享数据库中的 `memories` 表：

```yaml
memory:
  enabled: true
  store_path: .runtime/memory.db
  default_ttl_days: 365
  max_results: 5
```

运行时不会从模型回答或会话摘要自动抽取记忆。应用必须通过
`MemoryService.remember_user_statement()` 或 `AssistantService.remember()` 显式写入
用户陈述；每条记录包含来源消息、主体、置信度、过期时间和可见范围。检索会先执行
主体、会话/租户、过期和删除过滤，再把匹配记忆及其来源放在会话摘要之后。

用户可通过 `AssistantService.memories()` 查询系统记住的内容，使用
`correct_memory()` 创建带新来源的纠正版本，使用 `forget_memory()` 物理删除整条
纠正链。`private`、`conversation`、`tenant` 三种可见范围分别表示仅主体、当前会话
和当前租户可见；扩大范围必须由调用方显式指定。

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

## 配置契约

运行时按“代码默认值 → YAML → 环境变量”的优先级加载配置。配置文件中的未知
字段、错误的段类型以及越界数值会在启动时直接报错，避免运行到模型或工具调用
阶段才暴露问题。模型可靠性配置示例：

```yaml
model:
  provider: anthropic
  name: null
reliability:
  request_timeout_seconds: 60
  max_attempts: 3
  fallback_provider: openai
  fallback_model: gpt-5
```

对应环境变量为 `AGENT_MODEL_PROVIDER`、`AGENT_MODEL_ID`、
`AGENT_MODEL_REQUEST_TIMEOUT_SECONDS`、`AGENT_MODEL_MAX_ATTEMPTS`、
`AGENT_MODEL_FALLBACK_PROVIDER` 和 `AGENT_MODEL_FALLBACK_ID`。环境变量只在
`load_settings()` 中解析，`bootstrap` 和模型工厂仅消费强类型配置。fallback 的
实际重试和切换行为将在“模型可靠性、重试与 fallback”阶段接入。

## Token 预算与会话摘要

运行时在每次模型调用前计算系统提示、工具 schema 和消息的近似 token 总量，
并保证内部 `ModelRequest` 不超过 `context.max_input_tokens`。超过预算时优先移除
最旧消息；当前用户消息和最近一组工具调用/结果会被保留，过大的工具结果会带
明确标记截断。

```yaml
context:
  recent_message_limit: 20
  max_input_tokens: 32000
  summary_trigger_tokens: 24000
  tool_result_max_tokens: 4000
```

当摘要触发阈值被达到时，运行时把旧消息写入版本化的
`session_summaries`，并保存摘要覆盖到的 message ID。checkpoint 同时保存
`summary_version`；恢复 Run 时继续使用 checkpoint 中的消息快照，不会静默混入
之后生成的新摘要。当前默认使用可替换的确定性提取式摘要器，provider 精确 token
计数和模型摘要器可通过相同接口后续接入。

## Skill 加载与版本快照

Skill 默认关闭。启用后，运行时从配置目录的直接子目录发现 `skill.yaml`，校验
SemVer 版本、入口文件边界、所需工具和文件系统权限，并按 activation keywords
与描述选择最多 `max_active` 个 Skill。只有选中的 `SKILL.md` 内容会进入模型上下文。

```yaml
skills:
  enabled: true
  paths:
    - skills
  max_active: 3
  allowed_filesystem: read
```

每个 checkpoint 都保存选中 Skill 的名称、版本和内容摘要。恢复 Run 时会重新发现
Skill 并严格比对快照；Skill 被删除、升级或同版本内容发生变化时恢复会明确失败，
不会静默切换。Skill 只能声明现有工具，实际工具调用仍统一经过 `ToolExecutor` 和
`PermissionPolicy`。可用环境变量为 `AGENT_SKILLS_ENABLED`、
`AGENT_SKILLS_MAX_ACTIVE` 和 `AGENT_SKILLS_ALLOWED_FILESYSTEM`。

## 管理、审计与数据保留

管理面通过 `AdminService` 暴露租户隔离的 Session、Run、Checkpoint、工具执行和
Approval 查询。调用方必须从可信认证层构造 `AdminActor`；读取、运行控制、未知工具
处置、审计导出和数据清理使用独立 scope。暂停、取消、失败恢复和未知工具人工处置
都要求非空原因与 `operation_id`，重复请求返回首次结果，相同 ID 绑定不同请求会被
拒绝。审计记录默认不包含消息正文、工具参数和工具输出。

`RetentionService` 只接受显式租户和带时区的截止时间。清理使用终态白名单
`completed / failed / cancelled`：只要 Session 存在 `running`、
`waiting_approval`、`interrupted` 或未来未知状态的 Run，其消息、摘要和附件都会
被保护；非终态 Run 的 checkpoint 同样不会删除。数据保留不会自动启用，生产系统
应由受控调度器使用 `admin.retention.execute` scope 调用，并保存业务原因。

SQLite 可通过 `build_admin_service()` 和 `build_retention_service()` 复用 Session
数据库；PostgreSQL 使用相同领域接口和显式 migration。管理 API 是与 Web 框架无关
的薄门面，认证信息不得从请求体构造。

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
