"""仓储层：全项目 SQL 的唯一出口（架构文档 8.2）。

它放在 `core/` 而不是 `modules/m3_storage/`，是因为**它是契约的一部分**：
模块一和模块二都要写 `fact_content`，如果仓储归模块三私有，
"模块之间不互相 import"那条规则当场就破了。

区分一下两者管什么：

| | 管什么 |
|---|---|
| `core/repo/` | **怎么读写**。所有模块共用 |
| `modules/m3_storage/` | **数据长什么样、活多久**。表结构、迁移、分流、归档 |
"""

from core.repo.base import Repo
from core.repo.fake import FakeRepo

__all__ = ["Repo", "FakeRepo"]
