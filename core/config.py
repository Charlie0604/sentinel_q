"""配置与路径解析。

有三类目录不入 git（见 .gitignore 与架构文档 8.7），新克隆的仓库里是空的，
所以这里负责按需创建——否则"刚 clone 就跑不起来"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    """仓库内的目录布局。"""

    root: Path
    prompts: Path  # 提示词工作区：人工编辑，改完 push 到线上库
    prompts_cache: Path  # 运行快照：任务开始时从库拉取，只读，随时可删
    runtime: Path
    wal: Path
    logs: Path
    tmp: Path
    secrets: Path

    @classmethod
    def resolve(cls, root: str | os.PathLike[str] | None = None) -> Paths:
        root = Path(root or os.getenv("SENTINEL_Q_ROOT") or REPO_ROOT).resolve()
        runtime = root / "runtime"
        return cls(
            root=root,
            prompts=root / "prompts",
            prompts_cache=runtime / "prompts",
            runtime=runtime,
            wal=runtime / "wal",
            logs=runtime / "logs",
            tmp=runtime / "tmp",
            secrets=root / "secrets",
        )

    def ensure(self) -> None:
        """创建所有可变目录。

        `prompts/` 只建目录、不塞内容——它的初始内容来自仓库里入库的 *.example 模板。
        """
        for directory in (
            self.prompts,
            self.prompts_cache,
            self.wal,
            self.logs,
            self.tmp,
            self.secrets,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    """运行期配置，全部来自环境变量。模板见仓库根目录 .env.example。"""

    supabase_dsn: str | None  # Postgres 连接串，不是 REST URL——见 core/repo/supabase.py 开头
    llm_api_key: str | None
    llm_model: str
    batch_size: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            supabase_dsn=os.getenv("SUPABASE_DSN"),
            llm_api_key=os.getenv("LLM_API_KEY"),
            llm_model=os.getenv("LLM_MODEL", "claude-sonnet-5"),
            batch_size=int(os.getenv("AI_BATCH_SIZE", "8")),
        )


def paths() -> Paths:
    """取当前生效的目录布局，并确保目录存在。"""
    resolved = Paths.resolve()
    resolved.ensure()
    return resolved
