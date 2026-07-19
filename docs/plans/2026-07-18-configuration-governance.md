# 配置契约治理实施计划

**目标：** 让代码默认值、YAML 和环境变量形成单一、可验证的配置契约，并在
应用启动时拒绝未知字段和非法值。

## 任务 1：用测试定义配置边界

1. 覆盖 reliability 字段的 YAML 解析和环境变量覆盖。
2. 覆盖非法消息窗口、租约、请求超时、重试次数和孤立 fallback model。
3. 覆盖顶层、普通配置段和 MCP server 的未知字段。
4. 覆盖配置段不是 mapping（映射）时的启动失败。

## 任务 2：实现强类型配置

1. 新增 `ReliabilitySettings`，集中校验超时、尝试次数和 fallback 配对关系。
2. 为 model、approval、session 和 context 补齐构造期校验。
3. 对配置树执行白名单校验，并保留 MCP server 的明确字段集合。
4. 将环境变量解析集中在 `load_settings()`。

## 任务 3：收敛装配边界

1. 模型工厂不再读取环境变量。
2. `bootstrap` 只把 `Settings` 中的 provider 和 model 传给模型工厂。
3. 删除默认 YAML 中没有消费者的 provider、storage、security 和展示字段。

## 任务 4：文档与验收

1. 同步 `config/default.yaml`、`.env.example` 和 README。
2. 运行 `tests/test_architecture.py`。
3. 运行全量 pytest，并执行 `git diff --check`。

说明：本阶段只治理 fallback 配置契约；重试、超时和 provider 切换的执行语义由
路线图“模型可靠性、重试与 fallback”阶段实现。
