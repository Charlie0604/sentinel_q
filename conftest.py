"""放在仓库根目录，把 `src/` 放进 sys.path。

这样 `sentinel_q.*` 在测试里可以直接 import，不必先 `pip install -e .`——
克隆下来就能跑测试。（正式安装仍然推荐 `pip install -e ".[dev]"`，见 README。）
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
