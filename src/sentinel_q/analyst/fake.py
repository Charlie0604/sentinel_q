"""不联网的模型客户端，供离线测试用。

放在包内（而不是 `tests/` 下）是照 `storage/fake.py` 的样子来的：那边 `FakeRepo`
也是既给测试当替身、又能给 `main ingest --dry-run` 用。这边同理——将来
`analyst run --dry-run` 要的正是它。

**它是这个模块"能独立测试"这句话的落点。** 架构文档 7.5 说 AI 模块在没有数据库、
没有网络的环境里也能完整跑起来；光有延迟 import 还不够，还得有个东西**替模型答话**，
否则测"答复解析"就得真花钱问一遍。所以：

    client = FakeLLMClient(['{"is_relevant": true, ...}'])
    judge_content.judge_one(pending, bundle=b, client=client)
    assert client.calls[0][0] == b.assemble("content")   # 喂进去的 system 是什么

⚠️ 它**不做任何校验**：解析、枚举、缺字段那些判断全在 `client.parse_reply` 和
`judge_*.py` 里，这个类只负责"把预设的那段字符串原样吐回来"。测试要验的
正是那条路径，替身自己先过滤一遍就把被测对象挡住了。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from sentinel_q.shared.models import PROMPT_MODULE_ORDER, PromptBundle


@dataclass
class FakeLLMClient:
    """喂什么答什么，顺便把收到的 prompt 记下来。

    `replies` 传一个列表时**按到达顺序发**（每条请求取一个，取完为止，之后重复最后一个）；
    传一个字符串时每条请求都返回它。列表里可以混 `Exception` 实例——
    轮到它时抛出去，用来测"一条失败不拖垮一批"。

    ⚠️ **按到达顺序 = 并发下说不准谁拿哪个。** 列表只在串行（`concurrency=1`）
    或"每条都返回同一个东西"时才可靠。要按内容分别答复，就照
    `test_judge_content.py` 里那个 `_PickyClient` 自己写一个——
    否则写出来的测试会偶尔红一次，那种最难查。

    ⚠️ 加锁是因为 `batch.judge_many` 会**多线程**调它。没有锁的话
    `calls` 的追加和 `replies` 的取用都会在并发下出错，
    而那种错是间歇性的——同样的代码跑十遍红一遍。
    """

    replies: list[str | Exception] | str = '{"is_relevant": true}'
    model: str = "fake-model"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.replies, str):
            self.replies = [self.replies]
        self._lock = threading.Lock()
        self._next = 0

    def complete(self, *, system: str, user: str) -> str:
        with self._lock:
            self.calls.append((system, user))
            index = min(self._next, len(self.replies) - 1)
            self._next += 1
            reply = self.replies[index]

        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def systems(self) -> list[str]:
        """所有 system prompt，按送达顺序。断言"拼了哪几块"用这个。"""
        return [system for system, _ in self.calls]

    @property
    def users(self) -> list[str]:
        return [user for _, user in self.calls]


def full_bundle(*, content_hash: str = "hash-fake", **modules: str) -> PromptBundle:
    """一份十五个模块全填满的 bundle，每块的内容就是 `<模块名>`。

    测试要断言"任务 B 的 system 里没有 `c_tasks`"，就得让每块的内容**认得出来**。
    用模块名当内容，`"<c_tasks>" in system` 一眼就能看出来，比数下标可靠。
    """
    filled = {name: f"<{name}>" for name in PROMPT_MODULE_ORDER}
    filled.update(modules)
    return PromptBundle(version=1, content_hash=content_hash, modules=filled)
