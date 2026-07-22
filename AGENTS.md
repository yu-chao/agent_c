# Repository Guidelines（仓库贡献指南）

## 项目结构与模块组织

`agent/` 是可安装的 Python 包。与模型供应商无关的运行循环位于 `core/`，API 适配器位于 `models/`，工具注册逻辑位于 `tools/`；`hooks/`、`security/`、`storage/`、`tasks/`、`scheduler/`、`mcp/` 和 `gateway/` 分别承载对应的支撑能力。新增代码应放在其所属子系统中，必要时通过该包的 `__init__.py` 暴露公共接口。测试统一放在 `tests/`。默认配置位于 `config/default.yaml`，环境变量示例位于 `.env.example`。不要提交 `.env`、日志、缓存或其他本地产物。

## 构建、测试与本地开发

- `uv sync --extra test`：按锁文件安装 Python 3.11+ 依赖及 pytest。
- `uv run --extra test python -m pytest -q`：运行全部测试。
- `uv run --extra test python -m pytest tests/test_models.py -q`：运行单个测试模块。
- `uv run python -m agent_runtime --provider openai --model gpt-5`：通过推荐入口启动 CLI；Anthropic 使用 `--provider anthropic`。
- `uv build`：通过 Hatchling 构建 wheel 和源码分发包。

## 编码风格与命名

Python 使用四空格缩进；函数和模块采用 `snake_case`，类采用 `PascalCase`。公共边界应提供类型标注。供应商协议转换应限制在对应适配器内，共享运行逻辑保持供应商无关。优先编写职责单一、含义明确的小模块。项目尚未配置格式化或检查工具，因此代码应遵循 PEP 8，并避免顺带格式化无关文件。

## 测试规范

pytest 自动发现 `tests/test_*.py`；测试函数命名为 `test_<行为>`。使用小型 fake 或 `tmp_path` 隔离外部状态。修复缺陷时必须补充回归测试；涉及工具、权限、存储、网关和模型适配器时，应同时覆盖成功路径与拒绝或异常路径。项目目前没有硬性覆盖率阈值，但新增行为必须有针对性测试。

## 提交与 Pull Request

提交历史以简短摘要为主，也使用过 `feat: add multi-provider agent runtime`。建议采用祈使语气的 Conventional Commit，例如 `fix: 拒绝越界存储路径`，且每次提交只处理一个主题。Pull Request 应说明修改动机、行为变化和验证命令，关联相关 issue，并明确配置或安全影响。仅在 CLI 或网关输出等用户可见内容发生变化时附截图，严禁提交 API Key 或已填写的 `.env`。

## 文档语言规范

本项目新增或修改的文档默认使用简体中文，确保中文读者无需依赖翻译即可理解。命令、代码、路径、API 名称及行业通用术语可保留英文，并在首次出现且可能产生歧义时补充中文说明。文档示例应可直接执行，文件统一使用 UTF-8 编码。修改行为或配置时，同步更新相关 README、示例配置和注释。
