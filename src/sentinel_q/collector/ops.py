"""本地运维账本：断点、URL 清单、限流采样（架构文档 3.8）。

**这里的东西全部不入库，全部可丢弃。**

爬虫是本地运行的。运维状态放线上库意味着每次翻页、每条 URL 都走一次
网络往返，而它本来就不需要跨机器共享——2~4 人各领不重叠的关键词切片，
唯一的协调点是"别重复入库"，那由 `fact_content.url` 的唯一约束兜着。

    runtime/ops/
    ├── keywords.jsonl              # 关键词清单（人工维护）
    ├── runs.jsonl                  # 历次采集的采样日志（供 3.7 限流检测）
    └── <run-id>/                   # 一次任务一个目录
        ├── task.json               # 任务类型、状态、断点游标
        ├── urls.jsonl              # 能力一产出的 URL 清单（第 1 步）
        ├── update_list.jsonl       # 【更新列表】= urls − 初始列表，被逐段消费
        ├── answers.jsonl           # 能力四正文文件（第 3 步）
        ├── contents.jsonl          # 能力二正文文件（第 5 步）
        └── questions.jsonl         # 能力五问题详情（第 2 步）

⚠️ **硬约束（7.8 推论二）：放这里的东西必须可以重建**——要么能从数据库
推出来，要么重爬一次就能拿到。逐项对照见 3.8 的表格。唯一的例外是
`runs.jsonl`：限流基线没有上游可重建，丢了就得从头积累，所以它要带走。

⚠️ 两个**正文文件**（`answers.jsonl` / `contents.jsonl`）的定位在 2026-09-28
变了（决策 51）：它们原本只是"人工核对用的产物"，现在**采集与分析通过它们衔接**
——内容、AI 判定结果、断点标记写在**同一行**，断点就是"哪一行的布尔值还是 false"。
连带一个后果：**查重不能只查库**，因为采集期间库里看不到本轮内容
（不变量是"AI 判完才入库"）——这正是【更新列表】要落成文件的原因（3.3）。
它们仍满足推论二，只是重建代价变成"重爬一次 + 重问一次 AI"。

⚠️ 2026-09-29（决策 53）：**两条能力各写各的文件，不再共用一份。**
原来靠"共用文件"解的问题（能力四采过的能力二看不见），现在靠**顺序 +
一条被逐段划掉的【更新列表】**解。两份格式仍然一样（同一个 `_ContentsDoc`）。

⚠️ 格式用 JSONL 而不是 CSV：一行一条、追加写，崩溃时最多丢最后一行；
字段可以演进，上游多抓一个字段不用改历史文件的表头；也不用处理正文里
逗号/换行/引号的转义。需要人工看的时候再导出 CSV。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

TaskMode = Literal["backfill", "update", "follow_up"]
TaskStatus = Literal["pending", "running", "paused_need_captcha", "done", "failed"]


@dataclass
class UrlEntry:
    """`urls.jsonl` 的一行——阶段一列表采集的产出，也是阶段二的输入。

    底下那三个元数据字段是**搜索页白送的**：标题、赞同数、评论数本来就渲染在
    结果卡片上，`parse.parse_search_item_ex` 顺手就把它们抠出来了。
    之所以要一路带到阶段二，是因为**阶段二每打开一条要花十秒**——
    在这十秒之前拿免费信息过一遍（标题对不对题、赞同是不是 0），
    比打开之后才发现不值得要便宜得多。

    ⚠️ 它们**只是分诊用的线索，不是证据**。搜索页上显示的是采集那一刻的
    快照，而赞同数会变。要作为证据的热度数字，得从内容页现取（阶段二）。
    """

    url: str  # 规范化后的 URL（shared.urlnorm），天然去重
    content_type: str | None = None
    question_id: str | None = None  # 从 URL 解析出的所属问题，阶段三按它取全量
    keyword: str | None = None  # 哪次搜索搜出来的，排查用

    # ── 搜索页白送的元数据（分诊用，见类文档）─────────────────────
    # ⚠️ 没有作者字段：实测 198 张搜索卡片**没有一张**含 `/people/` 链接，
    #    搜索结果根本不展示作者。作者只能在阶段二从内容页取。
    title: str | None = None
    excerpt: str | None = None
    """卡片上的缩略信息，**在页面上被截断了**（每张卡都有「阅读全文」按钮）。
    够用来判断"这条讲的是什么"，不能当正文用。"""
    voteup_count: int = 0
    """⚠️ 只可信"有"，**不可信"无"**：0 既可能是真没人赞，也可能是卡片没渲染完。

    实测 196 张卡里有 8 张是 0（其余 188 张有值），没去分辨那 8 张属于哪种。
    所以拿它**排序**是安全的（把可疑的排后面），拿它**丢弃**则会误杀。
    """
    comment_count: int = 0
    """同上。实测 196 张卡里有 48 张没印评论数，解析结果就是 0。"""


@dataclass
class RunSample:
    """`runs.jsonl` 的一行——一次采集的采样（3.7 静默限流检测用）。

    ⚠️ 这是本模块唯一**不可重建**的东西。限流检测要拿本次结果数和历史均值
    比，丢了基线就断档，只能从头重新积累。所以它 append-only、只追加不修改，
    换机器/重装环境时要记得把 `runs.jsonl` 一并带走。
    """

    at: str  # ISO8601，本地任务不需要单调时钟
    keyword: str
    kind: str  # search / question / answer
    result_count: int
    page: int | None = None
    elapsed_ms: int | None = None


@dataclass
class TaskState:
    """`task.json` 的内容——任务元信息 + 断点游标。"""

    run_id: str
    mode: TaskMode
    status: TaskStatus = "pending"
    # 断点游标。按任务类型含义不同（3.8）：
    #   backfill  —— {"keyword_index": 12, "page": 3}
    #   update    —— {"since": "2026-09-25T00:00:00Z"}
    #   follow_up —— {"question_index": 340}
    cursor: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


class OpsStore:
    """`runtime/ops/` 的读写。一个实例对应一次任务。"""

    def __init__(self, ops_dir: Path, state: TaskState) -> None:
        self.root = ops_dir
        self.run_dir = ops_dir / state.run_id
        self.state = state

    # ── 打开 / 创建 ────────────────────────────────────────────────

    @classmethod
    def new_run(
        cls,
        ops_dir: Path,
        mode: TaskMode,
        run_id: str | None = None,
        note: str | None = None,
    ) -> OpsStore:
        """开一次新任务。`run_id` 不传则按时间戳生成。"""
        state = TaskState(run_id=run_id or _timestamp_id(), mode=mode, note=note)
        store = cls(ops_dir, state)
        store.run_dir.mkdir(parents=True, exist_ok=True)
        store.save_state()
        return store

    @classmethod
    def resume(cls, run_dir: Path) -> OpsStore:
        """从一个已存在的任务目录恢复——断点续跑走这里（3.8）。"""
        state = TaskState(**json.loads((run_dir / "task.json").read_text(encoding="utf-8")))
        return cls(run_dir.parent, state)

    @classmethod
    def list_runs(cls, ops_dir: Path) -> list[TaskState]:
        """按时间倒序列出历次任务，供"接着上次跑"选择。"""
        states = []
        for task_file in sorted(ops_dir.glob("*/task.json"), reverse=True):
            try:
                states.append(TaskState(**json.loads(task_file.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError):
                continue  # 半截文件（写到一半崩了）直接跳过，不影响其余任务
        return states

    # ── 断点 ────────────────────────────────────────────────────────

    def save_state(self) -> None:
        """落盘 task.json。

        ⚠️ 覆盖写。它很小（几个字段），而 URL 清单这种大东西在 urls.jsonl 里
        追加写——把两者混在一个文件里就会变成"读整个 JSON、改完写回"，
        崩溃时会把已有内容一起毁掉。这是拆成两个文件的全部理由。
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.run_dir / "task.json.tmp"
        tmp.write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.run_dir / "task.json")  # 原子替换，不会读到半截

    def advance(self, **cursor: Any) -> None:
        """推进断点并落盘——每处理完一个关键词/一页就调一次。"""
        self.state.cursor.update(cursor)
        self.save_state()

    def set_status(self, status: TaskStatus) -> None:
        self.state.status = status
        self.save_state()

    # ── URL 清单 ────────────────────────────────────────────────────

    @property
    def urls_path(self) -> Path:
        return self.run_dir / "urls.jsonl"

    def append_urls(self, entries: Iterator[UrlEntry] | list[UrlEntry]) -> int:
        """追加一批 URL，返回写入条数。

        一行一条、**写完即 flush**——进程崩掉时最多丢最后一行。这里不做去重：
        去重是入库前的成本闸门（3.9 第 3 条），由数据库的唯一索引说了算。

        ⚠️ flush 必须在**循环里**，不能挪到循环外。一次搜索可能跑十几分钟、
        采几千条，整批写完才 flush 的话，中途崩掉丢掉的是这一整批——
        而这个函数的全部意义就是"崩了也只丢最后一行"。
        （早先这里确实写在循环外，文档却已经这么写了。是文档对、代码错。）
        """
        count = 0
        with self.urls_path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
                fh.flush()
                count += 1
        return count

    def iter_urls(self) -> Iterator[UrlEntry]:
        """逐条读回 URL 清单。

        **这就是第 1 步的断点**：进程崩了重读一遍，接着往下搜。

        ⚠️ 这里**不做库查重**——采集模块连不上数据库（7.3 规则 2）。查重靠
        **开跑前一次性取全的初始列表**去减，结果落成【更新列表】（3.3）。
        不需要第二个"已处理清单"文件：那个清单的权威副本在库里，
        本地再存一份就是 7.8 说的第二个真相源。
        """
        if not self.urls_path.exists():
            return
        with self.urls_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield UrlEntry(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue  # 崩在最后一行会留下半截 JSON，跳过即可

    def url_count(self) -> int:
        return sum(1 for _ in self.iter_urls())

    # ── 正文产物 ────────────────────────────────────────────────────
    #
    # 几份产物文件，格式一样、互相独立（架构文档 3.3 / 7.8，决策 53）：
    #
    #   questions.jsonl  ← 能力五：问题详情（第 2 步）——**不是正文**，
    #                      装的是标题/描述/热度指标，进的是 dim_question
    #   answers.jsonl    ← 能力四：问题下的全部回答（第 3 步）
    #   contents.jsonl   ← 能力二：清单里剩下的正文（第 5 步），
    #                      也是"手动收评论"那条路的落点
    #
    # 名字就是这里的 key，文件落在 <run>/<name>.jsonl。几条能力**不共用一份**：
    # 它们共享的是【更新列表】（谁先跑谁划掉自己采到的），不是文件。

    BODY_FILES = ("answers", "contents")
    """**正文文件**：行装得成 `ContentRecord`、能进 `fact_content` 的那两份。

    ⚠️ `questions.jsonl` **不在这里，而且不能加进来**。它装的是问题详情
    （进 `dim_question`），行里没有 `zhihu_id`，`extract.from_document` 会一条条
    拒掉并逐行打"装不成记录"的警告。而 `scripts/` 下有三个夹具脚本正是
    `for name in OpsStore.BODY_FILES:` 地遍历它去入库的
    （`ingest_fixture` / `replay_fixture` / `judge_fixture`）——
    加一项进去，它们报出来的"装不成记录"会凭空多出问题的行数，
    而那个数字是多份 README 里写明的验收基准。
    """

    PRODUCT_FILES = (*BODY_FILES, "questions")
    """这个任务**全部产物文件**——`body_path()` 认的就是这几个名字。

    与 `BODY_FILES` 分开是刻意的：那个管"哪些行是 fact_content 的候选"，
    这个管"有哪些文件"。合成一个的话，"新增一份产物"和"新增一种正文"
    就变成同一个动作了（见 `BODY_FILES` 那段）。
    """

    def body_path(self, name: str = "contents") -> Path:
        """某一份产物文件的路径。

        ⚠️ 不认识的名字直接报错，**不能**当成合法路径拼出去：那样一个拼错的
        `"answer"`（少个 s）会悄悄造出第四份文件，而几份产物看起来都"跑通了"。
        """
        if name not in self.PRODUCT_FILES:
            raise ValueError(f"未知的产物文件 {name!r}，只能是 {self.PRODUCT_FILES}")
        return self.run_dir / f"{name}.jsonl"

    @property
    def contents_path(self) -> Path:
        """能力二那一份。保留这个名字，因为它已经是既有调用方的默认。"""
        return self.body_path("contents")

    def append_contents(self, rows: Iterator[dict] | list[dict], *, name: str = "contents") -> int:
        """追加一批正文，返回写入条数。一行一条、**写完即 flush**。

        ⚠️ 和 `append_urls` 是同一套写法，理由也一样：flush 必须在**循环里**。
        一轮采集要跑几十分钟，整批写完才 flush 的话，中途崩掉丢掉的是整批——
        而这个函数的全部意义就是"崩了也只丢最后一行"。

        ⚠️ 这里**不做去重**（去重是调用方的事，见 `__main__._ContentsDoc`，
        理由是那个 set 要跨 URL 累积，放在这一层反而要每次重读整份文件）。

        ⚠️ 逐行 `json.dumps`，所以传进来的 `dict` 里不能有**不可序列化**的值
        （`datetime` 之类）——`extract.to_document` / `question.to_row`
        已经把它们都转成字符串了。

        `name` 选哪一份：`"answers"` 是能力四的，`"contents"` 是能力二的，
        `"questions"` 是能力五的（它装的不是正文，见 `BODY_FILES`）。
        ⚠️ **只有前两份是"正文"**——遍历它们去入库的调用方要用 `BODY_FILES`，
        不是 `PRODUCT_FILES`。
        """
        count = 0
        with self.body_path(name).open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                count += 1
        return count

    def iter_contents(self, *, name: str = "contents") -> Iterator[dict]:
        """逐条读回落盘的正文。**空行和半截 JSON 跳过**（崩在最后一行会留下）。

        ⚠️ 名字的校验必须在**返回生成器之前**做完，所以这里先取 `body_path`、
        再把实际读文件的部分交给一个内部生成器。写成一个生成器函数的话，
        函数体要到**第一次迭代**才执行——拼错的名字要么推迟到那时才报错，
        要么（调用方提前跳出循环时）永远不报，然后安静地读到空。
        """
        return self._read_body(self.body_path(name))

    @staticmethod
    def _read_body(path: Path) -> Iterator[dict]:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row

    # ── 限流采样 ────────────────────────────────────────────────────

    @property
    def runs_path(self) -> Path:
        return self.root / "runs.jsonl"

    def append_sample(self, sample: RunSample) -> None:
        """追加一次采集的采样。⚠️ 永不删除、永不修改（3.7）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
            fh.flush()

    def recent_samples(self, keyword: str, limit: int = 20) -> list[RunSample]:
        """取某个关键词最近的采样，供 3.7 和历史均值比。

        小数据（一次采集一行），全量扫一遍即可——不值得为它上任何索引结构。
        """
        if not self.runs_path.exists():
            return []
        out: list[RunSample] = []
        with self.runs_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("keyword") == keyword:
                    out.append(RunSample(**row))
        return out[-limit:]

    # ── 关键词清单 ──────────────────────────────────────────────────

    @classmethod
    def load_keyword_file(cls, ops_dir: Path) -> list[str]:
        """读关键词清单，**不需要任务上下文**。

        关键词清单是整个任务目录共用的一个文件（不随 run 变），
        所以入口处想读它的时候手边往往没有 task.json。与其在调用方
        编一个假 run_id 出来，不如把这个动作收在这里——假 run_id 只出现在这一行。
        """
        return cls(ops_dir, TaskState(run_id="_", mode="backfill")).load_keywords()

    @property
    def keywords_path(self) -> Path:
        return self.root / "keywords.jsonl"

    def load_keywords(self) -> list[str]:
        """读关键词清单。人工维护的文件——不存在就返回空，不抛异常。

        ⚠️ 关键词清单也不入库：它反映的是"我们要监测什么"，敏感度和提示词
        同级（7.7）。所以它在 `runtime/` 下，跟着 `.gitignore` 一起不入库。
        """
        if not self.keywords_path.exists():
            return []
        words: list[str] = []
        with self.keywords_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                word = row.get("keyword") if isinstance(row, dict) else None
                if word:
                    words.append(str(word))
        return words

    # ── 并行切片（架构文档 3.8 末尾，决策 54）──────────────────────────
    #
    # 每个能力的输入都是一份**不重叠的清单**：能力一=关键词、能力四=问题清单、
    # 能力二=URL 列表。所以并行不需要队列、不需要抢任务——把清单切成 n 段，
    # n 个人各跑各的，往同一个产物文件追加写（三份产物都只增不改，所以不用锁）。

    @property
    def shards_dir(self) -> Path:
        return self.run_dir / "shards"

    def shard_path(self, who: str) -> Path:
        return self.shards_dir / f"{who}.json"

    def save_shard(self, who: str, items: Sequence[str]) -> Path:
        """把"这个人领到的那一段"落盘。

        ⚠️ **必须落盘，不能只在内存里切。** 中途换人、或者某人崩了要重跑时，
        没有这份文件就说不清"我上次领到的是哪一段"——两段会重叠（白采）
        或者漏掉一段（静默缺数据，而且事后看不出来）。
        """
        self.shards_dir.mkdir(parents=True, exist_ok=True)
        path = self.shard_path(who)
        path.write_text(
            json.dumps({"who": who, "items": list(items)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_shard(self, who: str) -> list[str]:
        """读回某人那一段。文件不在就返回空——"还没切"和"切出来是空的"同义。"""
        path = self.shard_path(who)
        if not path.exists():
            return []
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        items = row.get("items") if isinstance(row, dict) else None
        return [str(x) for x in items] if isinstance(items, list) else []


def shard(items: Sequence[str], *, index: int, count: int) -> list[str]:
    """把一份清单切成 `count` 段，返回第 `index` 段（0-based）。

    **按位置轮流分（`i % count`），不是顺序切两半。** 顺序切的话热门关键词、
    热门问题会整批落在同一个人头上，两边的**工作量根本不对等**——而那个人的
    耗时就是整轮的耗时的下限，等于没并行。

    轮流分不需要知道每条有多重，却天然把"位置相邻的"（往往也更相关）
    摊到不同人头上。

    ⚠️ 参数不合法时**报错而不是猜**。`index >= count` 时默默返回空列表的话，
    那个人会安静地跑完一个空任务、报"采集完成 0 条"——看起来像跑通了。
    """
    if count < 1:
        raise ValueError(f"切片数必须 ≥ 1，收到 {count}")
    if not 0 <= index < count:
        raise ValueError(f"切片序号必须在 [0, {count}) 内，收到 {index}")
    return [item for i, item in enumerate(items) if i % count == index]


def _timestamp_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
