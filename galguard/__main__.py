# -*- coding: utf-8 -*-
"""``python -m galguard`` 入口。"""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
