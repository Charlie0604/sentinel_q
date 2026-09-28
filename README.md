# sentinel_q

An end-to-end public opinion monitoring pipeline: automated content collection, AI-driven relevance/stance/risk classification, and structured evidence preservation — built on a cost-efficient scraping + LLM + Postgres stack.

完整的架构设计见 [知乎舆情监控系统-架构设计文档.md](知乎舆情监控系统-架构设计文档.md)（第七章是工程实现部分）。

## 模块是工具，主程序是流水线

三个模块各自只做一件事，**全都不知道数据库的存在**：

| 模块 | 只做这一件事 | 明确不做 |
|---|---|---|
| `sentinel_q.collector` | 采集：搜索 / 正文 / 评论 / 问题下全量回答 / 问题详情，以及登录知乎、保持登录态 | **不询问数据库**，只依赖本地文件（存内容、去重、当任务列表） |
| `sentinel_q.analyst` | 按不同任务发不同 prompt 给模型，解析模型的答复。**事件分类也在这里**（同一套机器，只差 prompt 内容和落哪张表） | **不下载/更新 prompt**，prompt 直接从本地读 |
| `sentinel_q.storage` | **跟数据库通信的全部内容**：表结构、迁移、读写、prompt 同步 | —— |

谁来接线？`sentinel_q.main`。它是**唯一**知道"先采集、再判定、再入库"的地方：

```
main
 ├─ storage.existing_urls() / existing_questions()   ← 开跑前一次取全，当本轮的去重基准
 ├─ collector ①搜索 → ②问题 → ③问题下全量回答 → ④削 → ⑤正文
 │    每个能力只认「一条任务清单 + 一张去重表」，清单被逐段消费（架构文档 3.3）
 ├─ analyst.judge(…)                                 ← 给一条内容，产出判定
 └─ storage.insert(内容, 判定)                        ← 一次写入 fact_content + fact_analysis
```

**顺序是硬的**：问题排在内容前面判，因为"哪些问题相关"是"去采哪些问题下的回答"的输入。

**要并行就把清单切一半。** 每个能力的输入都是一份不重叠的清单（能力一=关键词、
能力四=问题清单、能力二=URL 列表），切成 n 段分给 n 个人即可 —— 不需要队列，也不需要抢任务。
产物是往同一个文件追加写（只增不改，所以不用文件锁）。详见架构文档 3.8 末尾。

**这条流程就是那条不变量**：任何进入 `fact_content` 的内容都必须先经过 AI 判定，
判完才写——两行同批落库，所以库里不存在"已入库但没分析"的行。两处例外都是
"入库这一步的**选择**不由 AI 的相关性判定来做"，不是"不经过 AI"：问题靠 `zhihu_qid`
查重占位；问题一旦判为相关，它下面的回答**不区分相关性一律入库**。

推论：**本地文件必须可以重建**——能从库里推出来，或者重爬一次再重问一次 AI 就能拿到。
做不到这一点的东西，就不该放本地。

`console/` 是前端，**不是模块**，通过 `main` 暴露的接口访问。

## 仓库结构

代码全在 `src/`，运行数据在仓库根——一眼分得出哪是代码、哪是数据。

```
src/sentinel_q/
├── collector/    采集模块
├── analyst/      AI 模块（含事件分类）
├── storage/      数据库模块：全项目唯一出现 SQL 的地方
├── shared/       共享层：数据格式 + 纯函数 + 配置
└── main/         主程序：唯一接线的地方

console/          前端（不是 Python 包，所以不进 src/）
tests/            跨包契约测试

prompts/          ⛔ 运行数据：提示词工作区（人工编辑；只入库 *.example 模板）
runtime/          ⛔ 运行数据：断点、URL 清单、限流采样、提示词快照、日志
secrets/          ⛔ 运行数据：密钥 + 知乎登录态
```

数据库里**只有业务数据和提示词**，没有爬虫的运维表——任务断点、URL 清单、
限流采样都是 `runtime/` 下的本地文件，因为采集模块根本连不上数据库。

## 快速开始

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # 必需：src/ 布局，不装就跑不起来

# 跑测试：不需要数据库，也不需要网络
.venv/bin/pytest
```

`runtime/`、`prompts/`、`secrets/` 三个目录不入库，首次运行时由
`sentinel_q.shared.config.Paths.ensure()` 自动创建。数据库与模型密钥的配置见 `.env.example`。

## 各模块独立运行

模块自带 CLI，可以单独跑；但**跑完整流程要走主程序**。

```bash
python -m sentinel_q.collector search --keyword "…"    # 采集模块：能力一，搜关键词
python -m sentinel_q.analyst run --batch 8             # AI 模块：跑一轮判定
python -m sentinel_q.storage migrate                   # 数据库模块：应用迁移
python -m sentinel_q.main ingest --run <run-id>        # 主程序：把一次采集的产物写进库
```

⚠️ **采集和入库是两条命令。** 采集只产文件（`runtime/ops/<run>/` 下，
**不连数据库**），入库是主程序的事。所以采集中途崩了、或者库临时挂了，
对着产物再跑一次 `ingest` 就行，**不用再爬一遍**：

```bash
python -m sentinel_q.collector content --run 20260928-153000   # 浏览器，几十分钟，不连库
python -m sentinel_q.main ingest --run 20260928-153000         # 连库，几秒钟，不开浏览器
python -m sentinel_q.main ingest --run 20260928-153000 --dry-run   # 走内存库，不碰线上库
```

提示词同步（拉取/推送都要连数据库，所以归数据库模块）：

```bash
python -m sentinel_q.storage prompts pull                  # 线上库 → 本地工作区
python -m sentinel_q.storage prompts push -v 3 -n "说明"    # 本地工作区 → 线上库
```

## 四条不能破的规则

1. **模块之间不互相 import**，只依赖 `shared/`；`shared/` 不反向依赖任何模块。
2. **只有 `storage/` 里有 SQL。** `collector/` 和 `analyst/` 里出现 `psycopg`、`supabase` 或一句 `insert into` 就是 bug——它们不该知道数据库存在。
3. **只有 `main/` 调 `storage/`。**
4. **禁用相对 import**（相对 import 会绕过前三条检查）。

这四条都由 `tests/test_layering.py` 用 AST 扫描强制。没有它，一次顺手的跨模块 import
就会把模块粘回一坨，而且要到很久以后（改一处崩三处）才发现。

## 边界

本系统不逆向破解知乎的加密签名参数，也不搭建用于规避风控的代理池或打码平台。
采集依赖真实浏览器 + 真实账号 + 放慢节奏——用有规避风控痕迹的方式采集的数据，
会削弱证据的正当性。正式法律取证文件不进这套系统，只在 `fact_evidence` 登记元信息。
