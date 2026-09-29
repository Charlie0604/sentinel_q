"""配置与路径解析。

有三类目录不入 git（见 .gitignore 与架构文档 7.7），新克隆的仓库里是空的，
所以这里负责按需创建——否则"刚 clone 就跑不起来"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _find_repo_root() -> Path:
    """向上找 `pyproject.toml` 定位仓库根。

    ⚠️ **不能用 `Path(__file__).parent.parent`**：代码在 `src/sentinel_q/shared/`
    下，往上数两级是 `src/sentinel_q`，`runtime/`、`prompts/`、`secrets/` 全部找不到。
    非 editable 安装时 `__file__` 落在 site-packages，同样找不到——所以再退一步，
    从当前工作目录往上找，让"在仓库里跑"这条路径也能成立。

    两者都找不到时返回 cwd：宁可让 `runtime/` 落在明显错误的地方，
    也不要静默挑一个看起来对的目录。
    """
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return Path.cwd().resolve()


REPO_ROOT = _find_repo_root()


def load_dotenv(path: Path | None = None) -> None:
    """把仓库根目录的 `.env` 读进环境变量。**已存在的变量不动。**

    `.env` 不入库（见 .gitignore），所以模板是入库的 `.env.example`——
    但这只在有人真的去读 `.env` 时才有意义。自己解析而不是加一个依赖：
    格式只有 `KEY=VALUE`，省掉一个包，也省掉"它什么时候加载"的疑问。

    值里的 `#` 不算注释（数据库密码里可能有）；只有整行以 `#` 开头才是注释。
    """
    path = path or REPO_ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not key or key.startswith("#"):
            continue
        # 真正来自 shell / CI 的环境变量优先，.env 只补空缺
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Paths:
    """仓库内的目录布局。"""

    root: Path
    prompts: Path  # 提示词工作区：人工编辑，改完 push 到线上库
    prompts_cache: Path  # 运行快照：任务开始时从库拉取，只读，随时可删
    runtime: Path
    ops: Path  # 运维账本：任务断点、URL 清单、限流采样、contents.jsonl（见 3.8）
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
            ops=runtime / "ops",
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
            self.ops,
            self.logs,
            self.tmp,
            self.secrets,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    """运行期配置，全部来自环境变量。模板见仓库根目录 .env.example。"""

    supabase_dsn: str | None  # Postgres 连接串，不是 REST URL——见 storage/supabase.py 开头
    llm_api_key: str | None
    llm_base_url: str | None  # OpenAI 兼容的接口根地址，如 https://api.deepseek.com
    llm_model: str
    llm_reasoning_effort: str
    """思考强度：`none` / `low` / `high` / `max`，**空串 = 不传这个字段、用服务端默认**。

    ⚠️ **这不是个调优项，是个花钱项。** DeepSeek 的 `deepseek-flash` 默认
    `reasoning_effort=high`——不传就是最高档，每判一条都在烧推理 token，
    而这是个分类任务。所以这里默认给 `none`（关掉思考）。

    ⚠️ **思考模式下 `temperature` 不生效**（服务端明确忽略），所以关掉思考还顺带
    让 `TEMPERATURE = 0` 那个"同一输入问两遍得到同一个答案"的保证真的成立。

    合法的旧别名：`minimal`→`low`、`medium`/`xhigh`→`high`。真正支持的取值以
    服务端为准（`GET /models` 会回 `effort.supported_levels`）；写错了是个 4xx，
    客户端**不重试**、当场把原始报文打出来，不会白烧配额。
    """

    batch_size: int

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            supabase_dsn=os.getenv("SUPABASE_DSN"),
            llm_api_key=os.getenv("LLM_API_KEY"),
            # ⚠️ **不给默认值**。模型名和地址是一对，猜一个地址只会让人对着 404
            #    查半天；缺了就在 analyst/client.py 的 from_settings 里当场报错。
            llm_base_url=os.getenv("LLM_BASE_URL"),
            # 默认值对着架构文档 4.3 写的工具（DeepSeek V4 Flash）。模型名和地址是一对，
            # 两边对不上时第一次真调 API 就是 400 model not found。
            llm_model=os.getenv("LLM_MODEL", "deepseek-flash"),
            llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "none"),
            batch_size=int(os.getenv("AI_BATCH_SIZE", "8")),
        )


def paths() -> Paths:
    """取当前生效的目录布局，并确保目录存在。"""
    resolved = Paths.resolve()
    resolved.ensure()
    return resolved
