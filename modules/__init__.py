"""五个业务模块。

每个模块自带 `__main__.py`，可以单独运行（架构文档 8.4）；
每个模块的测试在各自的 `tests/` 下，用 `core.repo.fake.FakeRepo` 跑，
不碰网络、不连数据库（8.5）。

⚠️ 模块之间禁止互相 import，只允许依赖 `core/`——由 tests/test_layering.py 强制。
"""
