"""本地运维账本的测试（架构文档 3.8）。

这些用例要钉住的是**"本地文件必须可重建"这条硬约束在实现上的表现**：
追加写、崩溃只丢最后一行、半截文件不能让整个任务读不出来。

刻意用 tmp_path，不碰真的 `runtime/`——运维文件是本地状态，测试不该污染它。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from sentinel_q.collector.ops import OpsStore, RunSample, TaskState, UrlEntry, shard


def test_new_run_creates_dir_and_state(tmp_path: Path) -> None:
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="20260926-1030")

    assert (tmp_path / "20260926-1030" / "task.json").exists()
    assert store.state.run_id == "20260926-1030"
    assert store.state.mode == "backfill"
    assert store.state.status == "pending"


def test_urls_round_trip(tmp_path: Path) -> None:
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_urls(
        [
            UrlEntry(url="https://www.zhihu.com/question/1/answer/2", content_type="answer"),
            UrlEntry(url="https://zhuanlan.zhihu.com/p/3", content_type="article"),
        ]
    )

    assert [e.url for e in store.iter_urls()] == [
        "https://www.zhihu.com/question/1/answer/2",
        "https://zhuanlan.zhihu.com/p/3",
    ]
    assert store.url_count() == 2


def test_search_metadata_round_trips(tmp_path: Path) -> None:
    """搜索页白送的元数据要**原样**落盘再读回来——阶段二靠它分诊。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_urls(
        [
            UrlEntry(
                url="https://zhuanlan.zhihu.com/p/3",
                content_type="article",
                keyword="考研",
                title="某篇文章",
                excerpt="不点阅读全文就能看到的那段",
                voteup_count=1770,
                comment_count=65,
            )
        ]
    )

    (back,) = list(store.iter_urls())
    assert (back.title, back.excerpt) == ("某篇文章", "不点阅读全文就能看到的那段")
    assert (back.voteup_count, back.comment_count) == (1770, 65)


