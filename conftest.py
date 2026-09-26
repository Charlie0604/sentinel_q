"""放在仓库根目录，让 pytest 把根目录放进 sys.path。

这样 `core` 和 `modules` 两个顶层包在测试里可以直接 import，
不需要 `pip install -e .`——克隆下来就能跑测试。
"""
