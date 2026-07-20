# Agent Runtime 剩余架构优化实施路线图

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 在现有持久化会话、短时消息窗口、入站幂等、通用检查点和租约恢复基础上，补齐 Skill、上下文治理、可靠性、任务编排、可观测性和生产级部署能力。

**Architecture:** 保持“入口与装配层 → 应用层 → 核心端口 → 基础设施适配器”的单向依赖。新增能力优先抽象为 `core` 端口或独立领域子系统，再由 `bootstrap` 选择实现；不得把 provider、数据库或网关协议泄漏进核心运行循环。

**Tech Stack:** Python 3.11、pytest、SQLite/PostgreSQL、OpenTelemetry（待选）、现有 OpenAI/Anthropic/MCP 适配器。

---

## 0. 已完成基线（不再重复实现）

以下能力已经落地，后续任务应复用而不是另建平行实现：

- SQLite `Session / Run / Message / Checkpoint / ToolExecution` 持久化。
- 短时消息滑动窗口。
- `platform + message_id` 入站消息幂等。
- `InProgress`、Run CAS、owner lease、续租与过期回收。
- 模型调用前、工具调用前、审批等待和最终响应前的通用 checkpoint。
- 工具副作用账本、参数一致性校验及结果未知时禁止自动重放。
- 审批与 `(run_id, call_id)` 去重关联及旧数据回填。
- CLI、Gateway、AssistantService 会话身份接入。

基线测试命令：

```sh
uv run --extra test python -m pytest -q
```

---

## 1. 优先级总览

| 优先级 | 能力 | 前置依赖 | 完成标志 |
|---|---|---|---|
| P0 | 配置契约治理 | 当前基线 | YAML 中每个公开字段都被解析、校验和消费 |
| P0 | Token 预算与会话摘要 | 持久化消息 | 长会话不会超过模型输入限制 |
| P0 | 模型可靠性与 fallback | 配置治理 | 超时、重试、限流和 provider 降级可测试 |
| P0 | 通用取消、暂停与续租 | Run 状态机 | 用户可安全取消或暂停长任务 |
| P1 | Skill 加载与版本快照 | 上下文治理 | Skill 可发现、校验、按需激活和恢复 |
| P1 | 工具执行治理 | 取消机制 | Schema、超时、异步、重试、副作用等级统一 |
| P1 | 可观测性 | 稳定 Run 生命周期 | 日志、指标、Trace 可通过 run_id 串联 |
| P1 | TaskGraph 与 Scheduler 集成 | checkpoint | 长任务和定时任务进入统一 Run 生命周期 |
| P2 | 生产级存储与分布式执行 | 租约和可观测性 | PostgreSQL/队列支持多实例并发 |
| P2 | 长期记忆与检索 | 摘要与隐私策略 | 跨 Session 记忆可写、可查、可纠正、可删除 |
| P2 | 管理面与数据治理 | 生产存储 | 可查询、恢复、取消、清理和审计运行数据 |
| P3 | 多 Agent 编排 | TaskGraph、Skill | 子 Agent 任务可隔离、恢复和审计 |

---

## 2. P0：配置契约治理

> 实现状态（2026-07-18）：已完成配置解析、环境变量覆盖、递归未知字段校验、
> 数值范围校验和强类型装配。fallback 的执行语义归入第 4 节，仍待实现。

### 现状缺口

`config/default.yaml` 中的 `fallback_provider`、`fallback_model` 尚未进入 `Settings` 和运行时。配置缺少统一校验，非法值通常要到运行阶段才暴露。

### 目标文件

- Modify: `agent_runtime/settings.py`
- Modify: `agent_runtime/bootstrap.py`
- Modify: `config/default.yaml`
- Modify: `.env.example`
- Test: `tests/test_architecture.py`

### 实施步骤