def test_lines_written_before_the_metadata_existed_still_parse(tmp_path: Path) -> None:
    """⭐ **旧清单必须继续读得进来。**

    元数据是后加的，而这之前已经采过好几轮、盘上躺着没有这些键的
    `urls.jsonl`（2026-09-26 那次全量实跑就有 2160 行）。
    读的时候如果因为缺键就抛异常，那些已经采到的 URL 会**整批读不出来**——
    而它们是花十分钟真实访问换来的，重采的代价不只是时间。
    """
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.urls_path.write_text(
        json.dumps(
            {
                "url": "https://zhuanlan.zhihu.com/p/3",
                "content_type": "article",
                "question_id": None,
                "keyword": "考研",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    (back,) = list(store.iter_urls())
    assert back.url == "https://zhuanlan.zhihu.com/p/3"
    assert back.title is None
    assert back.voteup_count == 0


def test_appending_does_not_rewrite_existing_lines(tmp_path: Path) -> None:
    """追加写：第二次 append 不能把第一批冲掉——这是崩溃时只丢最后一行的前提。

    如果实现成"读出整个 JSON、改完写回"，崩在写回中途会把已有内容一起毁掉。
    """
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_urls([UrlEntry(url="https://www.zhihu.com/question/1/answer/2")])
    store.append_urls([UrlEntry(url="https://www.zhihu.com/question/1/answer/3")])

    assert store.url_count() == 2


def test_every_line_is_flushed_before_the_next_is_written(tmp_path: Path) -> None:
    """⭐ 逐行 flush：进程**没机会善后**地死掉时（SIGKILL / 断电），已采到的必须在盘上。

    ⚠️ 为什么要从生成器**中途**去读文件，而不是"抛个异常看还剩几行"：
    `with open(...)` 在异常传播出去时会走 `close()`，而 **close() 本身就会 flush**——
    所以那种写法**测试恒过，哪怕 flush 写在循环外面**，等于什么都没测。
    只有在循环还没结束时去读，才分得出两者的差别。

    这不是理论问题：一次搜索要跑十几分钟、几千条，整批写完才 flush 的话，
    崩掉丢的是整批，而这个函数的全部意义就是"崩了也只丢最后一行"。
    """
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    seen: list[str] = []

    def entries() -> Iterator[UrlEntry]:
        yield UrlEntry(url="https://www.zhihu.com/question/1/answer/2")
        # ↓ 此刻循环还在跑，文件没被 close 过。盘上必须已经有第一条。
        seen.append(store.urls_path.read_text(encoding="utf-8"))
        yield UrlEntry(url="https://www.zhihu.com/question/1/answer/3")

    store.append_urls(entries())

    assert len(seen) == 1
    assert "answer/2" in seen[0]
    assert "answer/3" not in seen[0]  # 第二条还没写，不该提前出现


def test_truncated_last_line_is_skipped(tmp_path: Path) -> None:
    """崩在追加中途会留下半截 JSON，读的时候要跳过它，而不是让整个任务读不出来。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_urls([UrlEntry(url="https://www.zhihu.com/question/1/answer/2")])
    with store.urls_path.open("a", encoding="utf-8") as fh:
        fh.write('{"url": "https://www.zhihu.com/question/1/ans')  # 模拟写一半断电

    assert [e.url for e in store.iter_urls()] == ["https://www.zhihu.com/question/1/answer/2"]


def test_resume_from_existing_run(tmp_path: Path) -> None:
    """断点续跑：从磁盘上的 task.json 恢复，游标必须还在（3.8）。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.advance(keyword_index=12, page=3)

    resumed = OpsStore.resume(tmp_path / "r1")

    assert resumed.state.cursor == {"keyword_index": 12, "page": 3}


def test_advance_persists_immediately(tmp_path: Path) -> None:
    """每处理完一个关键词就要落盘——否则崩了断点就退回上一次。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.advance(keyword_index=1)

    on_disk = json.loads((tmp_path / "r1" / "task.json").read_text(encoding="utf-8"))
    assert on_disk["cursor"] == {"keyword_index": 1}


def test_list_runs_skips_corrupt_task_files(tmp_path: Path) -> None:
    """一个任务目录写坏了，不该影响其余的能被列出来。"""
    OpsStore.new_run(tmp_path, mode="backfill", run_id="20260926-1030")
    broken = tmp_path / "20260927-0900"
    broken.mkdir()
    (broken / "task.json").write_text("{ 半截", encoding="utf-8")

    runs = OpsStore.list_runs(tmp_path)

    assert [r.run_id for r in runs] == ["20260926-1030"]
    assert all(isinstance(r, TaskState) for r in runs)


def test_runs_log_is_append_only_and_filtered(tmp_path: Path) -> None:
    """限流采样是本模块唯一不可重建的东西：只追加，且要能按关键词取回历史。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_sample(RunSample(at="2026-09-26T10:00:00Z", keyword="甲", kind="search", result_count=100))
    store.append_sample(RunSample(at="2026-09-26T11:00:00Z", keyword="乙", kind="search", result_count=50))
    store.append_sample(RunSample(at="2026-09-26T12:00:00Z", keyword="甲", kind="search", result_count=20))

    samples = store.recent_samples("甲")

    assert [s.result_count for s in samples] == [100, 20]  # 拿它和历史均值比，看是否骤降


def test_keywords_are_read_from_local_file(tmp_path: Path) -> None:
    """关键词清单也在本地：它反映"我们要监测什么"，敏感度和提示词同级（7.7）。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.keywords_path.write_text(
        '{"keyword": "甲"}\n{"keyword": "乙"}\n', encoding="utf-8"
    )

    assert store.load_keywords() == ["甲", "乙"]


def test_missing_keywords_file_is_empty_not_an_error(tmp_path: Path) -> None:
    """人工维护的文件——不存在就返回空，不该让采集任务起不来。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    assert store.load_keywords() == []


def test_the_two_body_files_are_separate(tmp_path: Path) -> None:
    """能力四和能力二各写各的（决策 53）。

    原来两条能力共用一份 `contents.jsonl`，因为采集期间库里看不到本轮内容、
    它们互相看不见对方采过什么。现在那件事由**顺序 + 一条被逐段划掉的
    【更新列表】**解，产物就分开了。这条用例钉住"分开"：写进 answers 的东西
    不能出现在 contents 里，反之亦然。
    """
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_contents([{"url": "https://zhihu.com/question/1/answer/1"}], name="answers")
    store.append_contents([{"url": "https://zhuanlan.zhihu.com/p/9"}])

    assert [r["url"] for r in store.iter_contents(name="answers")] == [
        "https://zhihu.com/question/1/answer/1"
    ]
    assert [r["url"] for r in store.iter_contents()] == ["https://zhuanlan.zhihu.com/p/9"]
    assert store.body_path("answers").name == "answers.jsonl"
    assert store.contents_path == store.body_path("contents")


def test_a_misspelled_body_file_raises_instead_of_silently_creating_one(
    tmp_path: Path,
) -> None:
    """⚠️ 拼错的 `"answer"`（少个 s）必须当场报错。

    当成合法路径拼出去的话，会悄悄造出第三份文件——而两份产物看起来都"跑通了"，
    只是其中一份永远读不到东西。这种错要等到某天发现"怎么一条回答都没入库"
    才会浮现，那时已经采过好几轮了。
    """
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")

    with pytest.raises(ValueError):
        store.body_path("answer")

    with pytest.raises(ValueError):
        store.iter_contents(name="answer")


def test_shards_cover_everything_exactly_once() -> None:
    """切片的两条硬要求：**合起来是全集，两两不相交**（架构文档 3.8，决策 54）。

    重叠 → 两个人采同一条，白问一次 AI；
    漏掉 → 静默缺数据，事后看不出来。所以这两条是切片存在的全部意义。
    """
    items = [f"关键词{i}" for i in range(23)]  # 23 不能被 4 整除，故意留个余数

    parts = [shard(items, index=i, count=4) for i in range(4)]

    assert sorted(x for part in parts for x in part) == sorted(items)  # 全覆盖
    assert sum(len(p) for p in parts) == len(items)  # 不重叠
    assert [len(p) for p in parts] == [6, 6, 6, 5]  # 余数摊给前面几个人


def test_shard_round_robins_instead_of_cutting_in_half() -> None:
    """⚠️ 按位置轮流分，**不是**顺序切两半。

    顺序切的话，位置靠前的那一批会整批落在第一个人头上。而清单靠前的
    往往是热门关键词/热门问题——那个人的耗时就变成整轮的下限，等于没并行。
    """
    items = ["a", "b", "c", "d", "e", "f"]

    assert shard(items, index=0, count=2) == ["a", "c", "e"]  # 不是 ["a", "b", "c"]
    assert shard(items, index=1, count=2) == ["b", "d", "f"]


def test_shard_rejects_an_out_of_range_index_loudly() -> None:
    """默默返回空列表的话，那个人会安静跑完一个空任务、报"采集完成 0 条"。"""
    with pytest.raises(ValueError):
        shard(["a"], index=2, count=2)

    with pytest.raises(ValueError):
        shard(["a"], index=-1, count=2)

    with pytest.raises(ValueError):
        shard(["a"], index=0, count=0)


def test_a_shard_is_persisted_so_it_can_be_resumed(tmp_path: Path) -> None:
    """切片要落盘——否则换人/重跑时说不清"上次领到哪一段"，会重叠或漏采。"""
    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.save_shard("小王", ["甲", "丙"])

    assert store.load_shard("小王") == ["甲", "丙"]
    assert store.load_shard("小李") == []  # 还没切给自己，不是错误


def test_questions_is_a_product_file_but_not_a_body_file(tmp_path: Path) -> None:
    """⭐ 能力五的 `questions.jsonl` 能落盘，但**不能混进 `BODY_FILES`**。

    两个集合管的是两件事：

        BODY_FILES     哪些行装得成 `ContentRecord`、能进 `fact_content`
        PRODUCT_FILES  这个任务**全部产物文件**（`body_path()` 的白名单）

    ⚠️ 把 `questions` 加进 `BODY_FILES` 会**静默弄坏三个夹具脚本**
    （`ingest_fixture` / `replay_fixture` / `judge_fixture` 里的
    `for name in ops.OpsStore.BODY_FILES:`）——问题详情的行里没有
    `zhihu_id`，`extract.from_document` 会一条条拒掉、逐行打
    "装不成记录"，于是它们报出来的那个数凭空多出问题的行数。
    而**那个数是多份 README 里写明的验收基准**。

    这条用例拿"真的去遍历一遍"来证明，而不是只比两个元组：断言的是
    那三个脚本的行为，不是常量的拼写。
    """
    from sentinel_q.collector import extract
    from sentinel_q.collector.ops import OpsStore

    assert "questions" in OpsStore.PRODUCT_FILES
    assert "questions" not in OpsStore.BODY_FILES, "混进去会弄坏三个夹具脚本的验收数字"

    store = OpsStore.new_run(tmp_path, mode="backfill", run_id="r1")
    store.append_contents([{"zhihu_qid": "1", "title": "标题"}], name="questions")
    # ⚠️ 正文那边也真写一行。不留的话下面那次遍历是空的，
    #    "装不出记录"就成了废话——空集合当然装不出任何东西。
    store.append_contents(
        [
            {
                "content_type": "answer",
                "zhihu_id": "456",
                "url": "https://www.zhihu.com/question/1/answer/456",
            }
        ]
    )

    # 能写能读（白名单放行了）……
    assert [r["zhihu_qid"] for r in store.iter_contents(name="questions")] == ["1"]
    assert store.body_path("questions").name == "questions.jsonl"

    # ……但夹具脚本那套遍历里**只有正文那一行**：问题详情那一行根本不在，
    #    所以它们报出来的"装不成记录"不会凭空多出问题的行数。
    rows = [r for name in OpsStore.BODY_FILES for r in store.iter_contents(name=name)]
    assert [r.get("zhihu_id") for r in rows] == ["456"], (
        "遍历 BODY_FILES 只该看见正文——问题详情不在里面"
    )
    assert extract.from_document(rows[0]) is not None, "正文那一行照样装得成"
