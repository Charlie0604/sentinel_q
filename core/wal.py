"""本地追加写日志（架构文档 8.8）。

不是台账，是缓冲。判据来自那条不变量——**任何内容在被送去问 AI 之前，
必须已经落在 Supabase 里**——所以本地文件永远可以安全删除，
最坏代价是重爬一段（廉价），而"重问一次 AI"昂贵。

用 JSONL（一行一条、append-only）的理由：崩溃时最多丢最后一行，
不会像"读出整个 JSON、改完再写回"那样把已有内容一起毁掉。
每条记录落进 Supabase 之后，对应行即失去意义，整个文件随时可以删。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Wal:
    """一个 WAL 文件。文件名自带用途与时间戳，避免多次运行互相覆盖。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def open(cls, directory: Path, purpose: str) -> Wal:
        """按用途建一个带时间戳的 WAL，例如 `collect-20260926-1030.jsonl`。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        return cls(directory / f"{purpose}-{stamp}.jsonl")

    def append(self, record: dict[str, Any]) -> None:
        """追加一条。每写一行 flush 一次——这正是它存在的意义。"""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    def replay(self) -> Iterator[dict[str, Any]]:
        """顺序读回，供崩溃后重放。坏行（写了一半）直接跳过。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
