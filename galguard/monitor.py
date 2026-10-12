# -*- coding: utf-8 -*-
"""静默文件监控。

设计原则：

* **只监控存档目录**，不监控游戏进程、不 hook、不注入、不读游戏内存。
* 有 ``watchdog`` 时使用 Windows ``ReadDirectoryChangesW``（内核事件，开销极低）；
  没有 ``watchdog`` 时自动降级为**轮询**（纯标准库，零依赖）。
* 防抖：同一游戏在 ``debounce_seconds`` 秒内发生的多次变化只触发一次备份。
* 暂停 / 恢复 / 停止都是线程安全的，停止后不会残留任何监控线程。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .fsutil import walk_files
from .logsetup import get_logger
from .models import GameEntry
from .paths import is_protected, normalize, path_key
from .rules import RuleSet

__all__ = ["SaveMonitor", "WATCHDOG_AVAILABLE"]

log = get_logger("monitor")

try:  # pragma: no cover - 取决于环境
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except Exception:  # noqa: BLE001
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False


class SaveMonitor:
    """监控所有已确认游戏的存档目录。"""

    def __init__(
        self,
        rules: RuleSet,
        on_change: Callable[[str], None],
        debounce_seconds: int = 5,
        poll_interval_seconds: int = 5,
        backend: str = "auto",
    ) -> None:
        self.rules = rules
        self.on_change = on_change
        self.debounce_seconds = max(0, int(debounce_seconds))
        self.poll_interval_seconds = max(2, int(poll_interval_seconds))
        self.backend_pref = backend

        self._lock = threading.RLock()
        self._games: List[GameEntry] = []
        self._timers: Dict[str, threading.Timer] = {}
        self._observer: Any = None
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._snapshot: Dict[str, Dict[str, Tuple[int, int]]] = {}
        self._running = False
        self._paused = False
        self._active_backend = "none"
        self._watched_paths: List[str] = []

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def backend(self) -> str:
        return self._active_backend

    @property
    def watched_paths(self) -> List[str]:
        with self._lock:
            return list(self._watched_paths)

    def status_text(self) -> str:
        if not self._running:
            return "已停止"
        if self._paused:
            return "已暂停"
        name = "watchdog 内核事件" if self._active_backend == "watchdog" else "轮询"
        return f"监控中（{name}，{len(self._watched_paths)} 个目录）"

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    def start(self, games: Sequence[GameEntry]) -> None:
        """开始监控给定游戏列表。重复调用会先停止再重启。"""
        self.stop()
        with self._lock:
            self._games = [g for g in games if g.enabled and g.monitor]
            self._running = True
            self._paused = False

        use_watchdog = WATCHDOG_AVAILABLE and self.backend_pref in ("auto", "watchdog")
        if use_watchdog:
            ok = self._start_watchdog()
            if ok:
                self._active_backend = "watchdog"
                return
            log.warning("watchdog 启动失败，降级为轮询监控")
        self._start_polling()
        self._active_backend = "polling"

    def stop(self) -> None:
        """停止全部监控线程与定时器（“彻底退出”时调用）。"""
        with self._lock:
            for timer in self._timers.values():
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._timers.clear()

            if self._observer is not None:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=3.0)
                except Exception:
                    log.debug("停止 watchdog observer 时出错", exc_info=True)
                self._observer = None

            self._poll_stop.set()
            thread = self._poll_thread
            self._poll_thread = None

            self._running = False
            self._active_backend = "none"
            self._watched_paths = []

        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._poll_stop = threading.Event()
        log.info("文件监控已停止")

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        log.info("文件监控已暂停")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        log.info("文件监控已恢复")

    def toggle_pause(self) -> bool:
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def set_games(self, games: Sequence[GameEntry]) -> None:
        """游戏列表变化后重载监控目标。"""
        was_running = self._running
        was_paused = self._paused
        self.start(games)
        if was_running and was_paused:
            self.pause()

    # ------------------------------------------------------------------
    # watchdog 后端
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> bool:
        try:
            observer = Observer()
            handler_factory = _make_handler(self)
            count = 0
            for game in self._games:
                for sp in game.save_paths_norm():
                    p = normalize(sp)
                    if not p.is_dir():
                        log.debug("存档目录不存在，暂不监控：%s", p)
                        continue
                    if is_protected(p):
                        # 监控自己的备份目录 = 备份写入触发监控再备份，死循环
                        log.warning("存档目录位于程序自己的数据/备份目录内，不监控：%s", p)
                        continue
                    try:
                        observer.schedule(handler_factory(game.id), str(p), recursive=True)
                        with self._lock:
                            self._watched_paths.append(str(p))
                        count += 1
                    except OSError as exc:
                        log.warning("无法监控 %s：%s", p, exc)
            observer.daemon = True
            observer.start()
            self._observer = observer
            log.info("watchdog 监控已启动，共 %d 个目录", count)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("watchdog 后端不可用：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 轮询后端
    # ------------------------------------------------------------------

    def _start_polling(self) -> None:
        self._poll_stop = threading.Event()
        self._snapshot = self._take_snapshot()
        with self._lock:
            self._watched_paths = [
                sp for g in self._games for sp in g.save_paths_norm() if normalize(sp).is_dir()
            ]
        t = threading.Thread(target=self._poll_loop, name="galguard-poll", daemon=True)
        self._poll_thread = t
        t.start()
        log.info("轮询监控已启动（间隔 %d 秒，%d 个目录）",
                 self.poll_interval_seconds, len(self._watched_paths))

    def _take_snapshot(self) -> Dict[str, Dict[str, Tuple[int, int]]]:
        snap: Dict[str, Dict[str, Tuple[int, int]]] = {}
        for game in self._games:
            files: Dict[str, Tuple[int, int]] = {}
            for sp in game.save_paths_norm():
                p = normalize(sp)
                if not p.is_dir() or is_protected(p):
                    continue
                for abs_p, _rel in walk_files(p):
                    try:
                        st = abs_p.stat()
                    except OSError:
                        continue
                    if self.rules.is_temp_file(abs_p.name):
                        continue
                    files[path_key(abs_p)] = (int(st.st_size), int(st.st_mtime))
            if files:
                snap[game.id] = files
        return snap

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(self.poll_interval_seconds):
            if self._stopping_now():
                return
            try:
                new_snap = self._take_snapshot()
            except Exception:  # noqa: BLE001
                log.exception("轮询扫描失败")
                continue
            with self._lock:
                if self._paused:
                    self._snapshot = new_snap
                    continue
                old_snap = self._snapshot
                self._snapshot = new_snap
            for game_id, files in new_snap.items():
                old = old_snap.get(game_id, {})
                if old != files:
                    log.debug("轮询检测到变化：%s", game_id)
                    self._schedule(game_id)

    def _stopping_now(self) -> bool:
        return self._poll_stop.is_set()

    # ------------------------------------------------------------------
    # 防抖
    # ------------------------------------------------------------------

    def _schedule(self, game_id: str) -> None:
        """收到变化事件，安排一次防抖后的备份触发。"""
        with self._lock:
            if not self._running or self._paused:
                return
            old = self._timers.pop(game_id, None)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass
            delay = self.debounce_seconds
            timer = threading.Timer(delay, self._fire, args=(game_id,))
            timer.name = f"galguard-debounce-{game_id}"
            timer.daemon = True
            self._timers[game_id] = timer
            timer.start()

    def _fire(self, game_id: str) -> None:
        with self._lock:
            self._timers.pop(game_id, None)
            if not self._running or self._paused:
                return
        log.info("存档变化触发自动备份：%s", game_id)
        try:
            self.on_change(game_id)
        except Exception:  # noqa: BLE001
            log.exception("自动备份回调失败：%s", game_id)


# ---------------------------------------------------------------------------
# watchdog 事件处理
# ---------------------------------------------------------------------------

_IGNORED_EVENT_TYPES = {"opened", "closed", "closed_no_write"}


def _make_handler(owner: SaveMonitor) -> Callable[[str], Any]:
    """为每个游戏生成一个事件处理器。"""

    class _Handler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
        def __init__(self, game_id: str) -> None:
            super().__init__()
            self.game_id = game_id

        def on_any_event(self, event) -> None:  # noqa: ANN001
            try:
                if getattr(event, "event_type", "") in _IGNORED_EVENT_TYPES:
                    return
                if getattr(event, "is_directory", False) and getattr(event, "event_type", "") == "modified":
                    return
                src = getattr(event, "src_path", "") or ""
                name = os.path.basename(src)
                if name and owner.rules.is_temp_file(name):
                    return
                owner._schedule(self.game_id)
            except Exception:  # noqa: BLE001
                log.debug("处理文件事件失败", exc_info=True)

    def factory(game_id: str) -> Any:
        return _Handler(game_id)

    return factory
