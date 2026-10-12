# -*- coding: utf-8 -*-
"""对话框集合。

* :class:`FirstRunDialog` —— 首次启动向导，选择总数据根目录 / 备份根目录 / 游戏库根目录。
  **在用户点击「开始使用」之前，程序不会写入任何文件。**
* :class:`CandidateDialog` —— 扫描候选确认（可勾选）。
* :class:`ManualGameDialog` —— 手动添加游戏（自动检测失败时的兜底）。
* :class:`SettingsDialog` —— 设置页（多标签）。

GUI 使用标准库 tkinter：

* 不写注册表、不写 AppData、不生成任何配置缓存；
* 因此天然满足零污染要求（换成 Qt/Electron 就需要额外做配置重定向）。
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, List, Optional, Sequence

from . import APP_NAME, APP_VERSION
from .config import AppConfig, ScanOptions, MonitorOptions, RetentionPolicy
from .detect import infer_save_paths, sniff_save_dirs
from .fsutil import human_bytes
from .models import Candidate, GameEntry, make_game_id
from .paths import is_within, normalize
from .rules import RULES_FILENAME, RULES_SUBDIR, RuleSet

__all__ = [
    "center_window",
    "FirstRunDialog",
    "CandidateDialog",
    "ManualGameDialog",
    "SettingsDialog",
    "ask_yes_no",
]

#: 候选来源的显示名
SOURCE_LABELS = {
    "steam": "Steam",
    "library": "游戏库",
    "registry": "注册表",
    "shortcut": "快捷方式",
    "recent": "最近使用",
    "manual": "手动",
    "system": "系统",
}


def center_window(win: tk.Misc, width: int, height: int) -> None:
    """把窗口居中；高度会**自动收进屏幕范围**，避免底部按钮跑到屏幕外。"""
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    width = min(int(width), max(400, sw - 40))
    height = min(int(height), max(320, sh - 100))
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 3)
    win.geometry(f"{width}x{height}+{x}+{y}")


class ScrollableFrame(ttk.Frame):
    """可滚动容器：把内容放进 ``.inner``。

    为什么需要它：对话框内容一旦比窗口高，**后 pack 的控件（尤其是底部按钮）
    会被直接裁掉、点都点不到**。把内容塞进可滚动区域，窗口再小也能把按钮露出来。
    """

    def __init__(self, master: tk.Misc, height: int = 380) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, height=height)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", self._grab_wheel)
            w.bind("<Leave>", self._release_wheel)

    def _on_inner(self, _event: Any = None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:  # noqa: BLE001
            pass

    def _on_canvas(self, event: tk.Event) -> None:
        try:
            self.canvas.itemconfigure(self._win, width=event.width)
        except Exception:  # noqa: BLE001
            pass

    def _wheel(self, event: tk.Event) -> None:
        try:
            self.canvas.yview_scroll(int(-1 * (getattr(event, "delta", 0) / 120)), "units")
        except Exception:  # noqa: BLE001
            pass

    def _grab_wheel(self, _event: Any = None) -> None:
        try:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
        except Exception:  # noqa: BLE001
            pass

    def _release_wheel(self, _event: Any = None) -> None:
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except Exception:  # noqa: BLE001
            pass


def ask_yes_no(parent: tk.Misc, title: str, message: str) -> bool:
    return bool(messagebox.askyesno(title, message, parent=parent))


def _select_dir(parent: tk.Misc, title: str, initial: str = "") -> str:
    path = filedialog.askdirectory(
        parent=parent, title=title, initialdir=initial or os.path.expanduser("~"), mustexist=False
    )
    if not path:
        return ""
    try:
        return str(normalize(path))
    except OSError:
        return path


def available_drives() -> List[str]:
    return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]


def _pick_from_row(parent: tk.Misc, var: tk.StringVar, title: str) -> None:
    p = _select_dir(parent, title, var.get())
    if p:
        var.set(p)


class PathListEditor(ttk.Frame):
    """一行一个路径的编辑控件（可添加多个文件夹）。

    每行 = **可直接输入/粘贴的输入框** + 「浏览…」 + 「✕」，
    底部还有「+ 添加文件夹…」「+ 添加整个磁盘…」。

    比 Listbox 好用：能直接粘贴路径、能一眼看清每一行、行数不限。
    末尾始终保留一个空行，方便直接粘。
    """

    def __init__(self, master: tk.Misc, browse_title: str = "选择文件夹",
                 initial_rows: int = 2, hint: str = "") -> None:
        super().__init__(master)
        self.browse_title = browse_title
        self._rows: List[Tuple[ttk.Frame, tk.StringVar]] = []

        self._holder = ttk.Frame(self)
        self._holder.pack(fill="x")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="+ 添加文件夹…", command=self.add_row_browse).pack(side="left")
        ttk.Button(bar, text="+ 添加整个磁盘…", command=self.add_row_drive).pack(side="left", padx=4)
        if hint:
            ttk.Label(bar, text=hint, foreground="#777777").pack(side="left", padx=(8, 0))

        for _ in range(max(1, initial_rows)):
            self.add_row()

    # -- 行管理 -------------------------------------------------------------

    def add_row(self, value: str = "") -> tk.StringVar:
        frame = ttk.Frame(self._holder)
        frame.pack(fill="x", pady=2)
        var = tk.StringVar(value=value)
        ttk.Entry(frame, textvariable=var).pack(side="left", fill="x", expand=True)
        ttk.Button(frame, text="浏览…", width=8,
                   command=lambda v=var: _pick_from_row(self, v, self.browse_title)
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(frame, text="✕", width=3,
                   command=lambda f=frame, v=var: self._remove_row(f, v)
                   ).pack(side="left", padx=(4, 0))
        self._rows.append((frame, var))
        return var

    def _remove_row(self, frame: ttk.Frame, var: tk.StringVar) -> None:
        try:
            frame.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._rows = [(f, v) for f, v in self._rows if v is not var]
        self._ensure_blank_row()

    def _ensure_blank_row(self) -> None:
        if not self._rows or any(not v.get().strip() for _f, v in self._rows):
            return
        self.add_row()

    def _live_rows(self) -> List[Tuple[ttk.Frame, tk.StringVar]]:
        return list(self._rows)

    def add_row_browse(self) -> None:
        p = _select_dir(self, self.browse_title)
        if p:                       # 取消选择时不新增空行
            self.add_row(p)

    def add_row_drive(self) -> None:
        d = choose_drive(self)
        if d:
            self.add_row(d)

    # -- 取值 / 赋值 ---------------------------------------------------------

    def get(self) -> List[str]:
        """返回去空、去重后的路径列表。"""
        out: List[str] = []
        for _f, var in self._live_rows():
            raw = var.get().strip().strip('"').strip()
            if not raw:
                continue
            try:
                p = str(normalize(raw))
            except OSError:
                p = raw
            if p and p not in out:
                out.append(p)
        return out

    def set(self, paths: Sequence[str]) -> None:
        for frame, _var in list(self._rows):
            try:
                frame.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._rows = []
        for p in (paths or []):
            if p:
                self.add_row(str(p))
        if not self._rows:
            self.add_row()


def choose_drive(parent: tk.Misc) -> str:
    """弹一个磁盘选择框，返回 ``"D:\\\\"`` 这样的路径（取消则返回空串）。"""
    drives = available_drives()
    if not drives:
        messagebox.showinfo("没有可用磁盘", "没有检测到可用磁盘。", parent=parent)
        return ""

    win = tk.Toplevel(parent)
    win.title("选择要扫描的磁盘")
    win.transient(parent)
    win.grab_set()
    center_window(win, 340, 320)

    ttk.Label(win, text="把整个磁盘加入扫描范围：",
              font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
    ttk.Label(win, text="注意：系统盘（通常是 C:）目录极多，扫描会慢一些。\n"
                        "扫描是只读的，不会改动任何文件。",
              foreground="#a05000", justify="left").pack(anchor="w", padx=12)

    lb = tk.Listbox(win, height=8, activestyle="none")
    lb.pack(fill="both", expand=True, padx=12, pady=8)
    for d in drives:
        lb.insert("end", d)
    lb.selection_set(0)

    result = {"v": ""}
    foot = ttk.Frame(win)
    foot.pack(fill="x", padx=12, pady=(0, 10))

    def _ok() -> None:
        sel = lb.curselection()
        if sel:
            result["v"] = drives[sel[0]]
        win.destroy()

    ttk.Button(foot, text="取消", command=win.destroy).pack(side="right")
    ttk.Button(foot, text="加入扫描范围", command=_ok).pack(side="right", padx=(0, 8))
    win.wait_window()
    return result["v"]


# ===========================================================================
# 首次启动向导
# ===========================================================================

class FirstRunDialog:
    """首次启动向导（独立 Tk 根窗口）。"""

    def __init__(self, pointer_path: Optional[Path] = None, suggested_root: str = "") -> None:
        self.pointer_path = pointer_path
        self.suggested_root = suggested_root
        self.result: Optional[Dict[str, Any]] = None

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} —— 首次启动设置")
        # 允许缩放：万一内容还是超出，用户自己也能拉大窗口
        self.root.resizable(True, True)
        self.root.minsize(660, 460)
        center_window(self.root, 760, 700)

        self.var_data_root = tk.StringVar(value=suggested_root)
        self.var_backup_root = tk.StringVar(value="")
        self.var_use_default_backup = tk.BooleanVar(value=True)
        self.var_remember = tk.BooleanVar(value=True)
        self.var_sha = tk.BooleanVar(value=True)
        self.var_mtime = tk.BooleanVar(value=True)
        self.library_roots: List[str] = []

        self._build()

    # -- 构建界面 -----------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}

        # ① 先把底部按钮钉在窗口底部 —— 无论内容多长，都一定可见、可点。
        footer = ttk.Frame(self.root)
        footer.pack(side="bottom", fill="x", **pad)
        ttk.Button(footer, text="彻底退出（不保存）", command=self._cancel).pack(side="right")
        self.btn_start = ttk.Button(footer, text="开始使用", command=self._confirm)
        self.btn_start.pack(side="right", padx=(0, 8))

        # ② 其余内容放进可滚动区域，内容再长也不会被裁掉
        self.scroller = ScrollableFrame(self.root, height=400)
        self.scroller.pack(side="top", fill="both", expand=True, padx=6, pady=(6, 0))
        body = self.scroller.inner

        header = ttk.Frame(body)
        header.pack(fill="x", **pad)
        ttk.Label(header, text=APP_NAME, font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "一个纯本地的存档快照备份工具。它不会开机自启、不写注册表、不装服务、"
                "不监控游戏进程。\n只有你主动启动它时才会工作，选择「彻底退出」后进程完全消失。"
            ),
            foreground="#444444",
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        box = ttk.LabelFrame(body, text="① 数据存放位置（必须由你指定）")
        box.pack(fill="x", **pad)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="总数据根目录：", width=14).pack(side="left")
        e = ttk.Entry(row, textvariable=self.var_data_root)
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._pick_data_root).pack(side="left", padx=(6, 0))

        ttk.Label(
            box,
            text=(
                "配置、日志、缓存、临时文件、数据库、检测规则、崩溃日志全部只写在这个目录里。\n"
                "在你点击下方「开始使用」之前，程序不会创建或写入任何文件。"
            ),
            foreground="#666666",
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", padx=8, pady=6)
        ttk.Label(row2, text="备份根目录：", width=14).pack(side="left")
        self.entry_backup = ttk.Entry(row2, textvariable=self.var_backup_root)
        self.entry_backup.pack(side="left", fill="x", expand=True)
        self.btn_backup = ttk.Button(row2, text="浏览…", command=self._pick_backup_root)
        self.btn_backup.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            box,
            text="使用默认位置（总数据根目录\\backups）",
            variable=self.var_use_default_backup,
            command=self._sync_backup_entry,
        ).pack(anchor="w", padx=10, pady=(0, 8))

        if self.pointer_path is not None:
            # 注意：ttk.Checkbutton 不支持 -justify（tk.Checkbutton 才支持），
            # 这里刻意用 tk.Checkbutton 来显示多行左对齐的说明文字。
            tk.Checkbutton(
                box,
                text=(
                    f"记住这个位置（会在程序目录写入一个仅含路径的指针文件："
                    f"{self.pointer_path.name}）\n"
                    f"取消勾选则每次启动都会询问，完全不在数据根目录之外写任何文件。"
                ),
                variable=self.var_remember,
                justify="left",
                anchor="w",
                background="#f0f0f0",
                activebackground="#f0f0f0",
            ).pack(anchor="w", padx=10, pady=(0, 8))

        box2 = ttk.LabelFrame(body, text="② 扫描范围：游戏库根目录（★ 最重要，决定能不能找到你下的游戏）")
        box2.pack(fill="both", expand=True, **pad)

        ttk.Label(
            box2,
            text=(
                "把你放 galgame 的文件夹加进来，例如  D:\\GAL、E:\\Galgame、F:\\游戏。\n"
                "要点到“装着一堆游戏文件夹”的那一层，不要点到某一个游戏里面。\n"
                "可以直接在输入框里粘贴路径，也可以点「浏览…」选。"
            ),
            foreground="#1c3e70", justify="left",
        ).pack(anchor="w", padx=10, pady=(6, 2))

        self.editor_lib = PathListEditor(
            box2,
            browse_title="选择游戏库根目录（装着一堆游戏文件夹的那一层）",
            initial_rows=3,
        )
        self.editor_lib.pack(fill="x", padx=8, pady=(2, 4))

        ttk.Label(
            box2,
            text=(
                "程序只扫描这些目录及其子目录，不做全盘扫描。\n"
                "不填也能用 —— 那样就只扫描 Steam 库和系统的“安装记录 / 快捷方式 / 最近使用”，\n"
                "但自己下载的、没装过的绿色版很可能就找不到了。\n"
                "之后随时可以在「设置 → 扫描」里增删。"
            ),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        box3 = ttk.LabelFrame(body, text="③ 扫描与备份选项")
        box3.pack(fill="x", **pad)
        ttk.Checkbutton(box3, text="计算 SHA256（更准确，大存档首次基线会慢一些）",
                        variable=self.var_sha).pack(anchor="w", padx=10, pady=2)
        ttk.Checkbutton(box3, text="比较修改时间（推荐开启）",
                        variable=self.var_mtime).pack(anchor="w", padx=10, pady=2)

        self.root.protocol("WM_DELETE_WINDOW", self._cancel)

        # ③ 建完之后再按“内容实际高度”调整窗口大小，尽量把整页显示全；
        #    实在放不下也没关系 —— 底部按钮已经钉住了，内容可以滚动。
        self.root.update_idletasks()
        try:
            need = body.winfo_reqheight() + footer.winfo_reqheight() + 80
        except Exception:  # noqa: BLE001
            need = 700
        center_window(self.root, 760, max(520, min(need, 900)))

    def _sync_backup_entry(self) -> None:
        state = "disabled" if self.var_use_default_backup.get() else "normal"
        self.entry_backup.configure(state=state)
        self.btn_backup.configure(state=state)

    def _pick_data_root(self) -> None:
        p = _select_dir(self.root, "选择总数据根目录", self.var_data_root.get())
        if p:
            self.var_data_root.set(p)
            self.var_use_default_backup.set(True)
            self._sync_backup_entry()

    def _pick_backup_root(self) -> None:
        p = _select_dir(self.root, "选择备份根目录", self.var_backup_root.get())
        if p:
            self.var_backup_root.set(p)

    # -- 校验与结果 ---------------------------------------------------------

    def _confirm(self) -> None:
        data_root = self.var_data_root.get().strip()
        if not data_root:
            messagebox.showwarning("需要指定目录", "请先选择「总数据根目录」。", parent=self.root)
            return
        try:
            data_root = str(normalize(data_root))
        except OSError as exc:
            messagebox.showerror("路径无效", str(exc), parent=self.root)
            return

        # 从“一行一个路径”的编辑器里取扫描范围（去空、去重）
        self.library_roots = self.editor_lib.get()

        if self.var_use_default_backup.get() or not self.var_backup_root.get().strip():
            backup_root = str(Path(data_root) / "backups")
        else:
            backup_root = str(normalize(self.var_backup_root.get().strip()))

        warn = self._sanity_check(data_root, backup_root)
        if warn and not messagebox.askyesno("确认位置", warn + "\n\n仍然使用这些位置吗？", parent=self.root):
            return

        # 没配扫描范围的话，自己在网上下载的游戏很可能一个都找不到 —— 明确提醒一次
        if not self.library_roots:
            if not messagebox.askyesno(
                "还没有添加扫描范围",
                "你还没有添加任何「游戏库根目录」。\n\n"
                "这样的话，程序只能扫描 Steam 库和系统的安装记录/快捷方式，\n"
                "自己在网上下载、解压即玩的 galgame 很可能一个都找不到。\n\n"
                "建议点「添加文件夹…」选到你放游戏的那一层（例如 D:\\GAL），\n"
                "或者点「添加整个磁盘…」直接扫整个盘。\n\n"
                "确定不添加、直接继续吗？",
                parent=self.root,
            ):
                return

        self.result = {
            "data_root": data_root,
            "backup_root": backup_root,
            "library_roots": list(self.library_roots),
            "remember": bool(self.var_remember.get()),
            "compute_sha256": bool(self.var_sha.get()),
            "compare_mtime": bool(self.var_mtime.get()),
        }
        self.root.destroy()

    def _sanity_check(self, data_root: str, backup_root: str) -> str:
        msgs: List[str] = []
        sensitive = []
        for var in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "ProgramFiles", "ProgramFiles(x86)"):
            v = os.environ.get(var)
            if v:
                sensitive.append(normalize(v))
        up = os.environ.get("USERPROFILE")
        if up:
            sensitive += [normalize(Path(up) / "Documents"), normalize(Path(up) / "Saved Games")]

        for label, p in (("总数据根目录", data_root), ("备份根目录", backup_root)):
            pn = normalize(p)
            for s in sensitive:
                try:
                    if pn == s or str(pn).lower().startswith(str(s).lower() + os.sep):
                        msgs.append(f"• {label} 位于系统敏感目录内：{pn}")
                except Exception:
                    pass
            if pn.parent == pn:
                msgs.append(f"• {label} 是磁盘根目录：{pn}")
        if msgs:
            return (
                "以下位置可能会让文件散落到系统目录：\n\n" + "\n".join(dict.fromkeys(msgs)) +
                "\n\n建议改用一个独立的文件夹，例如 D:\\GalgameGuard 与 E:\\GalgameBackups。"
            )
        return ""

    def _cancel(self) -> None:
        self.result = None
        self.root.destroy()

    def show(self) -> Optional[Dict[str, Any]]:
        self._sync_backup_entry()
        self.root.mainloop()
        return self.result


# ===========================================================================
# 候选确认
# ===========================================================================

class CandidateDialog:
    """扫描结果确认窗口。"""

    def __init__(self, parent: tk.Misc, candidates: Sequence[Candidate],
                 existing_ids: Sequence[str] = (), scan_summary: str = "",
                 rules: Optional[RuleSet] = None) -> None:
        self.parent = parent
        self.candidates: List[Candidate] = list(candidates)
        self.existing_ids = set(existing_ids)
        self.rules = rules
        self.result: Optional[Dict[str, Any]] = None
        self.manual_entries: List[GameEntry] = []
        self._visible: List[Candidate] = []

        self.win = tk.Toplevel(parent)
        self.win.title(f"扫描结果 —— 共 {len(self.candidates)} 个候选游戏")
        self.win.transient(parent)
        self.win.grab_set()
        center_window(self.win, 1060, 620)

        self._build(scan_summary)

    def _build(self, scan_summary: str) -> None:
        top = ttk.Frame(self.win)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="请勾选要加入监控的游戏：", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Label(top, text=scan_summary, foreground="#666666").pack(side="left", padx=(12, 0))

        # 筛选框：自下资源的候选可能很多，用来快速定位
        top2 = ttk.Frame(self.win)
        top2.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(top2, text="筛选：").pack(side="left")
        self.var_filter = tk.StringVar()
        ttk.Entry(top2, textvariable=self.var_filter, width=36).pack(side="left")
        self.var_filter.trace_add("write", lambda *a: self._refresh())
        ttk.Label(top2, text="（按游戏名 / 安装目录 / 引擎过滤）",
                  foreground="#777777").pack(side="left", padx=6)
        self.var_only_saves = tk.BooleanVar(value=False)
        ttk.Checkbutton(top2, text="只看已找到存档的", variable=self.var_only_saves,
                        command=self._refresh).pack(side="left", padx=(10, 0))
        self.var_count = tk.StringVar(value="")
        ttk.Label(top2, textvariable=self.var_count, foreground="#333333").pack(side="right")

        mid = ttk.Frame(self.win)
        mid.pack(fill="both", expand=True, padx=10)

        cols = ("sel", "name", "engine", "save", "install", "score", "source")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        headings = {
            "sel": ("选择", 46),
            "name": ("游戏名称", 220),
            "engine": ("引擎", 110),
            "save": ("存档路径", 80),
            "install": ("安装目录", 330),
            "score": ("评分", 50),
            "source": ("来源", 70),
        }
        for c, (text, width) in headings.items():
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor="w" if c in ("name", "install", "engine") else "center")
        self.tree.column("save", anchor="center")

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())

        detail_box = ttk.LabelFrame(self.win, text="详情（选中一行查看；双击游戏名可以直接改名）")
        detail_box.pack(fill="x", padx=10, pady=8)
        self.txt_detail = tk.Text(detail_box, height=7, wrap="word", state="disabled",
                                  background="#fafafa", relief="flat")
        self.txt_detail.pack(fill="both", expand=True, padx=6, pady=6)

        btns = ttk.Frame(self.win)
        btns.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(btns, text="全选", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(btns, text="全不选", command=lambda: self._set_all(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="仅选高置信度", command=self._select_high).pack(side="left", padx=4)
        ttk.Button(btns, text="重命名选中…", command=self._rename_selected).pack(side="left", padx=(16, 4))
        ttk.Button(btns, text="指定存档目录…", command=self._assign_save_dir).pack(side="left", padx=4)
        ttk.Button(btns, text="手动添加游戏…", command=self._manual_add).pack(side="left", padx=(16, 4))

        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="加入游戏列表", command=self._confirm).pack(side="right", padx=(0, 8))

        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        self._iid_map: Dict[str, Candidate] = {}
        self._refresh()

    # -- 列表操作 -----------------------------------------------------------

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._iid_map.clear()
        flt = ""
        try:
            flt = self.var_filter.get().strip().lower()
        except Exception:  # noqa: BLE001
            pass
        only_saves = False
        try:
            only_saves = bool(self.var_only_saves.get())
        except Exception:  # noqa: BLE001
            pass

        shown = 0
        picked = 0
        visible: List[Candidate] = []
        for i, c in enumerate(self.candidates):
            already = make_game_id(c.name, c.install_dir) in self.existing_ids
            if already:
                c.selected = False
            if c.selected:
                picked += 1
            if only_saves and not c.save_paths:
                continue
            hay = f"{c.name}\n{c.install_dir}\n{c.engine}".lower()
            if flt and flt not in hay:
                continue
            visible.append(c)
            iid = f"c{i}"
            self._iid_map[iid] = c
            self.tree.insert("", "end", iid=iid, values=(
                "☑" if c.selected else "☐",
                c.name + ("（已在列表中）" if already else ""),
                c.engine or "未识别",
                len(c.save_paths),
                c.install_dir,
                c.score,
                SOURCE_LABELS.get(c.source, c.source),
            ))
            shown += 1
        self._visible = visible
        try:
            self.var_count.set(f"显示 {shown}/{len(self.candidates)}　已勾选 {picked}")
        except Exception:  # noqa: BLE001
            pass

    def _selected_candidate(self) -> Optional[Candidate]:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一行。", parent=self.win)
            return None
        return self._iid_map.get(sel[0])

    def _on_double_click(self, event: tk.Event) -> None:
        """双击游戏名那一列 → 直接改名。"""
        try:
            if self.tree.identify_column(event.x) == "#2" and self.tree.identify_row(event.y):
                self.tree.selection_set(self.tree.identify_row(event.y))
                self._rename_selected()
        except Exception:  # noqa: BLE001
            pass

    def _rename_selected(self) -> None:
        c = self._selected_candidate()
        if c is None:
            return
        new = simpledialog.askstring(
            "重命名游戏", "游戏名称：", initialvalue=c.name, parent=self.win
        )
        if not new or not new.strip() or new.strip() == c.name:
            return
        c.name = new.strip()
        # 名字变了，它可能就不再是“已在列表中”的那个了，刷新一下
        self._refresh()
        self._show_detail()

    def _assign_save_dir(self) -> None:
        c = self._selected_candidate()
        if c is None:
            return
        p = filedialog.askdirectory(
            parent=self.win, title=f"为「{c.name}」选择存档目录", mustexist=True
        )
        if not p:
            return
        try:
            p = str(normalize(p))
        except OSError:
            return
        if all(p.lower() != x.lower() for x in c.save_paths):
            c.save_paths.append(p)
            c.save_evidence.append("用户手动指定的存档目录")
        c.selected = True
        self._refresh()
        self._show_detail()

    def _on_click(self, event: tk.Event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row or col != "#1":
            return
        cand = self._iid_map.get(row)
        if cand is None:
            return
        if make_game_id(cand.name, cand.install_dir) in self.existing_ids:
            return
        cand.selected = not cand.selected
        self.tree.set(row, "sel", "☑" if cand.selected else "☐")
        try:
            picked = sum(1 for c in self.candidates if c.selected)
            self.var_count.set(f"显示 {len(self._iid_map)}/{len(self.candidates)}　已勾选 {picked}")
        except Exception:  # noqa: BLE001
            pass

    def _set_all(self, value: bool) -> None:
        """全选 / 全不选 —— 只作用于**当前筛选出来的行**，避免误选看不见的东西。"""
        targets = self._visible if self._visible else self.candidates
        for c in targets:
            if make_game_id(c.name, c.install_dir) in self.existing_ids:
                c.selected = False
            else:
                c.selected = value
        self._refresh()

    def _select_high(self) -> None:
        targets = self._visible if self._visible else self.candidates
        for c in targets:
            if make_game_id(c.name, c.install_dir) in self.existing_ids:
                c.selected = False
            else:
                c.selected = c.score >= 4 and bool(c.save_paths)
        self._refresh()

    def _show_detail(self) -> None:
        sel = self.tree.selection()
        text = ""
        if sel:
            c = self._iid_map.get(sel[0])
            if c is not None:
                lines = [
                    f"游戏名称：{c.name}",
                    f"引擎推断：{c.engine or '未识别'}（置信度 {c.engine_confidence}）",
                    f"主程序　：{c.exe or '未找到'}",
                    f"安装目录：{c.install_dir}",
                    "",
                    "候选存档路径：",
                ]
                if c.save_paths:
                    lines += [f"  [{i + 1}] {p}" for i, p in enumerate(c.save_paths)]
                else:
                    lines.append("  （未找到，可加入后手动指定）")
                if c.save_evidence:
                    lines.append("")
                    lines.append("判定依据：")
                    lines += [f"  · {e}" for e in c.save_evidence]
                if c.reasons:
                    lines.append("")
                    lines.append("引擎特征：" + "，".join(c.reasons))
                text = "\n".join(lines)
        self.txt_detail.configure(state="normal")
        self.txt_detail.delete("1.0", "end")
        self.txt_detail.insert("1.0", text)
        self.txt_detail.configure(state="disabled")

    def _manual_add(self) -> None:
        dlg = ManualGameDialog(self.win, default_backup_root="", rules=self.rules)
        entry = dlg.show()
        if entry is not None:
            self.manual_entries.append(entry)
            messagebox.showinfo("已暂存", f"「{entry.name}」已暂存，点击「加入游戏列表」后一起保存。",
                                parent=self.win)

    def _confirm(self) -> None:
        selected = [c for c in self.candidates if c.selected]
        if not selected and not self.manual_entries:
            if not ask_yes_no(self.win, "没有选中任何游戏", "没有勾选任何候选游戏，仍要继续吗？"):
                return
        self.result = {"candidates": selected, "manual": self.manual_entries}
        self.win.grab_release()
        self.win.destroy()

    def _cancel(self) -> None:
        self.result = None
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()

    def show(self) -> Optional[Dict[str, Any]]:
        self.win.wait_window()
        return self.result


# ===========================================================================
# 手动添加 / 编辑游戏
# ===========================================================================

class ManualGameDialog:
    """手动添加或编辑一个游戏。"""

    def __init__(self, parent: tk.Misc, default_backup_root: str = "",
                 entry: Optional[GameEntry] = None,
                 data_root: str = "",
                 rules: Optional[RuleSet] = None) -> None:
        self.parent = parent
        self.default_backup_root = default_backup_root
        self.data_root = data_root
        self.entry = entry
        self.rules = rules
        self.result: Optional[GameEntry] = None

        self.win = tk.Toplevel(parent)
        self.win.title("编辑游戏" if entry else "手动添加游戏")
        self.win.transient(parent)
        self.win.grab_set()
        center_window(self.win, 780, 680)

        self.var_name = tk.StringVar(value=entry.name if entry else "")
        self.var_exe = tk.StringVar(value=entry.exe if entry else "")
        self.var_install = tk.StringVar(value=entry.install_dir if entry else "")
        self.var_engine = tk.StringVar(value=entry.engine if entry else "")
        self.var_backup = tk.StringVar(value=(entry.backup_root if entry else "") or "")
        self.var_notes = tk.StringVar(value=entry.notes if entry else "")
        self.var_monitor = tk.BooleanVar(value=entry.monitor if entry else True)
        self.var_enabled = tk.BooleanVar(value=entry.enabled if entry else True)
        self.var_scan_user = tk.BooleanVar(value=True)
        self.var_engine_text = tk.StringVar(value="填好「安装目录」后自动识别")

        self._build()
        if self.entry and self.entry.install_dir:
            self._detect_engine()

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 5}

        # 底部按钮先钉住（side="bottom" 且最先 pack），保证永远可见可点
        footer = ttk.Frame(self.win)
        footer.pack(side="bottom", fill="x", **pad)
        ttk.Button(footer, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(footer, text="保存", command=self._confirm).pack(side="right", padx=(0, 8))

        frm = ttk.Frame(self.win)
        frm.pack(fill="x", **pad)

        def row(label: str, var: tk.StringVar, browse: Optional[str] = None, width: int = 62) -> ttk.Entry:
            r = ttk.Frame(frm)
            r.pack(fill="x", pady=4)
            ttk.Label(r, text=label, width=13).pack(side="left")
            e = ttk.Entry(r, textvariable=var, width=width)
            e.pack(side="left", fill="x", expand=True)
            if browse == "dir":
                ttk.Button(r, text="浏览…", command=lambda v=var: self._pick_dir(v)).pack(side="left", padx=(6, 0))
            elif browse == "file":
                ttk.Button(r, text="浏览…", command=lambda v=var: self._pick_file(v)).pack(side="left", padx=(6, 0))
            return e

        row("游戏名称 *", self.var_name)
        row("主程序 exe", self.var_exe, "file")
        row("安装目录", self.var_install, "dir")
        row("备份根目录", self.var_backup, "dir")

        # 引擎**不需要你填**：程序会从安装目录的特征文件里自己识别。
        er = ttk.Frame(frm)
        er.pack(fill="x", pady=4)
        ttk.Label(er, text="引擎", width=13).pack(side="left")
        ttk.Label(er, textvariable=self.var_engine_text,
                  foreground="#1c3e70", wraplength=430, justify="left").pack(side="left")
        ttk.Button(er, text="重新识别", command=self._detect_engine).pack(side="left", padx=(10, 0))

        ttk.Label(frm, text="（备份根目录留空 = 使用全局备份根目录；引擎由程序自动识别，不用你填）",
                  foreground="#777777").pack(anchor="w", padx=(90, 0))

        sp_box = ttk.LabelFrame(self.win, text="存档路径 *（一行一个，可添加多个）")
        sp_box.pack(fill="both", expand=True, **pad)
        sp_inner = ttk.Frame(sp_box)
        sp_inner.pack(fill="both", expand=True, padx=6, pady=6)

        self.txt_saves = tk.Text(sp_inner, height=8, wrap="none")
        self.txt_saves.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(sp_inner, orient="vertical", command=self.txt_saves.yview)
        sb.pack(side="left", fill="y")
        self.txt_saves.configure(yscrollcommand=sb.set)
        if self.entry:
            self.txt_saves.insert("1.0", "\n".join(self.entry.save_paths))

        sp_btns = ttk.Frame(sp_box)
        sp_btns.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(sp_btns, text="添加目录…", command=self._add_save_dir).pack(side="left")
        ttk.Button(sp_btns, text="清空", command=lambda: self.txt_saves.delete("1.0", "end")).pack(side="left", padx=4)
        ttk.Button(sp_btns, text="★ 自动猜存档目录", command=self._auto_sniff).pack(side="left", padx=(16, 4))
        ttk.Button(sp_btns, text="重新识别（替换）", command=self._reinfer_paths).pack(side="left", padx=4)
        ttk.Checkbutton(sp_btns, text="同时搜文档/AppData",
                        variable=self.var_scan_user).pack(side="left", padx=4)

        note = ttk.Frame(self.win)
        note.pack(fill="x", **pad)
        ttk.Label(note, text="备注", width=13).pack(side="left")
        ttk.Entry(note, textvariable=self.var_notes).pack(side="left", fill="x", expand=True)

        opt = ttk.Frame(self.win)
        opt.pack(fill="x", **pad)
        ttk.Checkbutton(opt, text="启用静默监控（该游戏存档变化时自动备份）",
                        variable=self.var_monitor).pack(anchor="w")
        ttk.Checkbutton(opt, text="启用（取消勾选则忽略该游戏）",
                        variable=self.var_enabled).pack(anchor="w")

        ttk.Label(
            self.win,
            text=(
                "找不到存档？填好「安装目录」后点「自动猜存档目录」，"
                "程序会去安装目录及其子目录、旁边的目录、文档/AppData 里\n"
                "搜“长得像存档的文件”，把找到的目录都填进来（多填了可以删掉）。\n"
                "更准的办法：先在游戏里存一次档，再点这个按钮 —— 刚被写过的目录更容易被找到。\n"
                "常见位置：游戏安装目录\\save、%APPDATA%\\<引擎>\\<游戏名>、"
                "文档\\<游戏名>、%USERPROFILE%\\AppData\\LocalLow\\<公司>\\<产品>。"
            ),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=12)

        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

    def _reinfer_paths(self) -> None:
        """用当前（已修正的）识别逻辑重新推一遍，并**替换**文本框内容。

        适合这种情况：游戏早就加进列表了，但当时识别出来的存档路径是错的
        （比如把 AppData 下一堆无关目录当成了存档）。点一下就能刷新。
        """
        raw = self.var_install.get().strip()
        if not raw:
            messagebox.showinfo("提示", "请先填写「安装目录」，再点「重新识别」。", parent=self.win)
            return
        try:
            install = normalize(raw)
        except OSError:
            install = None
        if install is None or not install.is_dir():
            messagebox.showwarning("目录不存在", f"安装目录不存在：\n{raw}", parent=self.win)
            return

        rules = self.rules or RuleSet()
        name = self.var_name.get().strip() or install.name
        engine = ""
        try:
            hits = rules.detect_engines(install)
            if hits:
                engine = hits[0].engine
        except Exception:  # noqa: BLE001
            pass

        try:
            paths, why = infer_save_paths(
                install, engine, name, rules,
                scan_user_locations=bool(self.var_scan_user.get()),
                sniff_max_depth=4,
                max_paths=8,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("识别失败", f"{type(exc).__name__}: {exc}", parent=self.win)
            return

        if not paths:
            messagebox.showinfo(
                "没识别到存档路径",
                "没能自动识别出存档路径。\n\n"
                "可以点「添加目录…」手动指定；\n"
                "更有效的办法是先在游戏里存一次档，再回来点一次。",
                parent=self.win,
            )
            return

        if not messagebox.askyesno(
            "重新识别存档路径",
            f"将把存档路径**替换**为下面这 {len(paths)} 个：\n\n"
            + "\n".join(paths)
            + "\n\n判定依据：\n"
            + "\n".join(f"  · {w}" for w in why[:6])
            + "\n\n继续吗？",
            parent=self.win,
        ):
            return

        self.txt_saves.delete("1.0", "end")
        self.txt_saves.insert("1.0", "\n".join(paths))

    def _append_save_path(self, p: str) -> None:
        cur = self.txt_saves.get("1.0", "end").strip()
        if cur:
            self.txt_saves.insert("end", "\n" + p)
        else:
            self.txt_saves.insert("1.0", p)
        self.txt_saves.see("end")

    def _auto_sniff(self) -> None:
        """根据安装目录自动嗅探可能的存档目录。"""
        install_raw = self.var_install.get().strip()
        if not install_raw:
            messagebox.showinfo("提示", "请先填写「安装目录」，再点「自动猜存档目录」。", parent=self.win)
            return
        install = normalize(install_raw)
        if not install.is_dir():
            messagebox.showwarning("目录不存在", f"安装目录不存在：\n{install}", parent=self.win)
            return

        rules = self.rules or RuleSet()
        name = self.var_name.get().strip() or install.name
        try:
            found = sniff_save_dirs(
                install,
                name,
                rules,
                max_depth=4,
                recent_days=0,
                user_roots=(rules.user_save_roots() if self.var_scan_user.get() else []),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("嗅探失败", f"{type(exc).__name__}: {exc}", parent=self.win)
            return

        existing = {
            str(normalize(line.strip())).lower()
            for line in self.txt_saves.get("1.0", "end").splitlines()
            if line.strip()
        }
        added = 0
        lines: List[str] = []
        for p, why, _score in found[:8]:
            try:
                pn = str(normalize(p))
            except OSError:
                continue
            if pn.lower() in existing:
                continue
            self._append_save_path(pn)
            existing.add(pn.lower())
            added += 1
            lines.append(f"  · {pn}\n      （{why}）")

        if not found:
            messagebox.showinfo(
                "没嗅探到",
                "没有找到像存档的目录。\n\n"
                "建议：先启动游戏并存一次档，回到这里再点一次这个按钮；\n"
                "或者手动点「添加目录…」指定存档位置。",
                parent=self.win,
            )
            return
        messagebox.showinfo(
            "嗅探完成",
            f"找到 {len(found)} 个候选，新填入 {added} 个：\n\n" + "\n".join(lines[:6])
            + ("\n  …" if len(lines) > 6 else "")
            + "\n\n填多了不要紧，在文本框里删掉多余的即可。",
            parent=self.win,
        )

    def _pick_dir(self, var: tk.StringVar) -> None:
        p = _select_dir(self.win, "选择目录", var.get())
        if not p:
            return
        var.set(p)
        if var is self.var_install:
            # 选完安装目录：顺手自动识别引擎、并在游戏名为空时用目录名预填
            if not self.var_name.get().strip():
                try:
                    guessed = (self.rules or RuleSet()).strip_game_name(Path(p).name)
                except Exception:  # noqa: BLE001
                    guessed = Path(p).name
                self.var_name.set(guessed or Path(p).name)
            self._detect_engine()

    def _detect_engine(self) -> None:
        """从安装目录自动识别引擎（用户不需要懂什么是引擎）。"""
        rules = self.rules or RuleSet()
        raw = self.var_install.get().strip()
        if not raw:
            self.var_engine.set("")
            self.var_engine_text.set("先填「安装目录」，再点这里识别")
            return
        try:
            install = normalize(raw)
        except OSError:
            install = None
        if install is None or not install.is_dir():
            self.var_engine.set("")
            self.var_engine_text.set("安装目录不存在或无法访问")
            return
        try:
            hits = rules.detect_engines(install)
        except Exception:  # noqa: BLE001
            hits = []
        if hits:
            self.var_engine.set(hits[0].engine)
            self.var_engine_text.set(
                f"{hits[0].engine}　（置信度 {rules.confidence_label(hits[0])}："
                f"{'，'.join(hits[0].reasons)}）"
            )
        else:
            self.var_engine.set("")
            self.var_engine_text.set("未能识别引擎 —— 不影响使用，关键是「存档路径」填对")

    def _pick_file(self, var: tk.StringVar) -> None:
        p = filedialog.askopenfilename(
            parent=self.win, title="选择游戏主程序",
            filetypes=[("可执行文件", "*.exe *.lnk"), ("所有文件", "*.*")],
        )
        if p:
            var.set(str(normalize(p)))

    def _add_save_dir(self) -> None:
        p = _select_dir(self.win, "选择存档目录")
        if p:
            current = self.txt_saves.get("1.0", "end").strip()
            if current:
                self.txt_saves.insert("end", "\n" + p)
            else:
                self.txt_saves.insert("1.0", p)

    def _confirm(self) -> None:
        name = self.var_name.get().strip()
        if not name:
            messagebox.showwarning("缺少信息", "请填写游戏名称。", parent=self.win)
            return
        saves = [
            str(normalize(line.strip()))
            for line in self.txt_saves.get("1.0", "end").splitlines()
            if line.strip()
        ]
        saves = list(dict.fromkeys(saves))
        if not saves:
            if not ask_yes_no(self.win, "没有存档路径",
                              "还没有填写任何存档路径。仍然保存吗？（之后可在编辑里补充）"):
                return
        else:
            missing = [s for s in saves if not Path(s).is_dir()]
            if missing and not ask_yes_no(
                self.win, "部分目录不存在",
                "以下存档目录当前不存在：\n\n" + "\n".join(missing) +
                "\n\n仍然保存吗？（程序会在目录出现后自动纳入监控）",
            ):
                return

        install_dir = self.var_install.get().strip()
        install_dir = str(normalize(install_dir)) if install_dir else ""

        # 引擎始终以程序自己识别的结果为准（对话框里没有让你手填的地方）
        engine = ""
        if install_dir:
            try:
                hits = (self.rules or RuleSet()).detect_engines(normalize(install_dir))
                if hits:
                    engine = hits[0].engine
            except Exception:  # noqa: BLE001
                engine = self.var_engine.get().strip()
        if not engine:
            engine = self.var_engine.get().strip()

        entry = GameEntry(
            id=self.entry.id if self.entry else make_game_id(name, install_dir),
            name=name,
            engine=engine,
            exe=self.var_exe.get().strip(),
            install_dir=install_dir,
            save_paths=saves,
            backup_root=str(normalize(self.var_backup.get())) if self.var_backup.get().strip() else "",
            notes=self.var_notes.get().strip(),
            source=self.entry.source if self.entry else "manual",
            enabled=bool(self.var_enabled.get()),
            monitor=bool(self.var_monitor.get()),
            created_at=self.entry.created_at if self.entry else "",
        )
        if not entry.created_at:
            from .models import now_iso

            entry.created_at = now_iso()
        self.result = entry
        self.win.grab_release()
        self.win.destroy()

    def _cancel(self) -> None:
        self.result = None
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()

    def show(self) -> Optional[GameEntry]:
        self.win.wait_window()
        return self.result


# ===========================================================================
# 设置
# ===========================================================================

class SettingsDialog:
    """设置窗口，返回一份新的 :class:`AppConfig`。"""

    def __init__(self, parent: tk.Misc, config: AppConfig, data_root: str,
                 rules_path: str = "", on_reload_rules: Optional[Any] = None,
                 on_reset_rules: Optional[Any] = None) -> None:
        self.parent = parent
        self.config = config.clone()
        self.data_root = data_root
        self.rules_path = rules_path
        self.on_reload_rules = on_reload_rules
        self.on_reset_rules = on_reset_rules
        self.result: Optional[AppConfig] = None

        self.win = tk.Toplevel(parent)
        self.win.title("设置")
        self.win.transient(parent)
        self.win.grab_set()
        center_window(self.win, 780, 700)

        self._build()

    # -- 构建 ---------------------------------------------------------------

    def _build(self) -> None:
        # 底部按钮先钉住（最先 pack + side="bottom"），保证永远可见可点
        footer = ttk.Frame(self.win)
        footer.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        ttk.Button(footer, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(footer, text="保存设置", command=self._confirm).pack(side="right", padx=(0, 8))

        nb = ttk.Notebook(self.win)
        nb.pack(side="top", fill="both", expand=True, padx=10, pady=10)

        self._tab_general(nb)
        self._tab_sources(nb)
        self._tab_scan(nb)
        self._tab_discover(nb)
        self._tab_monitor(nb)
        self._tab_retention(nb)
        self._tab_rules(nb)
        self._tab_about(nb)

        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

    def _tab_general(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="常规")

        r = ttk.Frame(f)
        r.pack(fill="x", padx=10, pady=8)
        ttk.Label(r, text="总数据根目录：", width=16).pack(side="left")
        e = ttk.Entry(r, width=58)
        e.insert(0, self.data_root)
        e.configure(state="readonly")
        e.pack(side="left", fill="x", expand=True)

        ttk.Label(f, text="（总数据根目录在首次启动时确定，之后不可更改；如需更换请手动搬家或重装。）",
                  foreground="#777777").pack(anchor="w", padx=12)

        r2 = ttk.Frame(f)
        r2.pack(fill="x", padx=10, pady=8)
        ttk.Label(r2, text="备份根目录：", width=16).pack(side="left")
        self.var_backup = tk.StringVar(value=self.config.backup_root)
        ttk.Entry(r2, textvariable=self.var_backup, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="浏览…", command=lambda: self._pick(self.var_backup)).pack(side="left", padx=(6, 0))

        box = ttk.LabelFrame(f, text="关闭主窗口时（点右上角 X）")
        box.pack(fill="x", padx=10, pady=8)
        self.var_close = tk.StringVar(value=self.config.close_action)
        ttk.Radiobutton(box, text="最小化到系统托盘（程序继续监控，默认）",
                        variable=self.var_close, value="tray").pack(anchor="w", padx=10, pady=3)
        ttk.Radiobutton(box, text="直接彻底退出（停止监控、移除托盘、进程完全结束）",
                        variable=self.var_close, value="exit").pack(anchor="w", padx=10, pady=3)
        ttk.Label(box, text="无论选哪个，托盘菜单和主界面的「彻底退出」都会真正结束程序。",
                  foreground="#777777").pack(anchor="w", padx=10, pady=(2, 6))

        self.var_auto = tk.BooleanVar(value=self.config.auto_start_monitor)
        ttk.Checkbutton(f, text="程序启动后自动开始静默监控", variable=self.var_auto).pack(anchor="w", padx=12, pady=4)

        self.var_notify = tk.BooleanVar(value=self.config.tray_notifications)
        ttk.Checkbutton(f, text="备份完成时显示托盘气泡提示（默认关闭，保持安静）",
                        variable=self.var_notify).pack(anchor="w", padx=12, pady=4)

    def _tab_sources(self, nb: ttk.Notebook) -> None:
        """检测来源：每一类来源都可以单独关掉。"""
        f = ttk.Frame(nb)
        nb.add(f, text="检测来源")

        sc = self.config.scan
        self.var_src = {
            "steam": tk.BooleanVar(value=sc.detect_steam),
            "registry_games": tk.BooleanVar(value=sc.find_registry_games),
            "shortcuts": tk.BooleanVar(value=sc.find_shortcuts),
            "recent_used": tk.BooleanVar(value=sc.find_recent_used),
            "install_subdirs": tk.BooleanVar(value=sc.scan_install_subdirs),
            "install_root": tk.BooleanVar(value=sc.scan_install_root),
            "config_files": tk.BooleanVar(value=sc.scan_config_files),
            "sniff_install": tk.BooleanVar(value=sc.sniff_install_dir),
            "sniff_siblings": tk.BooleanVar(value=sc.sniff_sibling_dirs),
            "user_locations": tk.BooleanVar(value=sc.scan_user_save_locations),
            "steam_cloud": tk.BooleanVar(value=sc.steam_cloud_saves),
            "registry_saves": tk.BooleanVar(value=sc.registry_save_search),
        }

        ttk.Label(
            f,
            text=("关掉某个来源后，对应的扫描阶段**完全不执行**，既不产生候选游戏，"
                  "也不产生存档路径。\n全部关掉也能用 —— 那就只剩下「游戏库根目录」这一条途径。"),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=12, pady=(10, 6))

        box1 = ttk.LabelFrame(f, text="① 从哪里找游戏")
        box1.pack(fill="x", padx=10, pady=6)
        opts1 = [
            ("steam", "扫描 Steam 库（只读注册表 + libraryfolders.vdf + appmanifest_*.acf）"),
            ("registry_games", "从注册表的安装记录里发现（安装版游戏）"),
            ("shortcuts", "从开始菜单 / 桌面快捷方式里发现（绿色版、免安装版）"),
            ("recent_used", "从「最近使用」记录里发现（你真正运行过的程序）"),
        ]
        for key, text in opts1:
            ttk.Checkbutton(box1, text=text, variable=self.var_src[key]).pack(
                anchor="w", padx=10, pady=2)
        ttk.Label(
            box1,
            text=("后三项属于「系统痕迹发现」。全部关掉，程序就不会去读注册表、"
                  "也不会翻快捷方式和最近使用记录。"),
            foreground="#888888", justify="left",
        ).pack(anchor="w", padx=10, pady=(2, 6))

        box2 = ttk.LabelFrame(f, text="② 从哪里找存档")
        box2.pack(fill="x", padx=10, pady=6)
        opts2 = [
            ("install_subdirs", "安装目录下的常见存档目录（save / savedata / セーブ / 存档 …）"),
            ("install_root", "安装目录本身就是存档目录（NScripter / ONScripter 这类）"),
            ("config_files", "游戏自己的配置文件里写明的路径（.ini/.dat/.json …，精度最高）"),
            ("sniff_install", "存档嗅探：在安装目录子树里找像存档的目录"),
            ("sniff_siblings", "存档嗅探：在安装目录旁边的目录里找"),
            ("user_locations", "只读扫描 文档 / 已保存的游戏 / AppData / LocalLow"),
            ("steam_cloud", "Steam 云存档 userdata\\<id>\\<appid>\\remote"),
            ("registry_saves", "在注册表 HKCU\\Software 里找同名键记录的存档路径"),
        ]
        for key, text in opts2:
            ttk.Checkbutton(box2, text=text, variable=self.var_src[key]).pack(
                anchor="w", padx=10, pady=2)
        ttk.Label(
            box2,
            text=("全都通过「存档路径准入闸门」，被拦下的路径不会备份。\n"
                  "Ludusavi 数据库在「自动发现」页配置。"),
            foreground="#888888", justify="left",
        ).pack(anchor="w", padx=10, pady=(2, 6))

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bar, text="全部打开", command=lambda: self._set_sources(True)).pack(side="left")
        ttk.Button(bar, text="全部关闭", command=lambda: self._set_sources(False)).pack(
            side="left", padx=6)
        ttk.Button(bar, text="只保留游戏库 + 安装目录扫描",
                   command=self._sources_minimal).pack(side="left", padx=6)

    def _set_sources(self, value: bool) -> None:
        for var in self.var_src.values():
            var.set(bool(value))

    def _sources_minimal(self) -> None:
        """最“安静”的组合：只扫自己的游戏库和安装目录，不碰系统任何东西。"""
        keep = {"install_subdirs", "install_root", "config_files",
                "sniff_install", "sniff_siblings"}
        for key, var in self.var_src.items():
            var.set(key in keep)

    def _tab_scan(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="扫描")

        self.var_sha = tk.BooleanVar(value=self.config.scan.compute_sha256)
        self.var_mtime = tk.BooleanVar(value=self.config.scan.compare_mtime)
        self.var_steam = tk.BooleanVar(value=self.config.scan.detect_steam)
        self.var_userloc = tk.BooleanVar(value=self.config.scan.scan_user_save_locations)
        self.var_maxdepth = tk.IntVar(value=self.config.scan.max_depth)
        self.var_maxhash = tk.IntVar(value=self.config.scan.hash_max_mb)
        self.var_sniff = tk.IntVar(value=self.config.scan.sniff_max_depth)
        self.var_recent = tk.IntVar(value=self.config.scan.recent_days)
        self.var_maxpaths = tk.IntVar(value=self.config.scan.max_save_paths)

        ttk.Checkbutton(f, text="计算 SHA256（更可靠，代价是首次基线较慢）",
                        variable=self.var_sha).pack(anchor="w", padx=12, pady=4)
        ttk.Checkbutton(f, text="比较修改时间", variable=self.var_mtime).pack(anchor="w", padx=12, pady=4)
        ttk.Checkbutton(f, text="扫描 Steam 库（只读注册表 + libraryfolders.vdf）",
                        variable=self.var_steam).pack(anchor="w", padx=12, pady=4)
        ttk.Checkbutton(f, text="只读扫描文档 / 已保存的游戏 / AppData / LocalLow",
                        variable=self.var_userloc).pack(anchor="w", padx=12, pady=4)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="递归深度上限：").pack(side="left")
        ttk.Spinbox(r, from_=1, to=10, width=5, textvariable=self.var_maxdepth).pack(side="left")
        ttk.Label(r, text="　　超过该大小的文件不计算哈希（MB）：").pack(side="left")
        ttk.Spinbox(r, from_=0, to=4096, width=7, textvariable=self.var_maxhash).pack(side="left")

        r2 = ttk.Frame(f)
        r2.pack(fill="x", padx=12, pady=6)
        ttk.Label(r2, text="存档嗅探深度：").pack(side="left")
        ttk.Spinbox(r2, from_=1, to=8, width=5, textvariable=self.var_sniff).pack(side="left")
        ttk.Label(r2, text=" 层（在安装目录里往下找存档的层数）　　额外搜最近 ").pack(side="left")
        ttk.Spinbox(r2, from_=0, to=365, width=6, textvariable=self.var_recent).pack(side="left")
        ttk.Label(r2, text=" 天内改过的存档目录（0 = 关闭）").pack(side="left")

        r3 = ttk.Frame(f)
        r3.pack(fill="x", padx=12, pady=6)
        ttk.Label(r3, text="每个游戏最多自动选中 ").pack(side="left")
        ttk.Spinbox(r3, from_=1, to=30, width=5, textvariable=self.var_maxpaths).pack(side="left")
        ttk.Label(r3, text=" 个存档路径（按“像不像存档”打分排序取前几个）").pack(side="left")

        ttk.Label(
            f,
            text=(
                "「存档嗅探」是给自下资源（汉化版 / 绿色版 / 合集）准备的兜底：\n"
                "  不依赖引擎识别，直接在安装目录、安装目录旁边的目录、文档/AppData 里\n"
                "  搜“长得像存档的文件”（save*.dat / *.sav / savedata 等），找到就列为候选。\n"
                "精度保障：① 只认强存档特征（只有一个 config.dat 不算）；\n"
                "          ② 名字像“补丁 / 备份 / 破解 / 缓存”的目录直接排除；\n"
                "          ③ 父子目录折叠（save\\ 和 save\\备份\\ 只留前者）；\n"
                "          ④ 最近改动过的、以及游戏自己配置文件里写明的路径得分更高。\n"
                "被舍弃的候选会写进候选窗口的「详情」里，需要时可以手动补。"
            ),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=12, pady=(6, 0))

        box = ttk.LabelFrame(f, text="★ 扫描范围：游戏库根目录（只扫描这些目录及其子目录）")
        box.pack(fill="x", padx=10, pady=8)
        ttk.Label(
            box,
            text=("指到“装着一堆游戏文件夹”的那一层，例如 D:\\GAL。自己下的 galgame 全靠这里。\n"
                  "一行一个，可以直接粘贴路径。"),
            foreground="#1c3e70", justify="left",
        ).pack(anchor="w", padx=8, pady=(6, 2))
        self.editor_lib = PathListEditor(
            box,
            browse_title="选择游戏库根目录（装着一堆游戏文件夹的那一层）",
            initial_rows=2,
        )
        self.editor_lib.pack(fill="x", padx=8, pady=(2, 8))
        self.editor_lib.set(self.config.library_roots)

        box2 = ttk.LabelFrame(f, text="额外存档目录（自动检测不到时可在这里补充，只读扫描）")
        box2.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(
            box2,
            text="某些游戏的存档在很偏的地方时，把那个文件夹写在这里，也会被扫描。",
            foreground="#666666",
        ).pack(anchor="w", padx=8, pady=(6, 2))
        self.editor_extra = PathListEditor(
            box2, browse_title="选择额外存档目录", initial_rows=2
        )
        self.editor_extra.pack(fill="x", padx=8, pady=(2, 8))
        self.editor_extra.set(self.config.extra_save_dirs)

    def _tab_discover(self, nb: ttk.Notebook) -> None:
        """自动发现（系统痕迹 + Ludusavi 离线数据库）。"""
        f = ttk.Frame(nb)
        nb.add(f, text="自动发现")

        self.var_sysscan = tk.BooleanVar(value=self.config.scan.discover_system)
        self.var_regsave = tk.BooleanVar(value=self.config.scan.registry_save_search)
        self.var_manifest = tk.StringVar(value=self.config.scan.manifest_path)

        box = ttk.LabelFrame(f, text="从系统痕迹里发现游戏（全部只读，不写注册表）")
        box.pack(fill="x", padx=10, pady=10)
        ttk.Checkbutton(
            box,
            text="从「注册表安装记录 / 开始菜单与桌面快捷方式 / 最近使用」里发现游戏",
            variable=self.var_sysscan,
        ).pack(anchor="w", padx=10, pady=4)
        ttk.Label(
            box,
            text=("这一项专门用来找“自己下的资源”：\n"
                  "  · 安装版游戏一般在注册表里留了安装位置\n"
                  "  · 绿色版/免安装版通常有快捷方式\n"
                  "  · “最近使用”记录的是你真正运行过的程序，最贴近“玩过的游戏”"),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 4))
        ttk.Checkbutton(
            box,
            text="在 HKCU\\Software 里找与游戏同名的键，读出其中记录的存档路径",
            variable=self.var_regsave,
        ).pack(anchor="w", padx=10, pady=4)
        ttk.Label(
            box,
            text="不少日式引擎会把“存档目录”直接写在注册表里，这是一条很有效的捷径。",
            foreground="#666666",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        box2 = ttk.LabelFrame(f, text="Ludusavi 存档数据库（可选，离线导入）")
        box2.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(
            box2,
            text=("Ludusavi 是目前最主流的开源存档备份工具，它的做法是把 PCGamingWiki 上\n"
                  "人工维护的存档位置编译成一份 manifest.yaml。\n\n"
                  "本程序不联网、不下载，你可以自己把 manifest.yaml 下载到本地后在这里选中它；\n"
                  "之后每次扫描都会按游戏名查表，命中的存档路径会直接填进候选。\n"
                  "（对冷门同人/汉化作品覆盖率有限，但对付商业游戏很有效。）"),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=10, pady=(8, 4))

        rr = ttk.Frame(box2)
        rr.pack(fill="x", padx=10, pady=(2, 10))
        ttk.Entry(rr, textvariable=self.var_manifest).pack(side="left", fill="x", expand=True)
        ttk.Button(rr, text="浏览…", command=self._pick_manifest).pack(side="left", padx=(6, 0))
        ttk.Button(rr, text="清空", command=lambda: self.var_manifest.set("")).pack(side="left", padx=(6, 0))

    def _pick_manifest(self) -> None:
        p = filedialog.askopenfilename(
            parent=self.win,
            title="选择 Ludusavi manifest.yaml",
            filetypes=[("YAML 文件", "*.yaml *.yml"), ("所有文件", "*.*")],
        )
        if p:
            try:
                self.var_manifest.set(str(normalize(p)))
            except OSError:
                self.var_manifest.set(p)

    def _tab_monitor(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="监控")

        self.var_debounce = tk.IntVar(value=self.config.monitor.debounce_seconds)
        self.var_poll = tk.IntVar(value=self.config.monitor.poll_interval_seconds)
        self.var_backend = tk.StringVar(value=self.config.monitor.backend)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=12, pady=12)
        ttk.Label(r, text="防抖秒数：").pack(side="left")
        ttk.Spinbox(r, from_=0, to=120, width=6, textvariable=self.var_debounce).pack(side="left")
        ttk.Label(r, text="　（同一存档目录在这么多秒内的多次变化只备份一次）").pack(side="left")

        r2 = ttk.Frame(f)
        r2.pack(fill="x", padx=12, pady=6)
        ttk.Label(r2, text="轮询间隔：").pack(side="left")
        ttk.Spinbox(r2, from_=2, to=600, width=6, textvariable=self.var_poll).pack(side="left")
        ttk.Label(r2, text="　秒（仅在无 watchdog 时使用）").pack(side="left")

        box = ttk.LabelFrame(f, text="监控后端")
        box.pack(fill="x", padx=12, pady=10)
        for value, text in (
            ("auto", "自动（优先 watchdog 内核事件，不可用时回退到轮询）"),
            ("watchdog", "只用 watchdog（需要 pip install watchdog）"),
            ("polling", "只用轮询（零依赖，纯标准库）"),
        ):
            ttk.Radiobutton(box, text=text, variable=self.var_backend, value=value).pack(anchor="w", padx=10, pady=3)

        ttk.Label(
            f,
            text=(
                "监控只针对“已确认游戏”的存档目录，基于文件系统事件/轮询实现。\n"
                "程序不会监控游戏进程、不读游戏内存、不 hook 任何 API。\n"
                "暂停监控只是停止自动备份；手动“立即备份”始终可用。"
            ),
            foreground="#666666", justify="left",
        ).pack(anchor="w", padx=12, pady=8)

    def _tab_retention(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="保留策略")

        self.var_ret_mode = tk.StringVar(value=self.config.retention.mode)
        self.var_ret_count = tk.IntVar(value=self.config.retention.count)
        self.var_ret_days = tk.IntVar(value=self.config.retention.days)

        ttk.Radiobutton(f, text="不自动删除（默认，推荐；旧备份永久保留）",
                        variable=self.var_ret_mode, value="none").pack(anchor="w", padx=12, pady=8)
        r = ttk.Frame(f)
        r.pack(anchor="w", padx=12, pady=4)
        ttk.Radiobutton(r, text="每个游戏只保留最近", variable=self.var_ret_mode, value="count").pack(side="left")
        ttk.Spinbox(r, from_=1, to=9999, width=7, textvariable=self.var_ret_count).pack(side="left", padx=4)
        ttk.Label(r, text="份快照").pack(side="left")

        r2 = ttk.Frame(f)
        r2.pack(anchor="w", padx=12, pady=4)
        ttk.Radiobutton(r2, text="只保留最近", variable=self.var_ret_mode, value="days").pack(side="left")
        ttk.Spinbox(r2, from_=1, to=3650, width=7, textvariable=self.var_ret_days).pack(side="left", padx=4)
        ttk.Label(r2, text="天的快照").pack(side="left")

        ttk.Label(
            f,
            text="清理只作用于备份根目录内的快照目录，绝不会触碰游戏存档本身。",
            foreground="#666666",
        ).pack(anchor="w", padx=12, pady=12)

    def _tab_rules(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="检测规则")

        ttk.Label(f, text="规则文件位置：").pack(anchor="w", padx=12, pady=(12, 2))
        e = ttk.Entry(f, width=80)
        e.insert(0, self.rules_path or f"{self.data_root}\\{RULES_SUBDIR}\\{RULES_FILENAME}")
        e.configure(state="readonly")
        e.pack(fill="x", padx=12)

        ttk.Label(
            f,
            text=(
                "规则是 JSON 格式，可以自行增删：\n"
                "  · engine_signatures —— 引擎特征（目录名 / 文件 glob / 权重 / 常见存档子目录）\n"
                "  · save_dir_names —— 存档目录名候选\n"
                "  · ignore_dir_names —— 扫描时跳过的目录名\n"
                "  · ignore_file_globs —— 备份时忽略的临时文件\n"
                "  · name_strip_patterns —— 游戏名清洗正则\n"
                "  · user_save_rel_roots —— 用户级存档搜索根\n\n"
                "修改后点「重新加载规则」即可生效（部分设置需要重启程序）。"
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=10)

        b = ttk.Frame(f)
        b.pack(fill="x", padx=12, pady=6)
        ttk.Button(b, text="打开规则文件所在目录", command=self._open_rules_dir).pack(side="left")
        ttk.Button(b, text="重新加载规则", command=self._reload_rules).pack(side="left", padx=6)
        ttk.Button(b, text="恢复默认规则", command=self._reset_rules).pack(side="left", padx=6)

    def _tab_about(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb)
        nb.add(f, text="关于 / 零污染")
        text = (
            f"{APP_NAME} v{APP_VERSION}\n\n"
            "本程序只做一件事：把你自己的本地游戏存档，快照复制到你指定的备份目录。\n"
            "它不绕过 DRM、不修改游戏文件、不联网、不上传任何数据。\n\n"
            "【零污染承诺】\n"
            "  · 不实现开机自启、不写启动文件夹、不创建计划任务、不安装系统服务。\n"
            "  · 不写入 %APPDATA%、%LOCALAPPDATA%、%TEMP%、文档、已保存的游戏、注册表。\n"
            "  · 注册表只读（仅用于读取 Steam 安装路径）。\n"
            "  · Python 临时目录被重定向到「总数据根目录\\temp」。\n"
            "  · GUI 使用标准库 tkinter，不产生任何配置文件。\n"
            "  · 卸载只需删除「总数据根目录」和「备份根目录」。\n\n"
            "【彻底退出】\n"
            "  托盘菜单和主界面都提供「彻底退出」：停止监控线程与后台任务、\n"
            "  移除托盘图标、关闭数据库、释放文件锁，然后进程完全结束。\n"
            "  关闭主窗口（X）默认只是隐藏到托盘，可在「常规」页改为直接彻底退出。"
        )
        t = tk.Text(f, wrap="word", relief="flat", background="#f7f7f7", height=26)
        t.insert("1.0", text)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True, padx=14, pady=14)

    # -- 交互 ---------------------------------------------------------------

    def _pick(self, var: tk.StringVar) -> None:
        p = _select_dir(self.win, "选择目录", var.get())
        if p:
            var.set(p)

    def _open_rules_dir(self) -> None:
        p = self.rules_path or str(Path(self.data_root) / RULES_SUBDIR)
        target = Path(p)
        if target.is_file():
            target = target.parent
        if target.is_dir():
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
            except OSError:
                pass

    def _reload_rules(self) -> None:
        if self.on_reload_rules is not None:
            self.on_reload_rules()

    def _reset_rules(self) -> None:
        if self.on_reset_rules is None:
            return
        if ask_yes_no(self.win, "恢复默认规则", "会用内置默认规则覆盖当前规则文件，确定吗？"):
            self.on_reset_rules()

    # -- 结果 ---------------------------------------------------------------

    def _confirm(self) -> None:
        cfg = self.config
        backup_root = self.var_backup.get().strip()
        if not backup_root:
            messagebox.showwarning("需要备份目录", "备份根目录不能为空。", parent=self.win)
            return
        cfg.backup_root = str(normalize(backup_root))
        cfg.close_action = self.var_close.get()
        cfg.auto_start_monitor = bool(self.var_auto.get())
        cfg.tray_notifications = bool(self.var_notify.get())
        cfg.library_roots = self.editor_lib.get()
        cfg.extra_save_dirs = self.editor_extra.get()
        cfg.scan = ScanOptions(
            compute_sha256=bool(self.var_sha.get()),
            compare_mtime=bool(self.var_mtime.get()),
            hash_max_mb=int(self.var_maxhash.get() or 0),
            max_depth=int(self.var_maxdepth.get() or 5),
            max_dirs=cfg.scan.max_dirs,
            sniff_max_depth=max(1, int(self.var_sniff.get() or 3)),
            recent_days=max(0, int(self.var_recent.get() or 0)),
            discover_system=bool(self.var_sysscan.get()),
            manifest_path=self.var_manifest.get().strip(),
            max_save_paths=max(1, int(self.var_maxpaths.get() or 5)),
            # --- 检测来源开关 ---
            detect_steam=self.var_src["steam"].get(),
            find_registry_games=self.var_src["registry_games"].get(),
            find_shortcuts=self.var_src["shortcuts"].get(),
            find_recent_used=self.var_src["recent_used"].get(),
            scan_install_subdirs=self.var_src["install_subdirs"].get(),
            scan_install_root=self.var_src["install_root"].get(),
            scan_config_files=self.var_src["config_files"].get(),
            sniff_install_dir=self.var_src["sniff_install"].get(),
            sniff_sibling_dirs=self.var_src["sniff_siblings"].get(),
            scan_user_save_locations=self.var_src["user_locations"].get(),
            steam_cloud_saves=self.var_src["steam_cloud"].get(),
            registry_save_search=self.var_src["registry_saves"].get(),
        )
        cfg.monitor = MonitorOptions(
            debounce_seconds=int(self.var_debounce.get() or 0),
            poll_interval_seconds=int(self.var_poll.get() or 5),
            backend=self.var_backend.get(),
        )
        cfg.retention = RetentionPolicy(
            mode=self.var_ret_mode.get(),
            count=int(self.var_ret_count.get() or 30),
            days=int(self.var_ret_days.get() or 30),
        )

        # ★ 安全警告：备份根目录 / 数据根目录如果落在某个游戏库根目录里面，
        #   扫描会把自己的备份当成游戏/存档再扫一遍（备份套备份，体积指数增长）。
        risky: List[str] = []
        for lib in cfg.library_roots:
            for label, pr in (("备份根目录", cfg.backup_root), ("总数据根目录", self.data_root)):
                if not pr:
                    continue
                try:
                    if is_within(pr, lib):
                        risky.append(f"· {label} {pr}\n  落在游戏库根目录 {lib} 里面")
                except (OSError, ValueError):
                    continue
        if risky:
            if not ask_yes_no(
                self.win, "备份目录位置有风险",
                "检测到下面的位置关系，会让程序把自己的备份当成游戏/存档再扫一遍\n"
                "（“备份套备份”，体积会指数增长，也可能把磁盘写满）：\n\n"
                + "\n".join(dict.fromkeys(risky))
                + "\n\n建议：把「备份根目录」和「总数据根目录」放到游戏库之外"
                  "（例如 D:\\GalgameGuard 和 E:\\GalgameBackups）。\n\n"
                  "仍然保存这个设置吗？（程序会加保护，跳过备份目录里的内容，但最好还是挪出去）",
                parent=self.win,
            ):
                return

        self.result = cfg
        self.win.grab_release()
        self.win.destroy()

    def _cancel(self) -> None:
        self.result = None
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()

    def show(self) -> Optional[AppConfig]:
        self.win.wait_window()
        return self.result
