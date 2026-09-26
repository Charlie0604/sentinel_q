"""组装：把五个模块串成流水线（架构文档 8.6）。

⚠️ 这里只有接线，没有业务逻辑。业务逻辑属于各个模块。
一旦这里出现"顺手处理一下"的代码，模块边界就开始烂了——
因为那种代码两边都放得下，最后两边都改。

推进顺序：

    1. 冻结 core/ 接口 + migrations/          ← 契约
    2. 五个模块并行开发，各自用 FakeRepo 跑单元测试
    3. 回到这里接线，跑一次端到端
"""

from __future__ import annotations

import logging

from core.config import Settings, paths
from core.repo.supabase import SupabaseRepo

log = logging.getLogger(__name__)


def run_backfill() -> None:
    """首次建库：全域全时间搜索。耗时几小时到一天，跑一次。"""
    raise NotImplementedError("待模块一实现后接线")


def run_update() -> None:
    """日常增量：关键词搜索 + 监控目标，高频、分钟级。"""
    raise NotImplementedError("待模块一实现后接线")


def run_analysis() -> None:
    """模块二：AI 双任务，并发推送、回来一条入库一条（4.2）。"""
    raise NotImplementedError("待模块二实现后接线")


def run_classification(event_id: int) -> None:
    """模块四：对某个事件跑一轮内容归属。"""
    raise NotImplementedError("待模块四实现后接线")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    layout = paths()
    layout.ensure()
    log.info("仓库根目录：%s", layout.root)
    log.info("Supabase：%s", "已配置" if settings.supabase_dsn else "（未配置）")
    raise NotImplementedError("流水线尚未接线")


if __name__ == "__main__":
    main()
