# -*- coding: utf-8 -*-
"""系统托盘（可选功能）。

* 使用 ``pystray``（Windows 下基于 Shell_NotifyIcon，仅内存态，不写注册表/AppData）。
* 图标用 Pillow **在内存里绘制**，不落地任何图片文件。
* 托盘仅存在于本进程运行期间：程序彻底退出时调用 :meth:`TrayIcon.stop`
  → ``Shell_NotifyIcon(NIM_DELETE)``，托盘图标立即消失。
* 如果环境里没有 pystray / Pillow，或者托盘后端在本机不可用，
  本模块会自动降级为“无托盘”，主界面依然提供完整的「彻底退出」按钮，功能不受影响。

实现上有三个坑，这里都做了处理（否则会“看似成功、实际没有图标”）：

1. **pystray 不会自动显示图标。** ``Icon.run()`` 只跑消息循环，
   必须在 ``setup`` 回调里显式执行 ``icon.visible = True``，图标才会出现在通知区域。
2. **``ChangeWindowMessageFilterEx`` 在受限会话下会抛 ``WinError 5``**，
   而 pystray 把它放在 ``_create_window`` 里且 ``errcheck`` 会直接抛异常，
   导致托盘线程当场崩溃。该调用只影响“资源管理器重启后自动重建图标”，
   失败并不影响图标本身，因此这里把它降级为“忽略错误”。
3. **启动后必须验证图标真的可见**，否则“关闭主窗口 → 最小化到托盘”
   会把窗口藏到一个点不到的地方。:meth:`TrayIcon.start` 会等待并校验，
   失败就返回 ``False``，由上层按“无托盘”模式处理。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .logsetup import get_logger

__all__ = ["TrayIcon", "TRAY_AVAILABLE"]

log = get_logger("tray")

try:  # pragma: no cover - 取决于环境
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except Exception:  # noqa: BLE001
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    TRAY_AVAILABLE = False

#: 等待图标出现在通知区域的最长时间（秒）
_VISIBLE_TIMEOUT = 3.0


def make_icon_image(size: int = 64) -> Any:
    """在内存里画一个图标：深蓝圆角方块 + 白色“存档盘”。"""
    if not TRAY_AVAILABLE:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size // 6,
                        fill=(28, 62, 112, 255))
    # 存档盘外框
    m = size // 4
    d.rectangle([m, m, size - m, size - m], outline=(240, 246, 255, 255), width=max(2, size // 22))
    # 中间的“磁盘标签”
    d.rectangle([m + size // 12, m + size // 12, size - m - size // 12, size // 2 - size // 16],
                fill=(240, 246, 255, 255))
    d.rectangle([m + size // 8, size // 2 + size // 16, size - m - size // 8, size - m - size // 12],
                fill=(140, 190, 240, 255))
    return img


def apply_pystray_compat_patch() -> bool:
    """把 pystray 里会抛 WinError 5 的调用降级为“忽略错误”。"""
    if not TRAY_AVAILABLE:
        return False
    try:
        from pystray._util import win32 as _w32  # type: ignore[import-not-found]

        original = _w32.ChangeWindowMessageFilterEx
        if getattr(original, "_galguard_patched", False):
            return True

        def _safe(*args: Any, **kwargs: Any) -> Any:
            try:
                return original(*args, **kwargs)
            except OSError:
                # WinError 5 / 拒绝访问：只是无法在资源管理器重启后自动重建图标，
                # 不影响图标显示与菜单，忽略即可。
                log.debug("ChangeWindowMessageFilterEx 被系统拒绝，已忽略")
                return 0

        _safe._galguard_patched = True  # type: ignore[attr-defined]
        _w32.ChangeWindowMessageFilterEx = _safe
        log.debug("已应用 pystray 兼容补丁")
        return True
    except Exception:  # noqa: BLE001
        log.debug("应用 pystray 兼容补丁失败", exc_info=True)
        return False


class TrayIcon:
    """托盘图标封装。

    所有回调都在 pystray 自己的线程里执行，因此回调内部必须把真正的界面操作
    投递给 Tk 主线程（由 ``ui_main.MainWindow.submit`` 完成）。
    """

    def __init__(
        self,
        title: str,
        on_show: Callable[[], None],
        on_hide: Callable[[], None],
        on_backup_all: Callable[[], None],
        on_toggle_monitor: Callable[[], None],
        on_open_data_root: Callable[[], None],
        on_quit: Callable[[], None],
        is_monitoring: Callable[[], bool],
        is_paused: Callable[[], bool],
        status_text: Callable[[], str],
    ) -> None:
        self.title = title
        self.on_show = on_show
        self.on_hide = on_hide
        self.on_backup_all = on_backup_all
        self.on_toggle_monitor = on_toggle_monitor
        self.on_open_data_root = on_open_data_root
        self.on_quit = on_quit
        self.is_monitoring = is_monitoring
        self.is_paused = is_paused
        self.status_text = status_text

        self._icon: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._last_tooltip = title
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """本环境是否具备托盘能力（取决于是否装了 pystray/Pillow）。"""
        return TRAY_AVAILABLE

    @property
    def running(self) -> bool:
        """托盘是否**真的**在运行（图标已显示且线程还活着）。"""
        return bool(
            self._started
            and self._icon is not None
            and self._thread is not None
            and self._thread.is_alive()
            and getattr(self._icon, "visible", False)
        )

    @property
    def icon_visible(self) -> bool:
        return bool(self._icon is not None and getattr(self._icon, "visible", False))

    # ------------------------------------------------------------------
    # 菜单
    # ------------------------------------------------------------------

    def _toggle_label(self, item: Any = None) -> str:
        try:
            if not self.is_monitoring():
                return "开始静默监控"
            return "暂停监控" if not self.is_paused() else "恢复监控"
        except Exception:  # noqa: BLE001
            return "监控"

    def _build_menu(self) -> Any:
        return pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda: self._safe(self.on_show), default=True),
            pystray.MenuItem("隐藏主窗口", lambda: self._safe(self.on_hide)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("备份所有有更新的游戏", lambda: self._safe(self.on_backup_all)),
            pystray.MenuItem(self._toggle_label, lambda: self._safe(self.on_toggle_monitor)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开数据根目录", lambda: self._safe(self.on_open_data_root)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("彻底退出（结束进程）", lambda: self._safe(self.on_quit)),
        )

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("托盘回调执行失败")

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    def _setup(self, icon: Any) -> None:
        """pystray 在消息循环起来后调用本回调（在它自己的线程里）。

        ★ 必须在这里设置 ``visible = True``，否则图标永远不会显示。
        """
        try:
            icon.visible = True
            self._ready.set()
        except Exception:  # noqa: BLE001
            log.exception("显示托盘图标失败")

    def start(self) -> bool:
        """启动托盘。返回是否**真的**成功（失败时主界面功能完全不受影响）。"""
        if not TRAY_AVAILABLE:
            log.info("未安装 pystray/Pillow，托盘功能不可用（程序其余功能正常）")
            return False
        if self._started:
            return True

        apply_pystray_compat_patch()

        try:
            image = make_icon_image()
            self._icon = pystray.Icon(
                "galguard",
                icon=image,
                title=self.title,
                menu=self._build_menu(),
            )
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._icon.run,
                kwargs={"setup": self._setup},
                name="galguard-tray",
                daemon=True,
            )
            self._thread.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("创建托盘图标失败：%s", exc)
            self._reset()
            return False

        # 校验：线程必须活着，并且图标必须真的变可见
        deadline = time.time() + _VISIBLE_TIMEOUT
        while time.time() < deadline:
            if not self._thread.is_alive():
                break
            if self.icon_visible:
                break
            time.sleep(0.05)

        if not self._thread.is_alive():
            log.warning("托盘线程启动后立即退出，本环境下无法使用系统托盘")
            self._reset()
            return False
        if not self.icon_visible:
            log.warning("托盘图标未能在 %.1f 秒内显示，按“无托盘”模式运行", _VISIBLE_TIMEOUT)
            self.stop()
            return False

        self._started = True
        log.info("系统托盘图标已创建并显示")
        return True

    def _reset(self) -> None:
        self._icon = None
        self._thread = None
        self._started = False
        self._ready.clear()

    def stop(self) -> None:
        """移除托盘图标（彻底退出时调用，图标会立刻从任务栏消失）。"""
        icon = self._icon
        thread = self._thread
        self._started = False

        if icon is not None:
            try:
                icon.visible = False       # → Shell_NotifyIcon(NIM_DELETE)
            except Exception:  # noqa: BLE001
                pass
            try:
                icon.stop()                # 结束消息循环
            except Exception:  # noqa: BLE001
                log.debug("停止托盘图标时出错", exc_info=True)

        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        self._reset()
        log.info("系统托盘图标已移除")

    # ------------------------------------------------------------------
    # 提示与气泡
    # ------------------------------------------------------------------

    def set_tooltip(self, text: str) -> None:
        """更新鼠标悬停提示（Windows 托盘 tooltip 上限 127 字符）。"""
        self._last_tooltip = text[:120]
        icon = self._icon
        if icon is None or not self.icon_visible:
            return
        try:
            icon.title = self._last_tooltip
        except Exception:  # noqa: BLE001
            pass

    def notify(self, message: str, title: str = "") -> None:
        """弹一个气泡提示（仅在用户开启“备份完成提示”时调用）。"""
        icon = self._icon
        if icon is None or not self.icon_visible:
            return
        try:
            icon.notify(message, title or self.title)
        except Exception:  # noqa: BLE001
            log.debug("托盘气泡提示失败", exc_info=True)