1. 先增加失败测试，覆盖 fallback 字段解析、环境变量覆盖、非法消息窗口、非法租约时间和未知配置字段。
2. 新增 `FallbackSettings`、`ReliabilitySettings`，为数值配置增加范围校验。
3. 让 `bootstrap` 只读取强类型 Settings，不直接读取环境变量。
4. 删除或标记所有没有消费者的公开配置。
5. 更新中文配置文档。

建议接口：

```python
@dataclass(frozen=True)
class ReliabilitySettings:
    request_timeout_seconds: float = 60
    max_attempts: int = 3
    fallback_provider: str | None = None
    fallback_model: str | None = None
```

验收：配置文件、环境变量和代码默认值三者一致；所有非法配置在启动时失败。

---

## 3. P0：Token 预算与会话摘要

> 实现状态（2026-07-19）：已完成近似 token 计数、请求预算裁剪、工具结果压缩、
> 版本化会话摘要及 checkpoint 摘要版本快照。provider 精确计数保留为适配器扩展。

### 现状缺口

短时记忆目前按消息条数截断，没有 token 预算、工具结果压缩和会话摘要。长消息或大型工具输出仍可能超过 provider 上下文限制。

### 目标文件

- Create: `agent_runtime/context/models.py`
- Create: `agent_runtime/context/manager.py`
- Create: `agent_runtime/context/token_counter.py`
- Create: `agent_runtime/context/__init__.py`
- Modify: `agent_runtime/sessions/store.py`
- Modify: `agent_runtime/core/loop.py`
- Modify: `agent_runtime/settings.py`
- Test: `tests/test_context.py`
- Test: `tests/test_session_runtime.py`

### 核心接口

```python
class ContextManager(Protocol):
    def build(self, session_id: str, current_run_id: str) -> list[dict]: ...
    def compact(self, session_id: str) -> str | None: ...
```

### 实施步骤

1. 用失败测试定义：保留系统提示、保留当前用户消息、按 token 裁剪、工具输出截断、摘要覆盖范围。
2. 在 Session 存储中增加 `session_summaries` 表，记录摘要版本和 `through_message_id`。
3. 先实现可替换的近似 token counter，provider 精确计数作为后续适配器能力。
4. 达到阈值时压缩旧消息，保留最近完整消息和未完成事项。
5. checkpoint 保存摘要版本，恢复时不得混用较新的不兼容摘要。

验收：构造超长会话时，最终请求始终低于 `max_input_tokens`，且当前用户消息和最近工具结果不会被错误删除。

---

## 4. P0：模型可靠性、重试与 fallback

> 实现状态（2026-07-19）：已完成统一错误分类、请求超时、指数退避与 jitter、`Retry-After` 限流等待、熔断、fallback 装配、跨 provider 请求重建，以及实际 provider/model checkpoint 记录。

### 现状缺口

模型调用没有统一超时、错误分类、退避重试、限流处理、熔断和 fallback；YAML 中 fallback 配置未生效。

### 目标文件

- Create: `agent_runtime/models/resilient.py`
- Create: `agent_runtime/models/errors.py`
- Modify: `agent_runtime/models/factory.py`
- Modify: `agent_runtime/core/ports.py`
- Modify: `agent_runtime/bootstrap.py`
- Test: `tests/test_model_reliability.py`
- Test: `tests/test_models.py`

### 核心接口

```python
class ResilientModelProvider:
    def __init__(self, primary, fallback=None, retry_policy=None): ...
    def generate(self, request: ModelRequest) -> ModelResponse: ...
```

### 实施步骤

1. 先测试超时、429、5xx、认证失败、非法请求和 fallback 条件。
2. 定义 `RetryableModelError` 与 `PermanentModelError`，差异仍限制在 provider adapter 内。
3. 仅重试明确可重试错误，使用指数退避和 jitter。
4. fallback 前创建 checkpoint，并记录实际 provider/model。
5. 防止在已有 provider-specific `previous_response_id` 时错误切换 provider；切换时必须使用完整内部消息重建请求。

