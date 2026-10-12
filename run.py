#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Galgame 存档守护（Galgame Save Guard）启动入口。

零污染设计要点（本文件是最后一道防线）：

1. 在导入任何其它模块之前就把 ``sys.dont_write_bytecode`` 置为 True，
   保证源码运行时不会在源码目录里散落 ``__pycache__``。
2. 本文件本身不创建、不修改、不删除任何文件。
   所有写操作都由 ``galguard.paths.PathGuard`` 在“数据根目录 / 备份根目录”内完成。
3. 推荐运行方式::

       python -B run.py

   ``-B`` 与 ``sys.dont_write_bytecode`` 双保险。

用法::

    python -B run.py
    python -B run.py --data-root "D:\\GalgameGuard"
    python -B run.py --console            # 额外把日志打到控制台
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 必须在导入 galguard 之前执行
# ---------------------------------------------------------------------------
sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# 允许直接在源码目录下运行（例如双击 run.py 时当前目录不是源码目录）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from galguard.app import main  # noqa: E402  （必须在环境设置之后导入）

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
