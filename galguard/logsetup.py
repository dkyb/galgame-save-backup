# -*- coding: utf-8 -*-
"""滚动日志。

日志只写 ``<总数据根目录>\\logs\\app.log``，使用 ``RotatingFileHandler``
自动滚动（默认 2 MB × 5 份）。控制台输出是可选的，默认关闭，避免在
无控制台环境（pythonw / PyInstaller --noconsole）下报错。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

__all__ = ["setup_logging", "get_logger", "shutdown_logging"]

_LOGGER_NAME = "galguard"
_configured = False
_log_dir: Optional[Path] = None


def setup_logging(
    log_dir: Path,
    level: int = logging.INFO,
    console: bool = False,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """初始化日志系统；重复调用只更新级别。"""
    global _configured, _log_dir

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)

    if not _configured:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        _log_dir = log_dir

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)-7s] [%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        fh = logging.handlers.RotatingFileHandler(
            str(log_dir / "app.log"),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

        if console and sys.stderr is not None:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            sh.setLevel(logging.INFO)
            logger.addHandler(sh)

        logger.propagate = False
        _configured = True

    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.handlers.RotatingFileHandler
        ):
            h.setLevel(logging.DEBUG if level <= logging.DEBUG else logging.INFO)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME + ("." + name if name else ""))


def shutdown_logging() -> None:
    """刷盘并关闭所有 handler（退出前必须调用，否则 os._exit 会丢日志）。"""
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        try:
            h.flush()
        except Exception:
            pass
    logging.shutdown()
