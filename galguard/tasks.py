# -*- coding: utf-8 -*-
"""后台任务池（守护线程实现）。

**刻意不使用 ``concurrent.futures.ThreadPoolExecutor``**：
它的工作线程是非守护线程，并注册了 ``threading._register_atexit``，
解释器退出时会一直等待线程结束 —— 这正是“彻底退出后仍有残留进程”的常见原因。
这里用纯守护线程 + 队列，保证：

* 主窗口关闭 / 用户点“彻底退出”时，``stop()`` 能立即让线程退出；
* 即使有任务卡在慢速磁盘 IO 上，进程也不会被拖住（daemon 线程随进程终结）。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .logsetup import get_logger

__all__ = ["Task", "DaemonPool"]

log = get_logger("tasks")


@dataclass(eq=False)   # eq=False → 用对象标识做哈希，便于放进 set 里跟踪“正在运行的任务”
class Task:
    name: str
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    on_done: Optional[Callable[[Any], None]] = None
    on_error: Optional[Callable[[BaseException], None]] = None
    cancel_check: Optional[Callable[[], bool]] = None

    done: bool = False
    ok: bool = False
    result: Any = None
    error: Optional[BaseException] = None
    started_at: float = 0.0
    finished_at: float = 0.0


class DaemonPool:
    def __init__(self, workers: int = 3, name: str = "galguard-worker") -> None:
        self.workers = max(1, int(workers))
        self.name = name
        self._q: "queue.Queue[Optional[Task]]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._running: set = set()
        self._started = False

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopping.clear()
            for i in range(self.workers):
                t = threading.Thread(
                    target=self._loop, name=f"{self.name}-{i + 1}", daemon=True
                )
                t.start()
                self._threads.append(t)
            log.debug("后台任务池已启动：%d 个工作线程", self.workers)

    def stop(self, drain: bool = True, timeout: float = 5.0) -> None:
        """停止任务池。

        ``drain=True`` 时会先给正在执行/排队的任务一点时间收尾（默认最多 5 秒），
        超时后直接放弃 —— 因为线程是守护线程，进程结束时它们会随之消失，
        不会留下任何残留进程。
        """
        if drain:
            deadline = time.time() + max(0.0, timeout)
            while time.time() < deadline:
                if self._q.empty() and not self._running:
                    break
                time.sleep(0.05)

        self._stopping.set()
        # 清空队列：丢弃尚未开始的任务（用户正在彻底退出，没必要再备份）
        dropped = 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                dropped += 1
            self._q.task_done()
        if dropped:
            log.info("退出时丢弃了 %d 个排队中的任务", dropped)

        for _ in self._threads:
            try:
                self._q.put_nowait(None)
            except queue.Full:  # pragma: no cover
                pass
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()
        with self._lock:
            self._started = False
        log.debug("后台任务池已停止")

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    # -- 提交 ---------------------------------------------------------------

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "",
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        **kwargs: Any,
    ) -> Optional[Task]:
        if self._stopping.is_set():
            log.debug("任务池正在停止，拒绝新任务：%s", name or getattr(fn, "__name__", "?"))
            return None
        self.start()
        task = Task(
            name=name or getattr(fn, "__name__", "task"),
            fn=fn,
            args=args,
            kwargs=kwargs,
            on_done=on_done,
            on_error=on_error,
        )
        self._q.put(task)
        return task

    def pending(self) -> int:
        return self._q.qsize() + len(self._running)

    # -- 内部 ---------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                task = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if task is None:
                self._q.task_done()
                return

            with self._lock:
                self._running.add(task)
            task.started_at = time.time()
            try:
                task.result = task.fn(*task.args, **task.kwargs)
                task.ok = True
            except BaseException as exc:  # noqa: BLE001 - 后台任务必须兜住一切异常
                task.error = exc
                task.ok = False
                log.error("后台任务「%s」失败：%s\n%s", task.name, exc, traceback.format_exc())
                if task.on_error is not None:
                    try:
                        task.on_error(exc)
                    except Exception:
                        log.exception("任务错误回调本身也失败了")
            finally:
                task.finished_at = time.time()
                task.done = True
                with self._lock:
                    self._running.discard(task)
                self._q.task_done()

            if task.ok and task.on_done is not None:
                try:
                    task.on_done(task.result)
                except Exception:
                    log.exception("任务完成回调失败：%s", task.name)
