# sentinel_q

An end-to-end public opinion monitoring pipeline: automated content collection, AI-driven relevance/stance/risk classification, and structured evidence preservation — built on a cost-efficient scraping + LLM + Postgres stack.

完整的架构设计见 [知乎舆情监控系统-架构设计文档.md](知乎舆情监控系统-架构设计文档.md)（第八章是工程实现部分）。

## 仓库结构

```
core/       共享契约层：所有模块都依赖它，它不依赖任何模块
modules/    五个业务模块，各自可独立运行、独立测试
tests/      跨模块测试（依赖方向检查、仓储契约）
prompts/    提示词工作区 —— 只入库 *.example 模板，真实提示词在线上库
runtime/    运行文件 —— 不入库，随时可删
secrets/    密钥与知乎登录态 —— 不入库
```

## 快速开始

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 跑测试：不需要数据库，也不需要网络
.venv/bin/pytest
```

`runtime/`、`prompts/`、`secrets/` 三个目录不入库，首次运行时由
`core.config.Paths.ensure()` 自动创建。数据库与模型密钥的配置见 `.env.example`。

## 模块独立运行

```bash
python -m modules.m1_collector --mode backfill    # 模块一：内容采集
python -m modules.m2_analyst run --batch 8        # 模块二：AI 分析
python -m modules.m3_storage migrate              # 模块三：数据存储层
python -m modules.m4_classifier --event 3         # 模块四：自动化事件分类
python -m modules.m5_console                      # 模块五：用户交互界面
```

提示词与线上库同步：

```bash
python -m modules.m2_analyst prompts pull                  # 线上库 → 本地工作区
python -m modules.m2_analyst prompts push -v 3 -n "说明"    # 本地工作区 → 线上库
```

## 两条不能破的规则

1. **依赖只能单向**：`modules/* → core/*`。模块之间禁止互相 import，只能通过 `core/` 或落库传 ID 通信。由 `tests/test_layering.py` 强制——没有它，一次顺手的跨模块 import 就会把模块粘回一坨。

2. **问 AI 之前，内容必须已经落在 Supabase 里**。推论：本地文件永远可以被安全删除，最坏代价是重爬一段（廉价）；而重问一次 AI 是昂贵的，所以它绝不允许依赖只在本地、还没落库的东西。

## 边界

本系统不逆向破解知乎的加密签名参数，也不搭建用于规避风控的代理池或打码平台。
采集依赖真实浏览器 + 真实账号 + 放慢节奏——用有规避风控痕迹的方式采集的数据，
会削弱证据的正当性。正式法律取证文件不进这套系统，只在 `fact_evidence` 登记元信息。
