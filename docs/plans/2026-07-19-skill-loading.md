# Skill 加载、选择与版本快照实施计划

**目标：** 在不引入脚本直执行能力的前提下，让运行时可发现、校验、按需选择
Skill，并保证持久化 Run 恢复时不会静默切换 Skill 版本或内容。

## 任务 1：定义模型与 Loader 边界

1. 用失败测试覆盖目录发现、重复名称和严格 SemVer。
2. 校验 Manifest 字段类型、入口文件存在性及路径边界。
3. 校验 `required_tools` 均已注册，并限制 Skill 声明的文件系统权限。
4. 只读取声明式 Manifest 和 Markdown 入口，不执行入口中的脚本或命令。

## 任务 2：实现确定性选择

1. 根据 activation keywords、名称和描述为候选 Skill 评分。
2. 使用稳定排序并通过 `max_active` 限制注入数量。
3. 仅将选中 Skill 的内容加入本次请求的 system prompt。
4. 上下文预算和实际模型请求使用同一份有效 system prompt。

## 任务 3：持久化版本快照

1. 为选中 Skill 生成名称、版本和规范化内容摘要。
2. 初始及后续 checkpoint 均保存 `skill_snapshots`。
3. 审批 continuation 同步保存快照，避免审批恢复绕过版本校验。
4. 恢复时先校验当前安装内容与快照，再领取 Run 租约。

## 任务 4：配置、文档与验证

1. 增加 `SkillsSettings`、YAML 配置及环境变量覆盖。
2. bootstrap 在工具注册后装配 Loader 和 Selector，并在启动时校验 Manifest。
3. 更新 README、默认配置、`.env.example` 和总路线图状态。
4. 运行 Skill、架构、会话恢复和全量测试。