验收：可重试故障按策略重试，永久错误立即失败，fallback 行为可通过 fake provider 完整验证。

---

## 5. P0：暂停、取消与租约心跳

### 现状缺口

已有租约和手动 `renew_run()`，但没有后台心跳、取消令牌、`PAUSED/CANCELLING` 状态，也不能把用户取消传播到模型和工具。

### 目标文件

- Create: `agent_runtime/core/cancellation.py`
- Modify: `agent_runtime/sessions/models.py`
- Modify: `agent_runtime/sessions/store.py`
- Modify: `agent_runtime/core/loop.py`
- Modify: `agent_runtime/application/assistant.py`
- Modify: `agent_runtime/gateway/models.py`
- Test: `tests/test_cancellation.py`
- Test: `tests/test_session_runtime.py`

### 实施步骤

1. 先测试运行前取消、模型调用间取消、工具前取消、等待审批时取消和暂停后恢复。
2. 增加合法状态迁移：`RUNNING → PAUSING → PAUSED → RUNNING`、`RUNNING → CANCELLING → CANCELLED`。
3. 每个模型/工具边界检查取消令牌并保存 checkpoint。
4. 为长任务建立租约心跳上下文；正常结束必须停止心跳。
5. 副作用工具已经进入 `running` 时，取消结果应标记为未知，不声称已撤销外部操作。

验收：取消不会产生新的工具副作用，暂停后从最近安全 checkpoint 恢复，多实例不会抢占有效租约。

---

## 6. P1：Skill 加载、选择与版本快照

> 实现状态（2026-07-19）：已完成目录发现、严格 Manifest 校验、关键词/描述选择、
> 激活数量限制、权限上限校验、system prompt 按需注入、checkpoint/审批 continuation
> 版本快照，以及恢复前的名称、版本和内容摘要兼容性校验。

### 现状缺口

当前只有静态 ToolRegistry 和 MCP 工具，没有 Skill 发现、Manifest 校验、按需激活、权限声明、版本快照或热重载。

### 目标文件

- Create: `agent_runtime/skills/models.py`
- Create: `agent_runtime/skills/loader.py`
- Create: `agent_runtime/skills/selector.py`
- Create: `agent_runtime/skills/__init__.py`
- Modify: `agent_runtime/core/loop.py`
- Modify: `agent_runtime/bootstrap.py`
- Modify: `agent_runtime/settings.py`
- Create: `tests/test_skills.py`

### Manifest 最小格式

```yaml
name: code-review
version: 1.0.0
description: 审查代码变更
entrypoint: SKILL.md
required_tools:
  - read_file
permissions:
  filesystem: read
activation:
  keywords:
    - 代码审查
```

### 实施步骤

1. 测试目录发现、重复名称、非法版本、越界 entrypoint、缺失工具和权限扩大。
2. Loader 只负责发现与校验，不负责执行脚本。
3. Selector 根据描述选择候选 Skill，默认最多激活少量 Skill，避免全部注入上下文。
4. Run 保存 Skill 名称、版本和内容摘要；恢复时验证快照兼容性。
5. Skill 脚本仍通过 ToolExecutor 和 PermissionPolicy，不得自行扩大权限。

验收：同一 Run 恢复时不会静默切换 Skill 版本；恶意路径和未声明权限被拒绝。

---

## 7. P1：工具执行治理

### 现状缺口

工具 handler 主要是同步字符串接口，缺少统一输入校验、异步执行、超时、输出上限、重试声明、副作用等级和取消传播。

### 目标文件

- Modify: `agent_runtime/tools/registry.py`
- Modify: `agent_runtime/core/tool_execution.py`
- Modify: `agent_runtime/core/ports.py`
- Modify: `agent_runtime/contracts.py`
- Test: `tests/test_tool_execution.py`

### 建议模型

```python
@dataclass(frozen=True)
class ToolPolicy:
    timeout_seconds: float = 30
    side_effect: str = "none"
    retryable: bool = False
    max_output_chars: int = 12000
```

