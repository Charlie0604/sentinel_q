# 数据库迁移

表结构的**唯一依据是架构设计文档第五章**。这个目录放的是它的可执行形态。

规则（见架构文档 8.6）：

- 一个编号一个文件，`0001_*.sql`、`0002_*.sql`……
- **只增不改**：已经执行过的迁移文件永远不再编辑，改动一律新增一个编号
- 契约只允许向后兼容地扩展（加字段、加表），不允许改已有字段的语义
- 真要改语义，走版本号字段，而不是原地改

计划中的迁移：

| 编号 | 内容 | 依据 |
|---|---|---|
| `0001_ops.sql` | 爬虫运维表：`crawl_keywords` / `crawl_tasks` / `crawl_runs` / `crawl_queue` | 3.8 与 8.8 |
| `0002_business.sql` | 业务星型模型：`dim_author` / `dim_question` / `dim_event` / `fact_content` / `fact_analysis` / `fact_content_event` / `fact_evidence` | 5.4 |
| `0003_prompt.sql` | `dim_prompt`：提示词的权威副本（不入 git，所以库是唯一能找到它的地方） | 8.9 |

> ⚠️ `0003_prompt.sql` 落地时，记得同步第五章的「表总览」——那张表现在只列了 7 张业务表。
