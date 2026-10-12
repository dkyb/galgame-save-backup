# -*- coding: utf-8 -*-
"""进程引导：字节码、临时目录重定向、DPI、单实例锁、崩溃日志。

这里集中处理所有“Python 运行时 / 第三方库可能偷偷往系统目录写东西”的风险点：

1. ``sys.dont_write_bytecode`` —— 不生成 ``__pycache__``。
2. ``tempfile.tempdir`` + ``TMP/TEMP/TMPDIR`` 环境变量 —— 把临时目录整体
   重定向到 ``<总数据根目录>\\temp``。任何遵循标准约定的库（含 Pillow、
   shutil、zipfile 等）都会把临时文件写到这里，而不是 ``%TEMP%``。
3. 崩溃日志 —— ``sys.excepthook`` / ``threading.excepthook`` 写入
   ``<总数据根目录>\\logs\\crash.log``，绝不写 Windows 事件日志。
4. 单实例锁 —— 用文件锁而不是命名互斥体句柄，锁文件同样在数据根目录内。
"""

from __future__ import annotations

import atexit
import ctypes
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "enable_no_bytecode",
    "enable_dpi_awareness",
    "set_app_user_model_id",
    "redirect_temp_dir",
    "install_excepthooks",
    "SingleInstanceLock",
]


# ---------------------------------------------------------------------------
# 运行时开关
# ---------------------------------------------------------------------------

def enable_no_bytecode() -> None:
    """禁用字节码写入（源码运行时不再散落 ``__pycache__``）。"""
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def enable_dpi_awareness() -> None:
    """开启 Per-Monitor DPI 感知，让 Win11 高分屏下界面不糊。

    只调用 Win32 API，不写注册表（``SetProcessDpiAwarenessContext`` 是纯进程内设置）。
    """
    if os.name != "nt":
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def set_app_user_model_id(app_id: str = "GalgameSaveGuard") -> None:
    """设置 AppUserModelID，让任务栏图标归组正确。

    注意：这是进程内 API，**不会**在注册表写入任何项。
    """
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 临时目录重定向
# ---------------------------------------------------------------------------

_temp_redirected_to: Optional[Path] = None


def redirect_temp_dir(data_root: Path, cleanup_on_exit: bool = True) -> Path:
    """把全部临时目录指向 ``<data_root>\\temp``。

    返回临时目录路径。
    """
    global _temp_redirected_to

    temp_dir = Path(data_root) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 1) 标准库
    tempfile.tempdir = str(temp_dir)

    # 2) 环境变量：影响子进程与部分第三方库
    for var in ("TMP", "TEMP", "TMPDIR"):
        os.environ[var] = str(temp_dir)

    # 3) 清掉标准库缓存的候选目录，确保 gettempdir() 立即返回新目录
    try:  # pragma: no cover - 内部结构，加保护
        tempfile._candidate_tempdir_list = lambda: [str(temp_dir)]  # type: ignore[attr-defined]
    except Exception:
        pass

    _temp_redirected_to = temp_dir

    if cleanup_on_exit:
        atexit.register(cleanup_temp_dir)

    return temp_dir


def cleanup_temp_dir() -> None:
    """清空重定向后的临时目录内容（只删内容，不删目录本身）。"""
    root = _temp_redirected_to
    if root is None or not root.is_dir():
        return
    for child in list(root.iterdir()):
        try:
            if child.is_dir():
                import shutil

                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 崩溃日志
# ---------------------------------------------------------------------------

def install_excepthooks(log_path_provider: Callable[[], Optional[Path]]) -> None:
    """安装全局异常钩子，把未捕获异常写入 ``logs/crash.log``。

    ``log_path_provider`` 返回崩溃日志的完整路径；返回 ``None`` 表示
    尚未确定数据根目录（此时只在 stderr 输出，绝不写文件）。
    """
    original_hook = sys.excepthook

    def _write(title: str, exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        block = f"\n===== {stamp} [{title}] =====\n{text}"
        path = None
        try:
            path = log_path_provider()
        except Exception:
            path = None
        if path is None:
            try:
                sys.stderr.write(block)
            except Exception:
                pass
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(block)
        except OSError:
            pass

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            original_hook(exc_type, exc_value, exc_tb)
            return
        _write("未捕获异常", exc_type, exc_value, exc_tb)
        original_hook(exc_type, exc_value, exc_tb)

    def _thread_hook(args: "threading.ExceptHookArgs"):
        if issubclass(args.exc_type, SystemExit):
            return
        _write(
            f"线程 {getattr(args.thread, 'name', '?')} 异常",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )

    sys.excepthook = _sys_hook
    try:
        threading.excepthook = _thread_hook  # type: ignore[assignment]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------

class SingleInstanceLock:
    """基于文件锁的单实例保护。

    * 锁文件位于 ``<data_root>\\cache\\galguard.lock``。
    * 使用 ``msvcrt.locking`` 做非阻塞排它锁；进程退出（含被强杀）时
      操作系统自动释放，不会残留“僵尸锁”。
    * **不是**命名互斥体，也**不是**系统范围内的常驻对象：进程没了锁就没了。
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - 仅 Windows 目标
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode("utf-8"))
            fh.flush()
        except OSError:
            pass

        self._fh = fh
        atexit.register(self.release)
        return True

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        # 锁文件本身留在数据根目录内，删除它不影响“零污染”承诺，但也没必要强删
        try:
            self.lock_path.unlink()
        except OSError:
            pass
