# 管理面、审计与数据保留实施计划

## 目标

为 Session、Run、Checkpoint、ToolExecution 与 Approval 提供租户隔离的管理查询，支持 Run 控制、未知工具结果人工处置、审计导出，以及不会误删可恢复 Run 数据的保留策略。

## 安全边界

- Session 在入站时固化 `tenant_id`；管理服务只信任存储中的租户归属。
- 查询、控制、人工处置、审计导出和数据清理分别使用独立 scope 鉴权。
- 所有写操作要求非空原因和幂等键；同一幂等键不得绑定不同请求。
- `running`、`waiting_approval`、`interrupted` 以及未来未知状态均视为非终态，相关 Session 数据不得清理。
- 结果未知的工具只能由管理员明确标记成功或失败，不能自动重新执行。

## 实施步骤

1. 增加管理领域模型、鉴权器、查询/控制服务和薄 API 门面。
2. 为 SQLite/PostgreSQL 增加租户字段、管理查询、幂等操作和只追加审计表。
3. 增加保留服务，按租户和截止时间清理终态数据，并保护存在非终态 Run 的 Session。
4. 覆盖越权、跨租户、幂等冲突、未知工具处置和活跃 Run 保护测试。
5. 更新默认配置、环境变量示例、README 与总路线图状态。

## 验证

```sh
uv run --extra test python -m pytest tests/test_admin.py tests/test_retention.py -q
uv run --extra test python -m pytest -q
uv build
```
