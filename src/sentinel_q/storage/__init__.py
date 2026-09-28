"""数据库模块：全项目 SQL 的唯一出口（架构文档 7.3 规则 2）。

仓储**不再**是各模块共用的契约——2026-09-28 的重构（决策 52）之后，
**只有主程序写库**，采集模块和 AI 模块连数据库的存在都不知道。
所以 `Repo` 留在这里、不往外搬：它现在只服务 `main/` 一个调用方。

| | 管什么 |
|---|---|
| `repo.py` / `supabase.py` / `fake.py` | **怎么读写**。只有 `main/` 调 |
| `migrations/` | **数据长什么样**。表结构，第五章的逐字转写 |
| `migrate.py` / `prompts.py` | 迁移执行、提示词同步（两个方向都要连库） |
| `ingest.py` | 把采集产物变成库里的行（内容 + 分析同批，决策 51） |
"""

from sentinel_q.storage.fake import FakeRepo
from sentinel_q.storage.repo import Repo

__all__ = ["Repo", "FakeRepo"]
