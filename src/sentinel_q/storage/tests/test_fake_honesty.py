"""`FakeRepo` 不说谎。

这个方法**只实现它能如实模拟的原语**，其余的故意不实现（调用处 AttributeError）。
分界线不是"难不难写"，是**要不要把数据库的语义重写一遍**：

    单表筛选/排序/limit + 按外键取回一行   →  诚实
    多行 join（扇出）、聚合、全文相似度     →  不诚实

后半类的实现写出来就是**第二个查询引擎**：在 Python 里手写一遍条件 join 和
排序，与 SQL 有一百处可以不一致，而测试会一直绿。更糟的是它让测试**看起来**
覆盖了那些查询，于是没人再去做真库验证。

这个文件把分界线机械化。它挡住的是一件很具体的事：
**为了让某个用例通过，顺手给 fake 补一个猜的实现。**
"""

from __future__ import annotations

import pytest

from sentinel_q.storage.fake import HONEST, NOT_HONEST, FakeRepo
from sentinel_q.storage.repo import Repo

# 协议里的方法名（`Repo` 是 Protocol，方法就是它的公开属性）
PROTOCOL = frozenset(name for name in dir(Repo) if not name.startswith("_"))


def test_protocol_is_not_empty() -> None:
    """先确认探针本身有读数——不然下面两条断言会在空集上"通过"。"""
    assert len(PROTOCOL) > 30


def test_honest_and_not_honest_are_disjoint() -> None:
    assert not (HONEST & set(NOT_HONEST))


def test_every_protocol_method_is_classified() -> None:
    """协议里每个方法都必须被归置过——不许有"没人想过"的漏网之鱼。

    新加协议方法时这条会红，逼着人当场回答"fake 能不能如实模拟它"，
    而不是拖到某个测试莫名失败时才发现。
    """
    unclassified = PROTOCOL - HONEST - set(NOT_HONEST)
    assert not unclassified, f"这些协议方法既没进 HONEST 也没进 NOT_HONEST：{sorted(unclassified)}"


def test_fake_implements_exactly_the_honest_set() -> None:
    """双向断言。

    正向：HONEST 里的每个方法，FakeRepo 都真的有。
    反向：NOT_HONEST 里的每个方法，FakeRepo **必须没有**——
         有了就说明有人为了让用例通过偷偷补了一个猜的实现。
    """
    fake = FakeRepo()

    missing = sorted(name for name in HONEST if not hasattr(fake, name))
    assert not missing, f"HONEST 声明了但 FakeRepo 没实现：{missing}"

    smuggled = sorted(name for name in NOT_HONEST if hasattr(fake, name))
    assert not smuggled, (
        f"这些方法的语义 FakeRepo 模拟不了（{NOT_HONEST}），但它却实现了：{smuggled}。"
        "要么删掉实现，要么改走 @pytest.mark.integration 用真库测——"
        "不要让测试替身去猜数据库的行为。"
    )


@pytest.mark.parametrize("name", sorted(NOT_HONEST))
def test_unimplemented_methods_raise_attribute_error(name: str) -> None:
    """必须是 AttributeError，**不是 NotImplementedError 之类的桩**。

    桩会让人以为"补一下就行"，而 AttributeError 在测试里是当场、无歧义的失败。
    """
    fake = FakeRepo()
    with pytest.raises(AttributeError):
        getattr(fake, name)


def test_not_honest_covers_the_query_layer() -> None:
    """分界线本身：一切带 join / 聚合 / 相似度的原语都在不诚实那侧。

    这条防的是**边界被悄悄挪动**——比如某天有人把 `list_events` 挪进 HONEST，
    理由是"它那个 group by 其实很简单"。
    """
    expected = {
        "search_contents",  # 条件 join + 排序 + 分页
        "count_contents",
        "alert_rows",  # 三表 join
        "count_by_risk",  # group by
        "list_events",  # left join + group by + having
        "count_stale_judgments",  # 跨表比对版本的 count
        "prescreen_rows",  # pg_trgm，没有内存等价物
    }
    assert expected <= set(NOT_HONEST)
