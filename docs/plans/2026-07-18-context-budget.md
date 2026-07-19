# Token 预算与会话摘要实施计划

**目标：** 所有模型请求均受 `max_input_tokens` 限制，并通过版本化摘要压缩旧会话，
同时保留当前用户消息和最近工具交互。

## 任务 1：定义上下文领域模型

1. 新增近似 token counter 和请求总量计算。
2. 定义上下文预算、构建结果、会话摘要和预算溢出异常。
3. 用测试固定系统提示保留、当前用户消息保留和工具结果截断行为。

## 任务 2：增加摘要存储

1. 新增 `session_summaries` 表，保存版本、摘要内容和 `through_message_id`。
2. 提供读取最新摘要、保存下一版本以及读取摘要后的消息接口。
3. 测试摘要版本单调递增且覆盖范围准确。

## 任务 3：实现 ContextManager

1. 从最新摘要和其后的消息构建会话上下文。
2. 达到触发阈值时，用可替换 summarizer 压缩旧消息。
3. 每次模型调用前按总预算裁剪，优先删除最旧非关键消息。
4. 对大型工具结果执行带标记的确定性截断。

## 任务 4：接入 Run 生命周期

1. `RunCoordinator.start()` 使用 ContextManager 构建初始消息。
2. 模型调用前保存预算处理后的 checkpoint。
3. checkpoint 保存 `summary_version`，恢复时使用原有消息快照。
4. bootstrap 从强类型 `ContextSettings` 装配上下文管理器。

## 任务 5：验收

1. 运行 `tests/test_context.py` 和会话运行时回归测试。
2. 运行全量 pytest 与 `git diff --check`。
3. 同步默认配置、环境变量示例、README 和总路线图状态。