### 实施步骤

1. 用测试定义 JSON Schema 拒绝、超时、取消、输出截断和未知结果。
2. Registry 同时支持 sync/async handler，由执行器统一 await。
3. 只允许 `side_effect=none` 且明确声明 retryable 的工具自动重试。
4. 将异常保存为结构化 ToolResult，避免字符串协议漂移。
5. MCP 工具也映射到同一 ToolPolicy。

验收：成功、拒绝、超时、取消、异常、结果未知路径均有独立测试。

---

## 8. P1：日志、指标与 Trace

> 实现状态（2026-07-20）：已完成结构化脱敏日志、关联上下文传播、进程内指标注册表、
> 可注入 Trace exporter，以及网关、Run、模型、工具、审批和恢复链路埋点。

### 现状缺口

现有日志尚不能完整串联一次消息的网关、Run、模型、工具、审批和恢复过程，也没有 token、费用和延迟指标。

### 目标文件

- Create: `agent_runtime/observability/context.py`
- Create: `agent_runtime/observability/metrics.py`
- Create: `agent_runtime/observability/tracing.py`
- Modify: `agent_runtime/logging_utils.py`
- Modify: `agent_runtime/core/loop.py`
- Modify: `agent_runtime/gateway/*`
- Test: `tests/test_observability.py`

### 必须关联的字段

- `trace_id`
- `session_id`
- `run_id`
- `message_id`
- `model_request_id`
- `tool_execution_id`
- `approval_id`
- provider、model、耗时、token、重试次数和恢复次数

验收：任一最终响应都能用 `run_id` 查询完整事件链；敏感字段继续脱敏；可观测性故障不得中断业务 Run。

---

## 9. P1：TaskGraph、Scheduler 与 Run 集成

> 实现状态（2026-07-20）：已完成稳定 UUID、SQLite CAS 任务领取与状态迁移、依赖门控、Task/触发器/Run 关联、
> cron 时间片唯一触发、延迟任务恢复，以及失败、暂停、取消和审批等待状态对账。任务恢复复用原 Run 的
> checkpoint 与工具执行账本，不会创建隐式 Run。

### 现状缺口

`tasks/graph.py` 和 `scheduler/cron.py` 已存在，但没有进入 Session/Run/checkpoint 生命周期，也没有幂等触发和失败恢复。

### 目标文件

- Modify: `agent_runtime/tasks/graph.py`
- Create: `agent_runtime/tasks/service.py`
- Modify: `agent_runtime/scheduler/cron.py`
- Create: `agent_runtime/scheduler/service.py`
- Modify: `agent_runtime/application/assistant.py`
- Test: `tests/test_tasks.py`
- Test: `tests/test_scheduler.py`

### 实施步骤

1. Task 使用稳定 UUID，状态迁移采用 CAS。
2. 每次任务执行创建 Run，保存 `task_id` 和调度触发 ID。
3. Cron 触发键建立唯一约束，防止重启后重复触发同一时间片。
4. 任务依赖完成后再领取；失败、暂停和取消传播到 Run。
5. 恢复时复用工具账本和 checkpoint，不另建隐式任务状态。

验收：重复调度不会重复执行，同一 Task 只能被一个 owner 领取，依赖和失败恢复测试通过。

---

## 10. P2：生产级存储与分布式执行

> 实现状态（2026-07-20）：已完成 PostgreSQL 会话/Run/checkpoint/工具账本与审批
> 存储、显式版本迁移、基于行锁的多实例领取、只投递 Run ID 的可选 PostgreSQL
> 队列，以及 SQLite/PostgreSQL 参数化存储契约测试。

### 目标

将当前 SQLite 实现保留为本地默认，同时提供 PostgreSQL 会话/审批存储和可选任务队列。

### 目标文件

