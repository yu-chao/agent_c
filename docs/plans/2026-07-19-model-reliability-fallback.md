# 模型可靠性、重试与 fallback 实施计划

## 目标

为模型调用建立统一的错误分类、请求超时、指数退避、限流等待、熔断和 fallback（备用模型）能力，并确保跨 provider 切换时不会复用供应商专属响应 ID。

## 设计约束

- provider adapter 负责把供应商异常转换为 `RetryableModelError` 或 `PermanentModelError`。
- 运行时核心只依赖通用 `ModelPort`，不识别 OpenAI 或 Anthropic 异常类型。
- 只有明确的暂时性错误会自动重试；认证失败、非法请求等永久错误立即返回。
- fallback 使用完整内部消息重建请求，并清除 `previous_response_id`。
- fallback 调用前保存 checkpoint，调用后记录实际 provider 和 model。

## 实施任务

1. 为超时、429、5xx、永久错误、退避和 fallback 编写失败测试。
2. 新增通用模型错误类型及 adapter 异常分类。
3. 实现请求超时、指数退避、jitter、`Retry-After` 和熔断器。
4. 在模型工厂和 bootstrap 中装配主模型、备用模型及重试策略。
5. 在 fallback 前保存 checkpoint，并记录实际 provider/model。
6. 运行模型专项、架构专项和全量测试。

## 验收命令

```sh
uv run --extra test python -m pytest tests/test_model_reliability.py tests/test_models.py -q
uv run --extra test python -m pytest -q
```
