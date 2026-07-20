# 日志、指标与 Trace 实施说明

## 目标

一次入站消息从网关进入后，使用同一个 trace_id 串联网关、Run、模型、工具、
审批和恢复过程；持久化 Run 创建后，所有事件均可通过 run_id 查询。观测系统
自身不可用时，业务 Run 仍按原有语义继续执行。

## 设计

- observability/context.py 使用 ContextVar 传播关联字段，兼容异步任务和
  asyncio.to_thread()。
- observability/metrics.py 提供线程安全计数器、直方图及不可变快照。
- observability/tracing.py 提供 Span、可注入 exporter 和默认进程内查询实现。
- logging_utils.py 输出结构化 JSON，关联当前上下文并递归脱敏敏感字段。
- core/loop.py 记录 Run、模型、工具、审批和恢复 Span 及指标。
- 网关入口在调用运行循环前建立 Trace，运行循环确定 run_id 后补充到父 Span。

模型适配器统一解析输入和输出 token。ModelResponse.cost_usd 是可选费用字段；
上游计费组件填充该字段时会累计 agent_model_cost_usd_total，未提供时不对价格
作猜测。

## 指标

- agent_runs_total、agent_run_duration_seconds
- agent_gateway_messages_total、agent_gateway_duration_seconds
- agent_model_requests_total、agent_model_duration_seconds
- agent_model_tokens_total、agent_model_cost_usd_total
- agent_model_retries_total
- agent_tool_executions_total、agent_tool_duration_seconds
- agent_approvals_total、agent_recoveries_total

指标标签只包含低基数的状态、provider、model、工具名和平台。run_id 等高基数
关联标识仅进入日志和 Trace，不作为指标标签。

## 验证

```sh
uv run --extra test python -m pytest tests/test_observability.py -q
uv run --extra test python -m pytest -q
```

测试覆盖结构化日志关联与脱敏、按 run_id 查询完整 Span、token/费用/重试指标，
以及 metrics 或 exporter 故障时业务继续执行。
