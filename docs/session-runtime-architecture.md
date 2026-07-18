# 会话运行时架构

会话可靠性能力按职责拆分为以下组件：

- `AgentRuntime`：对外门面，维护 provider 无关的模型循环。
- `RunCoordinator`：负责 Run 创建、认领、checkpoint、heartbeat 和完成。
- `ToolExecutionCoordinator`：负责工具执行账本及结果复用。
- `CheckpointCodec`：负责带版本号的 checkpoint 编解码。
- `SQLiteSessionStore`：实现 Run、消息、checkpoint 和工具账本持久化端口。

`core` 只依赖由 `core.ports` 定义的端口。SQLite 实现可以同时实现多个小端口，
但数据库细节和迁移逻辑不会进入模型循环。

## Run 所有权

每次创建或重新认领 Run 都会获得递增的 `execution_token`（执行令牌）。保存
checkpoint、记录工具结果和完成 Run 时必须同时匹配 `owner_id` 与
`execution_token`。即使旧 worker 在租约过期后恢复执行，其写操作也会被拒绝。

模型和工具调用期间，`RunCoordinator` 按 `session.lease_seconds / 3` 的间隔自动
发送 heartbeat。`session.lease_seconds` 必须明显大于一次正常的数据库延迟。

## checkpoint 与恢复

checkpoint 包含 `schema_version`，模型消息的线格式只由 `CheckpointCodec` 管理。
当前实现兼容引入版本号之前的 version 0 数据；遇到未知的新版本时会明确失败，
避免用错误结构恢复执行。

工具执行后和审批结果消费后都会立即推进 checkpoint。恢复时以 checkpoint 游标
和工具执行账本为依据，不以审批表的 `COMPLETED` 状态推断整个 continuation 已经
完成。因此，即使进程在审批工具成功后、执行剩余步骤前崩溃，也可以复用已保存的
工具结果继续运行。

如果工具账本处于 `running` 且没有结果，说明副作用是否发生无法确定。运行时不会
自动重放该工具，而是把 Run 标记为 `failed`，等待人工对账。

## 数据库迁移

SQLite 存储使用 `PRAGMA user_version` 记录 schema 版本。审批关联历史数据只在对应
迁移首次执行时回填，不会在每次进程启动时扫描全表。新增 schema 变更应继续以单次、
可重复验证的迁移实现。

## 扩展存储实现

接入 PostgreSQL 等存储时，应分别实现 `RunRepository`、`CheckpointRepository`、
`MessageRepository` 和 `ToolExecutionRepository`。一个实现可以同时实现所有端口，
但必须保留以下语义：

1. 入站消息去重与 Run 创建原子完成。
2. Run 认领使用 CAS，并递增执行令牌。
3. 所有运行中写操作校验 owner 与执行令牌。
4. 工具调用按 `(run_id, call_id)` 唯一，并校验工具名和规范化参数。
5. 完成 Run、缓存响应和追加 assistant 消息原子完成。
