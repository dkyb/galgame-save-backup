# -*- coding: utf-8 -*-
"""主窗口。

线程模型（很重要）：

* Tk 只能在其创建线程里使用。所有来自托盘线程 / 后台任务线程的操作，
  都先塞进 ``MainWindow._ui_queue``，由主线程的 ``_pump``（每 200ms）取出执行。
* 后台任务结果通过 ``service.events`` 队列回到这里，同样在 ``_pump`` 里消费。
* 因此界面永不阻塞，且不存在跨线程操作控件的问题。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from . import APP_NAME, WINDOW_TITLE
from .fsutil import human_bytes
from .logsetup import get_logger
from .models import BaselineDiff, Candidate, GameEntry
from .paths import normalize
from .rules import RULES_FILENAME, RULES_SUBDIR
from .service import AppService
from .ui_dialogs import CandidateDialog, ManualGameDialog, SettingsDialog, ask_yes_no

__all__ = ["MainWindow"]

log = get_logger("ui")

_MONITOR_STOPPED = "已停止"
_MONITOR_PAUSED = "已暂停"
_MONITOR_ON = "监控中"
_MONITOR_OFF = "不监控"


class MainWindow(tk.Tk):
    def __init__(self, service: AppService, on_quit: Callable[[int], None]) -> None:
        super().__init__()
        self.service = service
        self.on_quit = on_quit
        self.tray: Optional[Any] = None          # 由 GalguardApp 注入
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._dirty = True
        self._log_lines: List[str] = []
        #: 被勾选的游戏 id（左侧 ☑ 列）—— 支持任意多选
        self._checked: Set[str] = set()
        self._quitting = False
        self._notified_hidden = False

        self.title(WINDOW_TITLE)
        self.geometry("1180x720")
        self.minsize(940, 560)

        self._build_menu()
        self._build_toolbar()
        self._build_tree()
        self._build_log()
        self._build_status()

        self.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self.bind("<F5>", lambda e: self.refresh_updates())

        self.after(200, self._pump)

    # ==================================================================
    # 界面构建
    # ==================================================================

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        m_file = tk.Menu(menubar, tearoff=False)
        m_file.add_command(label="设置…", command=self.open_settings)
        m_file.add_command(label="刷新列表", command=self._mark_dirty)
        m_file.add_separator()
        m_file.add_command(label="打开数据根目录", command=lambda: self.service.open_in_explorer(self.service.data_root()))
        m_file.add_command(label="打开备份根目录", command=lambda: self.service.open_in_explorer(self.service.backup_root()))
        m_file.add_separator()
        m_file.add_command(label="彻底退出", command=self.quit_app)
        menubar.add_cascade(label="文件", menu=m_file)

        m_act = tk.Menu(menubar, tearoff=False)
        m_act.add_command(label="添加游戏…", command=self.add_game)
        m_act.add_command(label="编辑选中游戏…", command=self.edit_game)
        m_act.add_command(label="删除勾选/选中的游戏", command=self.delete_game)
        m_act.add_separator()
        m_act.add_command(label="全选", command=lambda: self.set_all_checked(True))
        m_act.add_command(label="全不选", command=lambda: self.set_all_checked(False))
        m_act.add_command(label="反选", command=self.invert_checked)
        m_act.add_separator()
        m_act.add_command(label="扫描游戏库（查找候选游戏）", command=self.scan_library)
        m_act.add_command(label="扫描存档更新", command=self.refresh_updates)
        m_act.add_separator()
        m_act.add_command(label="备份所有游戏", command=self.backup_all_games)
        m_act.add_command(label="备份有更新的游戏", command=self.backup_updated)
        m_act.add_command(label="备份勾选/选中的游戏", command=self.backup_selected)
        m_act.add_separator()
        m_act.add_command(label="开始 / 暂停静默监控", command=self.toggle_monitor)
        menubar.add_cascade(label="操作", menu=m_act)

        m_help = tk.Menu(menubar, tearoff=False)
        m_help.add_command(label="打开日志目录", command=lambda: self.service.open_in_explorer(str(Path(self.service.data_root()) / "logs")))
        m_help.add_command(label="关于 / 零污染说明", command=lambda: self.open_settings("about"))
        menubar.add_cascade(label="帮助", menu=m_help)

        self.configure(menu=menubar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        def btn(text: str, cmd: Callable[[], None], side: str = "left") -> ttk.Button:
            b = ttk.Button(bar, text=text, command=cmd, width=0)
            b.pack(side=side, padx=3, pady=2)
            return b

        btn("添加游戏", self.add_game)
        btn("编辑", self.edit_game)
        btn("删除", self.delete_game)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        btn("扫描游戏库", self.scan_library)
        btn("扫描存档更新", self.refresh_updates)
        btn("备份有更新的游戏", self.backup_updated)
        btn("备份选中的游戏", self.backup_selected)
        btn("备份所有游戏", self.backup_all_games)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        btn("打开存档目录", self.open_save_dir)
        btn("打开备份目录", self.open_backup_dir)
        btn("复制存档路径", self.copy_save_path)

        bar2 = ttk.Frame(self, padding=(8, 0))
        bar2.pack(fill="x")

        ttk.Label(bar2, text="勾选：").pack(side="left", pady=(0, 6))
        ttk.Button(bar2, text="全选", width=6,
                   command=lambda: self.set_all_checked(True)).pack(side="left", padx=(0, 3), pady=(0, 6))
        ttk.Button(bar2, text="全不选", width=7,
                   command=lambda: self.set_all_checked(False)).pack(side="left", padx=(0, 3), pady=(0, 6))
        ttk.Button(bar2, text="反选", width=6,
                   command=self.invert_checked).pack(side="left", padx=(0, 8), pady=(0, 6))

        self.btn_monitor = ttk.Button(bar2, text="开始静默监控", command=self.toggle_monitor)
        self.btn_monitor.pack(side="left", padx=3, pady=(0, 6))
        ttk.Button(bar2, text="设置…", command=self.open_settings).pack(side="left", padx=3, pady=(0, 6))
        ttk.Button(bar2, text="打开数据根目录",
                   command=lambda: self.service.open_in_explorer(self.service.data_root())
                   ).pack(side="left", padx=3, pady=(0, 6))
        ttk.Button(bar2, text="彻底退出", command=self.quit_app).pack(side="right", padx=3, pady=(0, 6))

    def _build_tree(self) -> None:
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(4, 4))

        cols = ("sel", "name", "engine", "saves", "install", "last", "count", "changed", "monitor")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        spec = {
            "sel": ("☑", 40, "center"),
            "name": ("游戏名称", 200, "w"),
            "engine": ("引擎", 110, "center"),
            "saves": ("存档路径", 300, "w"),
            "install": ("安装目录", 250, "w"),
            "last": ("上次备份时间", 140, "center"),
            "count": ("备份数", 60, "center"),
            "changed": ("是否有更新", 90, "center"),
            "monitor": ("监控状态", 90, "center"),
        }
        for c, (text, width, anchor) in spec.items():
            if c == "sel":
                # 点标题栏 = 全选 / 全不选
                self.tree.heading(c, text=text, command=self._toggle_all_checked)
            else:
                self.tree.heading(c, text=text, command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=width, anchor=anchor, stretch=(c in ("saves", "install")))

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("changed", background="#fff6d8")
        self.tree.tag_configure("disabled", foreground="#999999")

        self.tree.bind("<Double-1>", self._open_row_backup_dir)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<space>", self._toggle_current_row)

        self.ctx = tk.Menu(self, tearoff=False)
        self.ctx.add_command(label="勾选 / 取消勾选", command=self._ctx_toggle_checked)
        self.ctx.add_separator()
        self.ctx.add_command(label="备份勾选/选中的游戏", command=self.backup_selected)
        self.ctx.add_command(label="备份所有游戏", command=self.backup_all_games)
        self.ctx.add_separator()
        self.ctx.add_command(label="编辑…", command=self.edit_game)
        self.ctx.add_command(label="打开存档目录", command=self.open_save_dir)
        self.ctx.add_command(label="打开备份目录", command=self.open_backup_dir)
        self.ctx.add_command(label="复制存档路径", command=self.copy_save_path)
        self.ctx.add_separator()
        self.ctx.add_command(label="删除", command=self.delete_game)

        self._sort_col = "name"
        self._sort_desc = False

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text="运行日志（最近 500 条，完整日志见 <数据根目录>\\logs\\app.log）")
        box.pack(fill="both", expand=False, padx=8, pady=(0, 4))
        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_log = tk.Text(inner, height=9, wrap="none", state="disabled", background="#fbfbfb")
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.txt_log.yview)
        sb.pack(side="left", fill="y")
        self.txt_log.configure(yscrollcommand=sb.set)

    def _build_status(self) -> None:
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status, anchor="w").pack(side="left", fill="x", expand=True)
        self.var_checked = tk.StringVar(value="已勾选 0 / 0")
        ttk.Label(bar, textvariable=self.var_checked, anchor="e",
                  foreground="#1c3e70").pack(side="right", padx=(0, 14))
        self.var_monitor = tk.StringVar(value="监控：已停止")
        ttk.Label(bar, textvariable=self.var_monitor, anchor="e").pack(side="right")

    # ==================================================================
    # 线程安全入口
    # ==================================================================

    def submit(self, fn: Callable[[], None]) -> None:
        """供托盘线程 / 后台线程调用：把操作排到 Tk 主线程执行。"""
        self._ui_queue.put(fn)

    def _pump(self) -> None:
        if self._quitting:
            return
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("界面任务执行失败")

        try:
            for event in self.service.poll_events():
                self._handle_event(event)
        except Exception:  # noqa: BLE001
            log.exception("处理服务事件失败")

        if self._dirty:
            self._dirty = False
            self.refresh_tree()
        self._update_monitor_button()
        self.after(200, self._pump)

    def _mark_dirty(self) -> None:
        self._dirty = True

    # ==================================================================
    # 事件处理
    # ==================================================================

    def _handle_event(self, event: Dict[str, Any]) -> None:
        etype = event.get("type")

        if etype == "log":
            self.append_log(event.get("text", ""), event.get("level", "info"))
        elif etype == "progress":
            text = event.get("text", "")
            count = event.get("count")
            self.var_status.set(f"{text}（{count}）" if count else text)
        elif etype == "busy":
            if event.get("value"):
                self.var_status.set(event.get("text") or "处理中…")
            else:
                self.var_status.set("就绪")
        elif etype == "games_changed":
            self._dirty = True
        elif etype == "scan_done":
            self._on_scan_done(event.get("result"))
        elif etype == "updates_done":
            self._dirty = True
        elif etype == "baselines_done":
            self._dirty = True
        elif etype == "backups_done":
            self._dirty = True
            self._on_backups_done(event.get("results") or [])
        elif etype == "monitor_state":
            self._update_monitor_button()
        elif etype == "config_changed":
            self._dirty = True
            self.append_log("设置已更新")
        elif etype == "fatal":
            messagebox.showerror(APP_NAME, event.get("text", "发生未知错误"), parent=self)

    def _on_backups_done(self, results: Sequence[Dict[str, Any]]) -> None:
        # ---- 准入闸门 / 熔断拦下来的存档路径，必须让用户看见 ----
        blocked: List[str] = []
        for r in results:
            name = r.get("game_name") or r.get("game_id") or "?"
            for x in (r.get("blocked_paths") or []):
                blocked.append(f"【{name}】{x}")
            for x in (r.get("oversize_paths") or []):
                blocked.append(f"【{name}】规模超限，需人工确认：{x}")
        if blocked:
            for line in blocked[:20]:
                self.append_log("闸门拦截：" + line, "warn")
            more = f"\n…还有 {len(blocked) - 12} 条，详见运行日志" if len(blocked) > 12 else ""
            messagebox.showwarning(
                "部分存档路径被拦下",
                "下面这些存档路径没有备份 —— 它们没通过“存档路径准入闸门”，\n"
                "多半是被误判成了存档（下载目录 / 临时目录 / 缓存 / 备份目录自身）：\n\n"
                + "\n".join(blocked[:12]) + more
                + "\n\n如果其中确实有你要的存档，请用「编辑」里的「指定存档目录…」手动添加。",
                parent=self,
            )

        ok = [r for r in results if r.get("ok")]
        if not ok:
            return
        summary = "，".join(
            f"{r.get('game_name')}({r.get('file_count')} 文件)" for r in ok[:3]
        )
        if len(ok) > 3:
            summary += f" 等 {len(ok)} 个游戏"
        if self.tray is not None and self.service.config.tray_notifications:
            try:
                self.tray.notify(f"已完成 {len(ok)} 个游戏的备份：{summary}")
            except Exception:  # noqa: BLE001
                pass
        if self.state() == "withdrawn":
            self.var_status.set(f"已完成备份：{summary}")

    def _on_scan_done(self, result: Any) -> None:
        if result is None:
            return

        # 先把诊断信息写进日志（遍历了多少目录、每个库根目录的结果……）
        try:
            diag = result.diagnosis()
        except Exception:  # noqa: BLE001
            diag = getattr(result, "summary", lambda: "")()
        for line in str(diag).splitlines()[1:]:
            if line.strip():
                self.append_log(line.strip(), "warn" if line.strip().startswith(("⚠", "×", "·")) else "info")

        if not result.candidates:
            messagebox.showinfo(f"{APP_NAME} —— 扫描诊断", diag, parent=self)
            return

        existing = [g.id for g in self.service.games()]
        dlg = CandidateDialog(self, result.candidates, existing, result.summary(),
                              rules=self.service.rules)
        outcome = dlg.show()
        if not outcome:
            return
        added = self.service.add_candidates(outcome.get("candidates") or [])
        for entry in outcome.get("manual") or []:
            if self.service.add_manual_game(entry):
                added += 1
        if added:
            messagebox.showinfo(APP_NAME, f"已加入 {added} 个游戏，正在后台建立存档基线。", parent=self)
        self._dirty = True

    # ==================================================================
    # 列表
    # ==================================================================

    def append_log(self, text: str, level: str = "info") -> None:
        if not text:
            return
        stamp = time.strftime("%H:%M:%S")
        prefix = {"error": "×", "warn": "!", "info": "·"}.get(level, "·")
        line = f"[{stamp}] {prefix} {text}"
        self._log_lines.append(line)
        if len(self._log_lines) > 500:
            self._log_lines = self._log_lines[-500:]
            self.txt_log.configure(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("end", "\n".join(self._log_lines) + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
            return
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        self.refresh_tree()

    def _ctx_toggle_checked(self) -> None:
        for iid in self.tree.selection():
            self.toggle_checked(iid)

    def _open_row_backup_dir(self, event: tk.Event) -> None:
        """双击某一行 → 只打开**这一行**的备份目录（不受勾选影响）。"""
        try:
            row = self.tree.identify_row(event.y)
            if not row:
                return
            if self.tree.identify_column(event.x) == "#1":
                self.toggle_checked(row)      # 双击 ☑ 列就是勾选
                return
            self.tree.selection_set(row)
            self.service.open_backup_dir(row)
        except Exception:  # noqa: BLE001
            log.debug("双击打开备份目录失败", exc_info=True)

    def _popup_menu(self, event: tk.Event) -> None:
        """右键菜单。"""
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            try:
                self.ctx.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    self.ctx.grab_release()
                except Exception:  # noqa: BLE001
                    pass

    def refresh_tree(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())

        games = self.service.games()
        stats = self.service.game_stats()
        monitor = self.service.monitor

        rows = []
        for g in games:
            st = stats.get(g.id, {})
            diff = self.service.diff_of(g.id)
            if not monitor.is_running:
                mstate = _MONITOR_STOPPED
            elif not (g.enabled and g.monitor):
                mstate = _MONITOR_OFF
            elif monitor.is_paused:
                mstate = _MONITOR_PAUSED
            else:
                mstate = _MONITOR_ON
            changed = "—"
            if diff is not None:
                changed = "有更新" if diff.is_changed else "无"
                if diff.error:
                    changed = "出错"
            rows.append({
                "id": g.id,
                "name": g.name,
                "engine": g.engine or "未识别",
                "saves": g.primary_save_path + (f"   (+{len(g.save_paths) - 1})" if len(g.save_paths) > 1 else ""),
                "install": g.install_dir,
                "last": st.get("last_at", "") or "从未备份",
                "count": st.get("count", 0),
                "changed": changed,
                "monitor": mstate,
                "enabled": g.enabled,
            })

        key_map = {
            "name": lambda r: r["name"].lower(),
            "engine": lambda r: r["engine"].lower(),
            "saves": lambda r: r["saves"].lower(),
            "install": lambda r: r["install"].lower(),
            "last": lambda r: r["last"],
            "count": lambda r: int(r["count"]),
            "changed": lambda r: 0 if r["changed"] == "有更新" else 1,
            "monitor": lambda r: r["monitor"],
        }
        rows.sort(key=key_map.get(self._sort_col, key_map["name"]), reverse=self._sort_desc)

        # 勾选状态只保留仍然存在的游戏
        existing_ids = {r["id"] for r in rows}
        self._checked &= existing_ids

        for r in rows:
            tags = []
            if r["changed"] == "有更新":
                tags.append("changed")
            if not r["enabled"]:
                tags.append("disabled")
            self.tree.insert("", "end", iid=r["id"], tags=tuple(tags), values=(
                "☑" if r["id"] in self._checked else "☐",
                r["name"], r["engine"], r["saves"], r["install"],
                r["last"], r["count"], r["changed"], r["monitor"],
            ))

        for iid in selected:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)

        total_backups = sum(int(v.get("count", 0)) for v in stats.values())
        self.var_status.set(
            f"共 {len(games)} 个游戏，{total_backups} 份备份"
            f"　|　数据根目录：{self.service.data_root()}"
        )
        self._update_checked_status()

    def _update_monitor_button(self) -> None:
        m = self.service.monitor
        if not m.is_running:
            self.btn_monitor.configure(text="开始静默监控")
            self.var_monitor.set("监控：已停止")
        elif m.is_paused:
            self.btn_monitor.configure(text="恢复静默监控")
            self.var_monitor.set(f"监控：已暂停（{len(m.watched_paths)} 个目录）")
        else:
            self.btn_monitor.configure(text="暂停静默监控")
            self.var_monitor.set(f"监控：{m.status_text()}")

    # ==================================================================
    # 选中 / 勾选（支持任意多选）
    # ==================================================================

    def _on_tree_click(self, event: tk.Event) -> None:
        """点最左边那一列 = 勾选/取消勾选；点别处走正常的 Ctrl/Shift 多选。"""
        try:
            if self.tree.identify_region(event.x, event.y) != "cell":
                return
            if self.tree.identify_column(event.x) != "#1":
                return
            row = self.tree.identify_row(event.y)
            if not row:
                return
            self.toggle_checked(row)
            return "break"        # 阻止 Treeview 自己处理这次点击
        except Exception:  # noqa: BLE001
            return None

    def _toggle_current_row(self, _event: tk.Event = None) -> None:
        """空格键勾选/取消当前行。"""
        for iid in self.tree.selection():
            self.toggle_checked(iid)

    def toggle_checked(self, game_id: str) -> None:
        if not self.tree.exists(game_id):
            return
        if game_id in self._checked:
            self._checked.discard(game_id)
        else:
            self._checked.add(game_id)
        self._paint_check(game_id)
        self._update_checked_status()

    def _paint_check(self, game_id: str) -> None:
        try:
            self.tree.set(game_id, "sel", "☑" if game_id in self._checked else "☐")
        except Exception:  # noqa: BLE001
            pass

    def _toggle_all_checked(self) -> None:
        """点表头 ☑：全选 / 全不选（切换）。"""
        ids = [iid for iid in self.tree.get_children()]
        if ids and all(i in self._checked for i in ids):
            self.set_all_checked(False)
        else:
            self.set_all_checked(True)

    def set_all_checked(self, value: bool) -> None:
        """全选 / 全不选 **当前显示出来的** 行。"""
        for iid in self.tree.get_children():
            if value:
                self._checked.add(iid)
            else:
                self._checked.discard(iid)
            self._paint_check(iid)
        self._update_checked_status()

    def invert_checked(self) -> None:
        for iid in self.tree.get_children():
            if iid in self._checked:
                self._checked.discard(iid)
            else:
                self._checked.add(iid)
            self._paint_check(iid)
        self._update_checked_status()

    def checked_games(self) -> List[GameEntry]:
        """被勾选的游戏（按列表顺序）。"""
        out: List[GameEntry] = []
        for iid in self.tree.get_children():
            if iid in self._checked:
                g = self.service.get_game(iid)
                if g is not None:
                    out.append(g)
        return out

    def target_games(self, action: str = "") -> List[GameEntry]:
        """本次操作要作用在哪些游戏上。

        优先用**勾选**的；一个都没勾就用树里**高亮选中**的。
        这样无论用户习惯勾选还是 Ctrl 点选都能用。
        """
        games = self.checked_games()
        if games:
            return games
        games = self.selected_games()
        if not games and action:
            messagebox.showinfo(
                APP_NAME,
                f"请先勾选（最左边的 ☑ 列）或选中要{action}的游戏。\n\n"
                "提示：\n"
                "  · 直接点最左边的方框就能勾选，可以勾任意多个\n"
                "  · 也可以用 Ctrl / Shift + 单击多选\n"
                "  · 勾选后点「全选 / 全不选 / 反选」批量操作\n"
                "  · 表头那个 ☑ 点一下等于全选",
                parent=self,
            )
        return games

    def _update_checked_status(self) -> None:
        n = len(self._checked)
        total = len(self.tree.get_children())
        try:
            self.var_checked.set(f"已勾选 {n} / {total}")
        except Exception:  # noqa: BLE001
            pass

    # ==================================================================
    # 选中项（树里高亮的那几行）
    # ==================================================================

    def selected_games(self) -> List[GameEntry]:
        out: List[GameEntry] = []
        for iid in self.tree.selection():
            g = self.service.get_game(iid)
            if g is not None:
                out.append(g)
        return out

    def _one_selected(self, action: str) -> Optional[GameEntry]:
        games = self.target_games("")
        if not games:
            messagebox.showinfo(APP_NAME, f"请先勾选或选中一个游戏，再点「{action}」。", parent=self)
            return None
        if len(games) > 1:
            messagebox.showinfo(
                APP_NAME,
                f"「{action}」一次只能作用于一个游戏。\n"
                f"现在勾选/选中了 {len(games)} 个，请只留一个（或取消其它勾选）。",
                parent=self,
            )
            return None
        return games[0]

    # ==================================================================
    # 操作
    # ==================================================================

    def add_game(self) -> None:
        dlg = ManualGameDialog(self, default_backup_root=self.service.backup_root(),
                               data_root=self.service.data_root(), rules=self.service.rules)
        entry = dlg.show()
        if entry is not None:
            self.service.add_manual_game(entry)
            self._dirty = True

    def edit_game(self) -> None:
        game = self._one_selected("编辑")
        if game is None:
            return
        dlg = ManualGameDialog(self, default_backup_root=self.service.backup_root(),
                               entry=game, data_root=self.service.data_root(),
                               rules=self.service.rules)
        new_entry = dlg.show()
        if new_entry is not None:
            self.service.update_game(new_entry)
            self.service.reload_monitoring()
            self._dirty = True

    def delete_game(self) -> None:
        games = self.target_games("删除")
        if not games:
            return
        names = "、".join(g.name for g in games[:6]) + ("…" if len(games) > 6 else "")
        if not ask_yes_no(
            self, "确认删除",
            f"确定要从列表删除这 {len(games)} 个游戏吗？\n\n{names}\n\n"
            "（不会删除游戏本体，也不会删除游戏存档）",
        ):
            return
        also = ask_yes_no(
            self, "是否删除备份",
            "是否同时删除这些游戏已经生成的备份快照？\n\n"
            "「是」= 连同备份一起删除（不可恢复）\n「否」= 只从列表移除，备份文件保留",
        )
        for g in games:
            self.service.remove_game(g.id, delete_backups=bool(also))
            self._checked.discard(g.id)
        self.service.reload_monitoring()
        self._dirty = True

    def scan_library(self) -> None:
        if not self.service.config.library_roots and not self.service.config.scan.detect_steam:
            messagebox.showinfo(
                APP_NAME,
                "还没有设置「游戏库根目录」，且 Steam 扫描已关闭。\n"
                "请到「设置 → 扫描」里添加至少一个游戏库根目录。",
                parent=self,
            )
            return
        self.service.scan_candidates(include_steam=self.service.config.scan.detect_steam)

    def refresh_updates(self) -> None:
        self.service.refresh_updates()

    def backup_updated(self) -> None:
        self.service.backup_updated()

    def backup_selected(self) -> None:
        games = self.target_games("备份")
        if not games:
            return
        if len(games) > 1:
            if not ask_yes_no(
                self, "批量备份",
                f"将对勾选/选中的 {len(games)} 个游戏各生成一份快照备份。\n\n"
                "（不比较是否有更新，直接备份）\n继续吗？",
            ):
                return
            self.append_log(f"开始批量备份 {len(games)} 个游戏…")
        for g in games:
            self.service.backup_game(g.id, trigger="manual")

    def backup_all_games(self) -> None:
        """备份列表里的**所有**游戏（不比较更新，直接各生成一份快照）。"""
        games = self.service.games()
        if not games:
            messagebox.showinfo(APP_NAME, "游戏列表是空的，先扫描或添加游戏吧。", parent=self)
            return
        total_paths = sum(len(g.save_paths_norm()) for g in games)
        no_save = [g.name for g in games if not g.save_paths_norm()]
        extra = ""
        if no_save:
            shown = "、".join(no_save[:5]) + ("…" if len(no_save) > 5 else "")
            extra = (f"\n\n注意：其中有 {len(no_save)} 个游戏还没记录存档路径，会被跳过：\n{shown}")
        if not ask_yes_no(
            self, "备份所有游戏",
            f"将对列表里全部 {len(games)} 个游戏各生成一份独立快照备份。\n\n"
            f"· 不比较是否有更新，直接备份\n"
            f"· 一共 {total_paths} 个存档路径\n"
            f"· 文件较多时可能耗时较久，请耐心等待（界面不会卡）"
            f"{extra}\n\n继续吗？",
        ):
            return
        self.append_log(f"开始备份所有游戏（{len(games)} 个）…")
        self.service.backup_all()

    def open_save_dir(self) -> None:
        game = self._one_selected("打开存档目录")
        if game is not None:
            self.service.open_save_dir(game.id)

    def open_backup_dir(self) -> None:
        game = self._one_selected("打开备份目录")
        if game is not None:
            self.service.open_backup_dir(game.id)

    def copy_save_path(self) -> None:
        games = self.target_games("复制存档路径")
        if not games:
            return
        lines: List[str] = []
        for g in games:
            paths = g.save_paths_norm()
            if len(games) > 1:
                lines.append(f"# {g.name}")
            lines.extend(paths or ["# （该游戏没有记录存档路径）"])
        if not lines:
            messagebox.showinfo(APP_NAME, "这些游戏都没有记录存档路径。", parent=self)
            return
        text = "\r\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.append_log(f"已复制 {len(games)} 个游戏的存档路径到剪贴板（共 {len(lines)} 行）")

    def toggle_monitor(self) -> None:
        self.service.toggle_monitoring()
        self._update_monitor_button()

    def open_settings(self, tab: str = "") -> None:
        rules_path = str(Path(self.service.data_root()) / RULES_SUBDIR / RULES_FILENAME)
        dlg = SettingsDialog(
            self,
            self.service.config,
            self.service.data_root(),
            rules_path=rules_path,
            on_reload_rules=self._reload_rules,
            on_reset_rules=self._reset_rules,
        )
        new_cfg = dlg.show()
        if new_cfg is None:
            return
        self.service.apply_config(new_cfg)
        self.service.reload_monitoring()
        self.service.ensure_baselines()
        self._dirty = True

    def _reload_rules(self) -> None:
        try:
            self.service.rules = type(self.service.rules).load(self.service.guard)
            self.service.scanner.rules = self.service.rules
            self.service.backups.rules = self.service.rules
            self.service.monitor.rules = self.service.rules
            self.append_log("检测规则已重新加载")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"加载规则失败：{exc}", parent=self)

    def _reset_rules(self) -> None:
        try:
            self.service.rules.reset_to_default(self.service.guard)
            self.append_log("已恢复默认检测规则")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"恢复默认规则失败：{exc}", parent=self)

    # ==================================================================
    # 显示 / 隐藏 / 退出
    # ==================================================================

    def show_window(self) -> None:
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(120, lambda: self.attributes("-topmost", False))
        except Exception:  # noqa: BLE001
            log.exception("显示主窗口失败")

    def hide_window(self) -> None:
        try:
            self.withdraw()
        except Exception:  # noqa: BLE001
            log.exception("隐藏主窗口失败")

    def on_close_button(self) -> None:
        """点击窗口右上角 X。行为由设置决定（默认最小化到托盘）。"""
        if self.service.config.close_action == "exit":
            self.quit_app()
            return
        if self.tray is not None and self.tray.running:
            self.hide_window()
            if not self._notified_hidden and self.service.config.tray_notifications:
                self._notified_hidden = True
                try:
                    self.tray.notify("程序仍在后台监控存档；从托盘菜单可以彻底退出。")
                except Exception:  # noqa: BLE001
                    pass
        else:
            # 没有托盘可用时，隐藏窗口就等于让用户找不到程序，直接退出更安全
            if ask_yes_no(self, "没有可用的系统托盘",
                          "当前环境没有安装 pystray/Pillow，无法最小化到托盘。\n\n要直接彻底退出吗？"):
                self.quit_app()

    def quit_app(self) -> None:
        """彻底退出（真正的进程结束，不是最小化）。"""
        if self._quitting:
            return
        if not ask_yes_no(
            self, "彻底退出",
            "确定要彻底退出吗？\n\n"
            "· 停止所有监控与后台任务\n"
            "· 移除系统托盘图标\n"
            "· 释放文件锁与数据库\n"
            "· 进程完全结束（任务管理器里不会残留）",
        ):
            return
        self._quitting = True
        try:
            self.withdraw()
        except Exception:
            pass
        self.on_quit(0)
