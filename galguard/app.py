# -*- coding: utf-8 -*-
"""应用装配与生命周期。

这是理解“为什么彻底退出后没有残留”的关键文件：

启动顺序
    1. 关闭字节码写入 → 开启 DPI 感知 → 设置 AppUserModelID（都是进程内设置）
    2. 解析数据根目录：命令行 > 环境变量 > 指针文件 > 首次启动向导
    3. **在这之前绝不写任何文件**
    4. 解锁 :data:`galguard.paths.GUARD` → 建立目录结构 → 重定向临时目录
    5. 初始化日志 → 安装崩溃钩子 → 取单实例文件锁
    6. 加载配置 / 规则 / 数据库 → 装配服务层 → 主窗口 → 托盘
    7. 进入 Tk 主循环

退出顺序（``GalguardApp.quit``）
    1. 停止文件监控（watchdog observer / 轮询线程）
    2. 停止后台任务池（守护线程，最多等 4 秒，然后直接放弃）
    3. 关闭 SQLite（含 WAL checkpoint）
    4. 移除托盘图标（Shell_NotifyIcon NIM_DELETE）
    5. 销毁窗口、释放文件锁、清理 temp 目录
    6. 刷盘并关闭日志 → ``os._exit(0)``

最后一步用 ``os._exit`` 是刻意的：它跳过解释器的 atexit 等待逻辑，
保证即使有线程卡在慢速磁盘 IO 上，进程也会立刻结束、不会在任务管理器里残留。
因为所有工作线程都是守护线程，且资源已在前面显式释放，所以这样退出是安全的。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import APP_ID, APP_NAME, APP_VERSION, SCHEMA_VERSION
from . import bootstrap, logsetup
from .config import AppConfig, ConfigStore
from .db import HistoryDB
from .logsetup import get_logger
from .paths import GUARD, PathGuard, normalize
from .rules import RuleSet
from .service import AppService
from .ui_dialogs import FirstRunDialog, ask_yes_no
from .ui_main import MainWindow

__all__ = ["main", "GalguardApp"]

POINTER_FILENAME = "galguard.root.json"
ENV_DATA_ROOT = "GALGUARD_DATA_ROOT"
ENV_BACKUP_ROOT = "GALGUARD_BACKUP_ROOT"


# ---------------------------------------------------------------------------
# 启动器位置 / 指针文件
# ---------------------------------------------------------------------------

def launcher_dir() -> Path:
    """返回启动器所在目录（用于放置“记住路径”的指针文件）。"""
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    # 源码运行：galguard/app.py → 上一级即 run.py 所在目录
    return Path(__file__).resolve().parent.parent


def pointer_path() -> Path:
    return launcher_dir() / POINTER_FILENAME


def read_pointer() -> Dict[str, str]:
    p = pointer_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if isinstance(v, (str, int))}
    except (OSError, ValueError):
        pass
    return {}


def write_pointer(data_root: str, backup_root: str) -> bool:
    """写指针文件。

    这是**唯一**一处可能写到数据根目录之外的地方，并且：
      * 只有用户在首启向导里**明确勾选**了“记住这个位置”才会执行；
      * 文件只有两行路径，不含任何其它信息；
      * 不勾选就完全不写，程序每次启动都会询问数据位置。
    """
    payload = {
        "comment": "Galgame 存档守护 的数据位置指针。删除本文件后，程序下次启动会重新询问数据根目录。",
        "data_root": data_root,
        "backup_root": backup_root,
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(pointer_path(), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def remove_pointer() -> bool:
    try:
        pointer_path().unlink()
        return True
    except OSError:
        return False


def _say(text: str) -> None:
    """在无控制台（PyInstaller --windowed / pythonw）环境下也能给出提示。"""
    try:
        print(text)
        return
    except Exception:  # noqa: BLE001
        pass
    _message_box(text, f"{APP_NAME}", 0x40)   # MB_ICONINFORMATION


def _message_box(text: str, title: str, flags: int) -> None:
    """直接用 Win32 MessageBoxW 弹窗。

    刻意不依赖 tkinter：启动期的致命错误往往发生在 Tk 还没准备好、
    或者 GUI 相关代码本身就出错的时候，这时只有原生 MessageBox 才靠得住。
    """
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, str(text)[:4000], str(title)[:120], int(flags))
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        r = tk.Tk()
        r.withdraw()
        (messagebox.showerror if flags & 0x10 else messagebox.showinfo)(title, text)
        r.destroy()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="galgame-save-guard",
        description=f"{APP_NAME} v{APP_VERSION} —— 本地 Galgame/视觉小说存档扫描与快照备份",
    )
    ap.add_argument("--data-root", default="", help="总数据根目录（不填则读取环境变量/指针文件/弹向导）")
    ap.add_argument("--backup-root", default="", help="备份根目录（默认 <总数据根目录>\\backups）")
    ap.add_argument("--console", action="store_true", help="同时把日志输出到控制台")
    ap.add_argument("--no-tray", action="store_true", help="不创建系统托盘图标")
    ap.add_argument("--forget-pointer", action="store_true", help="删除“记住数据位置”的指针文件后退出")
    ap.add_argument("--version", action="store_true", help="显示版本号后退出")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

class GalguardApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.guard: PathGuard = GUARD
        self.config: Optional[AppConfig] = None
        self.store: Optional[ConfigStore] = None
        self.db: Optional[HistoryDB] = None
        self.rules: Optional[RuleSet] = None
        self.service: Optional[AppService] = None
        self.window: Optional[MainWindow] = None
        self.tray: Optional[Any] = None
        self.lock: Optional[bootstrap.SingleInstanceLock] = None
        self.first_run = False
        self._exited = False

    # ------------------------------------------------------------------

    def resolve_roots(self) -> Optional[Dict[str, Any]]:
        """确定数据根目录，必要时弹出首次启动向导。"""
        cli_data = (self.args.data_root or "").strip()
        cli_backup = (self.args.backup_root or "").strip()
        env_data = os.environ.get(ENV_DATA_ROOT, "").strip()
        env_backup = os.environ.get(ENV_BACKUP_ROOT, "").strip()
        ptr = read_pointer()

        data_root = cli_data or env_data or ptr.get("data_root", "")
        backup_root = cli_backup or env_backup or ptr.get("backup_root", "")

        if data_root:
            data_root = str(normalize(data_root))
            if not backup_root:
                backup_root = str(Path(data_root) / "backups")
            backup_root = str(normalize(backup_root))
            return {
                "data_root": data_root,
                "backup_root": backup_root,
                "library_roots": None,       # 表示沿用已有配置
                "remember": False,
                "compute_sha256": None,
                "compare_mtime": None,
            }

        # 首次启动（或指针丢失）：弹向导
        dlg = FirstRunDialog(pointer_path=pointer_path())
        return dlg.show()

    # ------------------------------------------------------------------

    def bootstrap_environment(self, roots: Dict[str, Any]) -> None:
        """用户已指定路径之后的初始化：建目录、重定向 temp、开日志、加锁。"""
        data_root = Path(roots["data_root"])
        backup_root = Path(roots["backup_root"])

        # 1) 解锁沙盒（此后才允许写文件）
        self.guard.set_data_root(data_root)
        self.guard.set_backup_root(backup_root)

        # 2) 建立标准目录结构（全部在数据根目录内）
        for name in ("logs", "cache", "temp", "data", "rules", "exports"):
            self.guard.ensure_dir(data_root / name)
        self.guard.ensure_dir(backup_root)

        # 3) 临时目录重定向（必须在任何库创建临时文件之前完成）
        bootstrap.redirect_temp_dir(data_root)

        # 4) 日志
        log = logsetup.setup_logging(
            data_root / "logs", console=bool(self.args.console), level=10
        )
        log.info("=" * 70)
        log.info("%s v%s 启动", APP_NAME, APP_VERSION)
        log.info("数据根目录：%s", data_root)
        log.info("备份根目录：%s", backup_root)
        log.info("临时目录已重定向到：%s", data_root / "temp")

        # 5) 崩溃钩子（此时才有地方可写）
        bootstrap.install_excepthooks(lambda: data_root / "logs" / "crash.log")

        # 6) 单实例锁
        self.lock = bootstrap.SingleInstanceLock(data_root / "cache" / "galguard.lock")
        if not self.lock.acquire():
            log.warning("已有另一个实例在运行")
            raise RuntimeError(
                "已经有一个「Galgame 存档守护」在运行了。\n"
                "请从系统托盘打开它，或先在任务管理器里结束旧实例。"
            )

    # ------------------------------------------------------------------

    def load_config(self, roots: Dict[str, Any]) -> AppConfig:
        assert self.store is not None
        existed = self.store.exists()
        cfg = self.store.load()
        self.first_run = not existed

        if not existed:
            # 首次运行：把向导里的选择写进配置
            cfg.backup_root = roots["backup_root"]
            if roots.get("library_roots"):
                cfg.library_roots = list(roots["library_roots"])
            if roots.get("compute_sha256") is not None:
                cfg.scan.compute_sha256 = bool(roots["compute_sha256"])
            if roots.get("compare_mtime") is not None:
                cfg.scan.compare_mtime = bool(roots["compare_mtime"])
            self.store.save(cfg)
            logsetup.get_logger("app").info("已创建配置文件：%s", self.store.path)
        else:
            # 备份根目录可能被指针文件/命令行改动
            if roots["backup_root"] and str(cfg.backup_root) != str(roots["backup_root"]):
                if roots.get("remember") or self.args.backup_root or os.environ.get(ENV_BACKUP_ROOT):
                    cfg.backup_root = roots["backup_root"]
                    self.guard.set_backup_root(cfg.backup_root)
                    self.store.save(cfg)
        return cfg

    # ------------------------------------------------------------------

    def setup(self, roots: Dict[str, Any]) -> None:
        self.bootstrap_environment(roots)
        self.store = ConfigStore(self.guard)
        self.config = self.load_config(roots)

        # 备份根目录写入白名单
        self.guard.set_backup_root(self.config.backup_root)
        self.guard.ensure_dir(self.guard.backup_root)

        # 指针文件（仅在用户明确勾选时）
        if roots.get("remember") and not self.args.data_root and not os.environ.get(ENV_DATA_ROOT):
            if write_pointer(str(self.guard.data_root), str(self.guard.backup_root)):
                logsetup.get_logger("app").info("已写入数据位置指针：%s", pointer_path())

        self.rules = RuleSet.load(self.guard)
        self.db = HistoryDB(self.guard)
        self.db.connect()

        # 可选的 Ludusavi 离线存档数据库（用户自己下载的 manifest.yaml）
        manifest = None
        mpath = (self.config.scan.manifest_path or "").strip()
        if mpath:
            from .manifest import LudusaviManifest

            mf = LudusaviManifest()
            if mf.load(Path(mpath)):
                manifest = mf
            else:
                logsetup.get_logger("app").warning(
                    "Ludusavi 存档数据库加载失败：%s", "；".join(mf.errors or ["未知错误"])
                )

        self.service = AppService(
            self.guard, self.store, self.config, self.db, self.rules, manifest
        )

        # 为游戏库根目录登记只读范围（信息性）
        for p in self.config.library_roots:
            self.guard.add_read_root(p)

    # ------------------------------------------------------------------

    def run(self) -> int:
        roots = self.resolve_roots()
        if not roots:
            # 用户没有选择数据根目录 → 提示并退出，此时程序没有写过任何文件
            _say(
                "未选择总数据根目录，程序退出。\n"
                "（零污染策略：在你指定数据位置之前，程序不会创建或写入任何文件。）"
            )
            return 0

        try:
            self.setup(roots)
        except RuntimeError as exc:
            # 单实例冲突等
            try:
                import tkinter as tk
                from tkinter import messagebox

                r = tk.Tk()
                r.withdraw()
                messagebox.showwarning(APP_NAME, str(exc))
                r.destroy()
            except Exception:  # noqa: BLE001
                print(str(exc), file=sys.stderr)
            if self.lock is not None:
                self.lock.release()
            return 1

        assert self.service is not None
        assert self.config is not None

        self.window = MainWindow(self.service, on_quit=self.quit)

        # 托盘（可选）
        if not self.args.no_tray:
            try:
                from .tray import TrayIcon

                self.tray = TrayIcon(
                    title=f"{APP_NAME} v{APP_VERSION}",
                    on_show=lambda: self.window.submit(self.window.show_window),
                    on_hide=lambda: self.window.submit(self.window.hide_window),
                    on_backup_all=lambda: self.window.submit(self.window.backup_updated),
                    on_toggle_monitor=lambda: self.window.submit(self.window.toggle_monitor),
                    on_open_data_root=lambda: self.window.submit(
                        lambda: self.service.open_in_explorer(self.service.data_root())
                    ),
                    on_quit=lambda: self.window.submit(self.window.quit_app),
                    is_monitoring=lambda: bool(self.service and self.service.monitor.is_running),
                    is_paused=lambda: bool(self.service and self.service.monitor.is_paused),
                    status_text=lambda: self.service.monitor.status_text() if self.service else "",
                )
                if not self.tray.start():
                    logsetup.get_logger("app").warning(
                        "系统托盘不可用（未安装 pystray/Pillow，或本环境不允许创建托盘图标）。"
                        "此时点窗口右上角 X 会询问你是否彻底退出，而不是把窗口藏到没有图标的地方。"
                    )
                self.window.tray = self.tray
            except Exception:  # noqa: BLE001
                logsetup.get_logger("app").exception("托盘初始化失败（不影响其它功能）")

        # 启动后的自动动作
        w = self.window
        if self.config.auto_start_monitor:
            w.after(900, self.service.start_monitoring)
        w.after(1200, self.service.ensure_baselines)
        if self.config.games:
            w.after(1800, lambda: self.service.refresh_updates(quiet=True))
        if self.first_run:
            w.after(600, lambda: self.service.emit_log(
                "首次启动：正在扫描游戏库以生成候选游戏列表…"))
            if self.config.library_roots or self.config.scan.detect_steam:
                w.after(1000, self.service.scan_candidates)
            else:
                w.after(1000, lambda: self.service.emit_log(
                    "尚未设置游戏库根目录。请点「设置 → 扫描」添加，或用「添加游戏」手动指定。", "warn"))

        try:
            w.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.quit(0)
        return 0

    # ------------------------------------------------------------------

    def quit(self, code: int = 0) -> None:
        """彻底退出：释放一切资源后结束进程。"""
        if self._exited:
            return
        self._exited = True
        log = logsetup.get_logger("app")
        log.info("用户请求彻底退出，开始清理…")

        try:
            if self.window is not None:
                self.window.var_status.set("正在退出：停止监控与后台任务…")
                self.window.update_idletasks()
        except Exception:
            pass

        # 1) 停止监控 + 后台任务 + 数据库
        try:
            if self.service is not None:
                self.service.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("停止服务层失败")

        # 2) 移除托盘图标（图标立刻从任务栏消失）
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:  # noqa: BLE001
            log.exception("移除托盘失败")

        # 3) 销毁窗口
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:  # noqa: BLE001
            pass

        # 4) 释放文件锁
        try:
            if self.lock is not None:
                self.lock.release()
        except Exception:  # noqa: BLE001
            log.exception("释放单实例锁失败")

        # 5) 清理临时目录
        try:
            bootstrap.cleanup_temp_dir()
        except Exception:  # noqa: BLE001
            pass

        log.info("清理完成，进程即将结束")
        # 6) 刷盘并关闭日志（os._exit 不会触发 atexit，所以要显式做）
        try:
            logsetup.shutdown_logging()
        except Exception:  # noqa: BLE001
            pass

        # 7) 立即结束进程：保证任务管理器中无残留、无挂起线程
        os._exit(int(code) if int(code) >= 0 else 0)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))

    # 最早的运行时设置：不写字节码
    bootstrap.enable_no_bytecode()

    if args.version:
        _say(f"{APP_NAME} v{APP_VERSION} (schema {SCHEMA_VERSION})")
        return 0

    if args.forget_pointer:
        ok = remove_pointer()
        _say(f"指针文件{'已删除' if ok else '不存在或删除失败'}：{pointer_path()}")
        return 0

    bootstrap.enable_dpi_awareness()
    bootstrap.set_app_user_model_id(APP_ID)

    app = GalguardApp(args)
    try:
        return app.run()
    except Exception:  # noqa: BLE001 - 最后一道兜底
        import traceback

        tb = traceback.format_exc()
        try:
            logsetup.get_logger("app").exception("致命错误")
            logsetup.shutdown_logging()
        except Exception:
            pass

        # 尽可能把 traceback 落到数据根目录（如果那时已经确定）
        try:
            if app.guard.unlocked:
                logs_dir = app.guard.data_root / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                with open(logs_dir / "crash.log", "a", encoding="utf-8") as fh:
                    fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} [启动失败] =====\n{tb}")
        except Exception:  # noqa: BLE001
            pass

        # 控制台可见就打到控制台
        try:
            if sys.stderr is not None:
                sys.stderr.write(tb)
                sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

        # ★ 关键：窗口模式下 stderr 是不可见的，必须用原生 MessageBox 把错误显示出来，
        #   否则用户看到的现象就是“双击没反应”。
        _message_box(
            "程序启动失败。\n\n"
            f"{tb}\n"
            "如果已经选定数据根目录，同样的信息也会写到：\n"
            "    <总数据根目录>\\logs\\crash.log",
            f"{APP_NAME} —— 启动失败",
            0x10,   # MB_ICONERROR
        )

        try:
            app.quit(1)
        except Exception:  # noqa: BLE001
            os._exit(1)
        return 1
