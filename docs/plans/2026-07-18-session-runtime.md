# Agent Runtime 会话可靠性实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 为 Agent Runtime 增加短时消息记忆、SQLite 持久化会话、入站消息幂等、通用检查点和崩溃后的安全恢复能力。

**Architecture:** 新增独立的 `sessions` 子系统保存 Session、Run、Message、Checkpoint、InboundMessage 和 ToolExecution。`AgentRuntime` 在配置了会话仓库时负责运行生命周期与检查点，未配置时保持现有无状态兼容行为；副作用工具使用执行账本避免恢复时重复执行。

**Tech Stack:** Python 3.11、标准库 `sqlite3`、pytest、现有 provider 无关消息协议。

---

### Task 1: 定义会话模型与 SQLite 仓库

**Objective:** 建立可原子创建 Run、保存消息和读取短时上下文的持久化边界。

**Files:**
- Create: `agent_runtime/sessions/models.py`
- Create: `agent_runtime/sessions/store.py`
- Create: `agent_runtime/sessions/__init__.py`
- Test: `tests/test_sessions.py`

**Steps:**
1. 先编写会话创建、消息窗口和入站幂等测试。
2. 运行 `uv run --extra test python -m pytest tests/test_sessions.py -q`，确认因模块缺失而失败。
3. 实现最小 SQLite schema 和仓库方法。
4. 重跑定向测试，预期通过。

### Task 2: 定义检查点、恢复状态与工具执行账本

**Objective:** 持久化每个 Run 的恢复载荷，并保证结果未知的工具调用不会自动重放。

**Files:**
- Modify: `agent_runtime/sessions/models.py`
- Modify: `agent_runtime/sessions/store.py`
- Test: `tests/test_sessions.py`

**Steps:**
1. 先编写检查点递增、启动恢复和工具执行唯一性测试。
2. 确认测试按预期失败。
3. 实现检查点、Run 状态迁移及工具执行 claim/complete。
4. 重跑定向测试，预期通过。

### Task 3: 将会话生命周期接入 AgentRuntime

**Objective:** 在每轮对话中加载短时历史、保存消息与检查点，并提供 `resume_run`。

**Files:**
- Modify: `agent_runtime/core/loop.py`
- Modify: `agent_runtime/core/results.py`
- Test: `tests/test_session_runtime.py`

**Steps:**
1. 先编写跨实例记忆、重复消息返回缓存结果和断点恢复测试。
2. 确认新测试失败。
3. 最小化修改循环并保持无状态调用兼容。
4. 运行新测试和既有 runtime/approval 测试。

### Task 4: 接入配置、装配与应用服务

**Objective:** 默认启用 SQLite 会话仓库并在进程启动时隔离遗留运行。

**Files:**
- Modify: `agent_runtime/settings.py`
- Modify: `agent_runtime/bootstrap.py`
- Modify: `agent_runtime/application/assistant.py`
- Modify: `config/default.yaml`
- Test: `tests/test_architecture.py`

**Steps:**
1. 先增加配置解析和装配测试并确认失败。
2. 实现 `session`、`context.recent_message_limit` 配置。
3. 启动时将遗留 `running` Run 转为 `interrupted`，暴露恢复查询与恢复入口。
4. 运行配置和集成测试。

### Task 5: 文档与全量验证

**Objective:** 记录行为、安全边界和操作方法，并验证所有回归测试。

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

**Steps:**
1. 更新简体中文说明、配置示例和恢复 API。
2. 运行 `uv run --extra test python -m pytest -q`，预期全部通过。
3. 运行 `uv build`，预期 wheel 和源码包构建成功。

