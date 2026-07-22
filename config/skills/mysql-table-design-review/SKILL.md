---
name: mysql-table-design-review
description: Perform offline review of user-provided MySQL CREATE TABLE DDL, SHOW CREATE TABLE text, or DDL files against organization-specific rules for dictionary-backed enums, deprecated fields, logical deletion, relationship comments, audit timestamps, enum documentation, comments, business naming, dictionary consistency, and oversized main tables. Use when Codex is asked to 校验数据库表 DDL、离线评审建表语句、检查表设计、审查字段注释、检查枚举或字典码、检查逻辑删除与审计字段，或输出数据库设计问题单和整改建议。Never connect to or query a database.
---

# MySQL 表设计评审

依据问题单对用户提供的 DDL 执行离线、证据驱动的表结构评审。把能从 DDL 直接确认的问题与需要数据、代码或业务背景才能确认的风险严格分开。

## 准备

1. 完整读取 [references/review-rules.md](references/review-rules.md)。
2. 只接受用户直接粘贴的 `CREATE TABLE`、`SHOW CREATE TABLE` 文本或 DDL 文件。不要接受数据库连接作为评审输入。
3. 用户只提供表名、截图、连接信息或自然语言描述时，请求其导出并提供完整 DDL；在获得 DDL 前不要假装已经完成结构校验。
4. 用户同时提供业务名称、字典字段清单、废弃字段清单或关联关系时，可将其作为补充上下文；缺少时继续评审，把相应事项标记为 `MANUAL_REVIEW`。
5. 不连接数据库，不调用数据库工具，不执行任何 SQL，包括 `SELECT`、`SHOW`、`EXPLAIN`、DDL 和 DML。

## 评审流程

### 1. 建立结构清单

逐表提取：表名与表注释、字段名/类型/空值/默认值/注释、主键与索引、`enabled`、`create_time`、`modify_time`、疑似枚举字段、疑似关联字段和字段总数。

DDL 无法完整解析时停止对受影响表下结论，指出具体无法解析的片段。不要静默忽略字段或约束。

### 2. 标注证据等级

- `DDL_CONFIRMED`：用户提供的 DDL 文本可直接证明。
- `MANUAL_REVIEW`：需要业务定义、字典数据、应用代码或历史变更才能判断。

不得把字段名猜测、常见实践或问题单中的案例当成当前表的事实。

### 3. 执行规则

按参考文件中的 R01-R10 逐项检查。遵循以下判级：

- `ERROR`：输入证据直接违反明确要求，例如字段无注释、缺少必要审计字段，或审计字段可空且无自动默认值。
- `WARNING`：结构显示明显风险，但是否违规依赖业务，例如主表字段过度膨胀、疑似枚举未写取值、关联字段注释不精确。
- `MANUAL_REVIEW`：仅靠现有输入无法确认，例如字段是否废弃、是否发生物理删除、字典与业务数据是否一致、表名是否匹配真实业务。

不要因为 `MANUAL_REVIEW` 把表判定为确定性不合规。

### 4. 处理 DDL 无法证明的事项

把数据质量、字典内容、字段是否真实废弃、应用是否物理删除、表名是否匹配实际业务等事项列入 `MANUAL_REVIEW`。说明还需要什么信息才能确认，但不要生成或建议执行数据库查询，也不要因为无法查询数据库而中断其余 DDL 评审。

### 5. 给出整改方案

对每个问题给出最小可行整改：目标注释文本、建议字段定义、迁移步骤或待确认问题。涉及字段删除、类型变更、默认值变更或拆表时，说明兼容性、回填、索引、锁表和回滚风险；除非用户明确要求，不直接执行变更。

## 输出格式

先给结论摘要，再输出问题表：

| 严重级别 | 规则 | 对象 | 证据等级 | 证据 | 风险 | 整改建议 |
|---|---|---|---|---|---|---|

随后分别列出：

1. `确定性问题`：只包含 `DDL_CONFIRMED` 的 ERROR/WARNING。
2. `需人工确认`：列出需要补充的业务定义、字典说明、废弃字段清单或关联关系；不要索要数据库连接。
3. `建议 DDL`：仅在用户要求修改方案时给出，保持可审阅且不自动执行。

逐表结论使用：

- `pass`：没有确定性 ERROR。
- `fail`：至少一个确定性 ERROR。
- `incomplete`：DDL 不完整或无法解析；保留已经确认的问题，并指出需补充的 DDL 片段。

结论只由确定性 ERROR 决定。WARNING 和 MANUAL_REVIEW 不单独导致 `fail`。

## 边界

- 不把逻辑删除规则曲解为允许永久保留所有数据；合规、隐私和法定删除要求优先。
- 不根据注释中的表名直接断言真实关联有效；只有目标表也出现在输入 DDL 中时才能核对字段存在性和类型，否则标记人工确认。
- 不为字段数量设置问题单未规定的硬阈值。字段多只触发结构合理性评估。
- 不接受、使用或保存数据库密码和连接串。用户提供连接信息时，提醒其删除敏感信息并改为导出 DDL。