- Create: `agent_runtime/sessions/ports.py`
- Create: `agent_runtime/sessions/postgres_store.py`
- Create: `agent_runtime/approval/postgres_store.py`
- Create: `agent_runtime/migrations/`
- Modify: `agent_runtime/bootstrap.py`
- Test: `tests/integration/test_postgres_storage.py`

### 关键要求

- 所有 CAS、租约、审批去重和工具账本语义必须与 SQLite 一致。
- Schema 使用显式版本迁移，不允许仅靠启动时 `ALTER TABLE` 长期演进。
- 多实例领取使用数据库行锁或等价原子语义。
- 队列只负责投递 Run ID，真实状态仍以数据库为准。

验收：同一套存储契约测试可参数化运行在 SQLite 和 PostgreSQL 上。

---

## 11. P2：长期记忆与检索

### 原则

长期记忆后置于会话摘要，并必须先解决隐私、来源、过期、纠正和删除，而不是简单接入向量数据库。

### 目标文件

- Create: `agent_runtime/memory/models.py`
- Create: `agent_runtime/memory/service.py`
- Create: `agent_runtime/memory/store.py`
- Create: `agent_runtime/memory/retrieval.py`
- Test: `tests/test_memory.py`

### 数据模型

每条记忆至少包含：主体、内容、来源消息、置信度、创建时间、过期时间、可见范围和删除状态。

验收：用户可以查询“系统记住了什么”、纠正错误记忆和彻底删除；检索结果携带来源，不把模型推测当成事实保存。

---

## 12. P2：管理面、审计与数据保留

### 目标能力

- 查询 Session、Run、Checkpoint、ToolExecution 和 Approval。
- 恢复、暂停、取消失败 Run。
- 查看结果未知的副作用工具并人工处置。
- 按租户和时间清理消息、附件和 checkpoint。
- 导出审计记录，记录操作者和原因。

### 建议文件

- Create: `agent_runtime/admin/service.py`
- Create: `agent_runtime/admin/api.py`
- Create: `agent_runtime/retention/service.py`
- Test: `tests/test_admin.py`
- Test: `tests/test_retention.py`

验收：管理操作全部鉴权、审计且幂等；清理不会删除仍被活跃 Run 引用的数据。

---

## 13. P3：多 Agent 编排（最后实施）

多 Agent 不是下一阶段前置条件。只有 TaskGraph、Skill、checkpoint、租约、取消和可观测性稳定后再实现。

### 最小范围

- 主 Agent 将有界任务派发给子 Agent。
- 子 Agent 拥有独立 Run、上下文预算、Skill 快照和权限范围。
- 结果通过结构化任务结果返回，不共享可变消息列表。
- 子 Agent 崩溃可独立恢复，不导致主 Run 重放已完成副作用。

### 建议文件

- Create: `agent_runtime/orchestration/coordinator.py`
- Create: `agent_runtime/orchestration/models.py`
- Test: `tests/test_orchestration.py`

---

## 14. 推荐实施顺序

严格按以下顺序推进：

1. 配置契约治理。
2. Token 预算和会话摘要。
3. 模型可靠性与 fallback。
4. 暂停、取消和租约心跳。
5. Skill 加载与版本快照。
6. 工具执行治理。
7. 可观测性。
8. TaskGraph 与 Scheduler 集成。
9. PostgreSQL 与分布式执行。
10. 长期记忆、管理面。
11. 多 Agent 编排。

每个一级条目实施前，应从本文拆出独立的 `docs/plans/YYYY-MM-DD-<feature>.md`，按 TDD 编写 2–5 分钟粒度任务，并在提交前执行独立代码审查。

## 15. 暂不实施的内容

在 P0/P1 完成前，不应优先建设：

- Skill 商店或在线安装市场。
- 无数据治理的向量长期记忆。
- 自主修改系统 Prompt、权限规则或自身 Skill。
- 大规模多 Agent 自动协作。
- 与 Run 状态无关的独立任务队列。

这些功能会放大现有可靠性和安全边界，提前实现会增加返工成本。
