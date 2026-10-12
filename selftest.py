#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端自检脚本（无界面）。

用途：在不打开 GUI 的情况下，验证核心链路是否正常：

    1. 路径沙盒（PathGuard）确实拒绝越界写入
    2. temp 目录被重定向到 <数据根目录>\\temp
    3. 游戏库扫描能识别出候选游戏与存档路径
    4. 基线建立 / 差异比较（新增、修改、删除）正确
    5. 快照备份生成 manifest.json 与 原始存档路径.txt，文件完整
    6. 保留策略按“最近 N 份”正确清理
    7. 轮询监控能检测到存档变化并触发回调

运行::

    python -B selftest.py

脚本只在 ``<本脚本目录>\\_selftest_run`` 下创建测试数据，结束后自动删除，
不会触碰你的真实游戏或存档目录。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from galguard import bootstrap                                    # noqa: E402
from galguard.backup import BackupManager                          # noqa: E402
from galguard.config import AppConfig, ConfigStore, ScanOptions      # noqa: E402
from galguard.db import HistoryDB                                  # noqa: E402
from galguard.models import GameEntry, make_game_id                # noqa: E402
from galguard.monitor import SaveMonitor, WATCHDOG_AVAILABLE       # noqa: E402
from galguard.paths import GUARD, SandboxViolation                 # noqa: E402
from galguard.rules import RuleSet                                 # noqa: E402
from galguard.scanner import GameScanner                           # noqa: E402

PASS: list = []
FAIL: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------------------

def build_fake_game(library: Path) -> Path:
    """造一个假的 Ren'Py 游戏目录。"""
    game = library / "FakeGalgame"
    (game / "renpy").mkdir(parents=True, exist_ok=True)
    (game / "game").mkdir(parents=True, exist_ok=True)
    (game / "save").mkdir(parents=True, exist_ok=True)

    (game / "FakeGalgame.exe").write_bytes(b"MZ" + b"\x00" * 2048)
    (game / "game" / "script.rpyc").write_bytes(b"renpy-bytecode")
    (game / "game" / "data.rpa").write_bytes(b"RPA-3.0 archive")
    for i in (1, 2):
        (game / "save" / f"save{i}.sav").write_bytes(f"save-slot-{i}".encode() * 16)
    return game


def _build_messy_library(library: Path) -> None:
    """造一个“自己下的资源”风格的库，专门覆盖各种容易漏检的目录结构。"""
    exe = b"MZ" + b"\x00" * 1024

    # ① 合集目录（本身没有 exe），里面有多个游戏
    pack = library / "汉化gal合集"
    a = pack / "游戏A 完整版"
    (a / "save").mkdir(parents=True, exist_ok=True)
    (a / "游戏A.exe").write_bytes(exe)
    (a / "save" / "save1.sav").write_bytes(b"A" * 64)
    (a / "save" / "save2.dat").write_bytes(b"A" * 32)

    # ② exe 藏在通用名目录 bin\ 里
    b = pack / "游戏B"
    (b / "bin").mkdir(parents=True, exist_ok=True)
    (b / "data").mkdir(parents=True, exist_ok=True)
    (b / "bin" / "游戏B.exe").write_bytes(exe)
    (b / "bin" / "kirikiri2.dll").write_bytes(b"dll")

    # ③ 只有 launcher.exe（会被 install_exe_exclude 命中）
    c = pack / "游戏C"
    (c / "data").mkdir(parents=True, exist_ok=True)
    (c / "launcher.exe").write_bytes(exe)

    # ④ exe 藏在 GameData\ 里，存档在 GameData\savedata\
    d = library / "单机游戏D" / "GameData"
    (d / "savedata").mkdir(parents=True, exist_ok=True)
    (d / "游戏D.exe").write_bytes(exe)
    (d / "savedata" / "save01.sav").write_bytes(b"D" * 48)

    # ⑤ 安装目录本身就是存档目录（NScripter 风格）
    e = library / "游戏E"
    e.mkdir(parents=True, exist_ok=True)
    (e / "游戏E.exe").write_bytes(exe)
    (e / "nscript.dat").write_bytes(b"nscripter")
    (e / "save01.dat").write_bytes(b"E" * 40)


def main() -> int:
    here = Path(__file__).resolve().parent
    root = here / "_selftest_run"

    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    data_root = root / "data"
    backup_root = root / "backups"
    library = root / "library"
    data_root.mkdir(parents=True)
    backup_root.mkdir(parents=True)
    library.mkdir(parents=True)
    build_fake_game(library)

    print("=" * 66)
    print("Galgame 存档守护 —— 端到端自检")
    print("=" * 66)
    print(f"watchdog 可用：{WATCHDOG_AVAILABLE}")
    print(f"测试目录：{root}")
    print()

    try:
        # ------------------------------------------------------------------
        print("[1] 路径沙盒")
        GUARD.set_data_root(data_root)
        GUARD.set_backup_root(backup_root)

        blocked = []
        for target in (
            Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "galguard_should_not_exist.txt",
            Path(os.environ.get("APPDATA", "C:/nope")) / "galguard_should_not_exist.json",
            here / "galguard_should_not_exist.txt",
        ):
            try:
                GUARD.check_write(target)
                blocked.append((str(target), "未被拦截"))
            except SandboxViolation:
                pass
        check("越界写入被拦截（TEMP / APPDATA / 程序目录）", not blocked, str(blocked))

        try:
            GUARD.check_write(data_root / "ok.txt")
            inside_ok = True
        except SandboxViolation:
            inside_ok = False
        check("数据根目录内写入被放行", inside_ok)

        # 未解锁状态必须拒绝一切写入
        from galguard.paths import PathGuard

        fresh = PathGuard()
        try:
            fresh.check_write(Path("C:/whatever.txt"))
            locked_ok = False
        except SandboxViolation:
            locked_ok = True
        check("未指定数据根目录时拒绝一切写入", locked_ok)

        # ------------------------------------------------------------------
        print("[2] 临时目录重定向")
        tmp = bootstrap.redirect_temp_dir(data_root)
        check("tempfile.gettempdir() 指向 <数据根目录>\\temp",
              Path(tempfile.gettempdir()).resolve() == tmp.resolve(),
              f"实际 {tempfile.gettempdir()}")
        check("TEMP/TMP 环境变量已重定向",
              os.environ.get("TEMP") == str(tmp) and os.environ.get("TMP") == str(tmp))

        # ------------------------------------------------------------------
        print("[3] 配置读写")
        store = ConfigStore(GUARD)
        cfg = AppConfig(
            library_roots=[str(library)],
            backup_root=str(backup_root),
            extra_save_dirs=[],
        )
        cfg.scan.scan_user_save_locations = False   # 自检时不去碰真实用户目录
        cfg.scan.detect_steam = False
        cfg.scan.discover_system = False             # 自检时不去读真实注册表/快捷方式
        cfg.scan.registry_save_search = False
        store.save(cfg)
        check("config.json 已写入数据根目录", store.path.is_file(), str(store.path))
        cfg2 = store.load()
        check("配置可正确回读", cfg2.library_roots == [str(library)])

        # ------------------------------------------------------------------
        print("[4] 游戏库扫描")
        rules = RuleSet.load(GUARD)
        check("默认规则文件已生成",
              (data_root / "rules" / "detect_rules.json").is_file())

        scanner = GameScanner(rules, cfg2, GUARD)
        result = scanner.scan()
        print(f"      {result.summary()}")
        check("扫描到 1 个候选游戏", len(result.candidates) == 1,
              f"实际 {len(result.candidates)}")
        cand = result.candidates[0] if result.candidates else None
        if cand:
            check("引擎识别为 Ren'Py", cand.engine == "Ren'Py", f"实际 {cand.engine!r}")
            check("推断出存档路径 <安装目录>\\save",
                  any(Path(p).name.lower() == "save" for p in cand.save_paths),
                  str(cand.save_paths))

        # ------------------------------------------------------------------
        print("[5] 基线 / 差异比较 / 快照备份")
        db = HistoryDB(GUARD)
        db.connect()
        backups = BackupManager(GUARD, cfg2, db, rules, scanner)

        game = GameEntry(
            id=make_game_id(cand.name, cand.install_dir) if cand else "x",
            name=cand.name if cand else "FakeGalgame",
            engine=cand.engine if cand else "Ren'Py",
            exe=cand.exe if cand else "",
            install_dir=cand.install_dir if cand else str(library / "FakeGalgame"),
            save_paths=cand.save_paths if cand else [str(library / "FakeGalgame" / "save")],
        )
        cfg2.games.append(game)
        store.save(cfg2)

        n = backups.update_baseline(game)
        check("基线记录了 2 个存档文件", n == 2, f"实际 {n}")

        diff = backups.diff_game(game)
        check("刚建完基线时无更新", not diff.is_changed, diff.summary())

        save_dir = Path(game.save_paths[0])
        (save_dir / "save1.sav").write_bytes(b"CHANGED-CONTENT" * 32)
        time.sleep(1.1)
        (save_dir / "save3.sav").write_bytes(b"brand-new-slot")
        diff = backups.diff_game(game)
        check("检测到 1 个修改", len(diff.modified) == 1, f"实际 {len(diff.modified)}")
        check("检测到 1 个新增", len(diff.added) == 1, f"实际 {len(diff.added)}")

        res = backups.create_snapshot(game, trigger="manual")
        check("快照创建成功", bool(res.get("ok")), res.get("message", ""))
        snap = Path(res.get("snapshot_path", ""))
        check("快照目录形如 <备份根>\\<游戏名>\\<时间戳>",
              snap.is_dir() and snap.parent.parent.resolve() == backup_root.resolve(),
              str(snap))
        check("manifest.json 存在", (snap / "manifest.json").is_file())
        check("原始存档路径.txt 存在", (snap / "原始存档路径.txt").is_file())
        check("快照内含 3 个存档副本", res.get("file_count") == 3, str(res.get("file_count")))

        import json as _json

        manifest = _json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
        check("manifest 记录了原始存档路径",
              manifest.get("origin_save_paths") == game.save_paths_norm(),
              str(manifest.get("origin_save_paths")))
        check("manifest 记录了 SHA256",
              any(f.get("sha256") for f in manifest.get("files", [])))
        note = (snap / "原始存档路径.txt").read_text(encoding="utf-8-sig")
        check("原始存档路径.txt 写出了来源路径",
              game.save_paths_norm()[0] in note)
        copied = list((snap / "files").rglob("*.sav"))
        check("存档文件被完整复制", len(copied) == 3, f"实际 {len(copied)}")

        diff = backups.diff_game(game)
        check("备份后基线被刷新，重新变为“无更新”", not diff.is_changed, diff.summary())

        # 删除一个存档文件 → 应被识别为删除
        (save_dir / "save3.sav").unlink()
        diff = backups.diff_game(game)
        check("检测到 1 个删除", len(diff.removed) == 1, f"实际 {len(diff.removed)}")
        backups.update_baseline(game)

        # ------------------------------------------------------------------
        print("[6] 保留策略")
        cfg2.retention.mode = "count"
        cfg2.retention.count = 1
        time.sleep(1.1)
        backups.create_snapshot(game, trigger="manual")
        remaining = backup_count = None
        rows = db.list_backups(game.id, limit=100)
        backup_count = len(rows)
        remaining = [p for p in (backup_root / game.safe_folder_name()).iterdir() if p.is_dir()]
        check("保留策略把快照数限制为 1 份",
              len(remaining) == 1 and backup_count == 1,
              f"目录 {len(remaining)} 条 / 数据库 {backup_count} 条")
        cfg2.retention.mode = "none"

        # ------------------------------------------------------------------
        print("[7] 轮询监控")
        fired = threading.Event()

        monitor = SaveMonitor(
            rules=rules,
            on_change=lambda gid: fired.set(),
            debounce_seconds=1,
            poll_interval_seconds=2,
            backend="polling",
        )
        monitor.start([game])
        check("监控已启动（轮询后端）", monitor.is_running and monitor.backend == "polling",
              monitor.status_text())
        time.sleep(0.4)
        (save_dir / "save1.sav").write_bytes(b"MONITOR-TRIGGER" * 8)
        got = fired.wait(timeout=12.0)
        check("存档变化被监控捕获并触发回调", got)
        monitor.stop()
        check("监控已完全停止", not monitor.is_running)

        # ------------------------------------------------------------------
        print("[8] 后台任务池（守护线程）")
        from galguard.tasks import DaemonPool

        pool = DaemonPool(workers=2)
        box = {}
        ev = threading.Event()

        def job() -> str:
            return "ok"

        pool.submit(job, name="selftest", on_done=lambda r: (box.update(result=r), ev.set()))
        check("后台任务执行并回调", ev.wait(timeout=5.0) and box.get("result") == "ok")
        pool.stop(drain=True, timeout=2.0)
        check("任务池停止后不再有工作线程",
              all(not t.is_alive() for t in pool._threads))

        # ------------------------------------------------------------------
        print("[9] 界面装配（创建后立即销毁，不显示窗口）")
        gui_ok = None
        gui_err = ""
        try:
            from galguard.service import AppService
            from galguard.ui_main import MainWindow

            service = AppService(GUARD, store, cfg2, db, rules)
            win = MainWindow(service, on_quit=lambda code: None)
            win.withdraw()
            win.refresh_tree()
            win.append_log("自检：界面装配正常")
            win.update_idletasks()
            win.destroy()
            gui_ok = True
        except Exception as exc:  # noqa: BLE001
            gui_ok = False
            gui_err = f"{type(exc).__name__}: {exc}"
        if gui_ok is None:
            print("      SKIP：无可用图形环境")
        else:
            check("主窗口 + 服务层可正常装配", bool(gui_ok), gui_err)

        # ------------------------------------------------------------------
        print("[10] 系统托盘")
        try:
            from galguard.tray import TRAY_AVAILABLE, TrayIcon

            if not TRAY_AVAILABLE:
                print("      SKIP：未安装 pystray/Pillow（程序会自动降级为无托盘模式）")
            else:
                tray = TrayIcon(
                    title="Galgame 存档守护 自检",
                    on_show=lambda: None,
                    on_hide=lambda: None,
                    on_backup_all=lambda: None,
                    on_toggle_monitor=lambda: None,
                    on_open_data_root=lambda: None,
                    on_quit=lambda: None,
                    is_monitoring=lambda: True,
                    is_paused=lambda: False,
                    status_text=lambda: "自检",
                )
                started = tray.start()
                check("托盘图标创建并真正显示（visible=True）", bool(started) and tray.running,
                      f"started={started} running={tray.running}")
                tray.set_tooltip("自检中")
                tray.stop()
                check("托盘图标已销毁", not tray.running and not tray.icon_visible)
        except Exception as exc:  # noqa: BLE001
            check("托盘功能正常", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        # 这一节专门防止“双击没反应 / 按钮点了没反应”：
        #   · 控件用了不支持的选项 → 构造时抛 TclError
        #   · 回调里引用了不存在的属性（例如 FirstRunDialog 只有 self.root，
        #     回调却写成 self.win）→ 点击时抛 AttributeError，窗口模式下完全看不见
        # 所以这里不光要“构造”，还要**真的把回调点一遍**。
        print("[11] 各对话框装配 + 回调实际执行")
        try:
            import tkinter as tk

            from galguard.ui_dialogs import (
                CandidateDialog,
                FirstRunDialog,
                ManualGameDialog,
                SettingsDialog,
            )

            # ---- FirstRunDialog 自带 Tk 根窗口（双击启动必经路径）----
            d1 = FirstRunDialog(
                pointer_path=data_root / "galguard.root.json",
                suggested_root=str(data_root),
            )
            d1.root.update_idletasks()

            # 回归测试：扫描范围编辑器必须真的能加行、删行、取值
            ed = d1.editor_lib
            try:
                _ = len(ed._rows)
                ed.add_row(r"D:\FakeGAL_1")
                ed.add_row(r"E:\FakeGAL_2")
                got = ed.get()
                _f, _v = ed._rows[-1]
                ed._remove_row(_f, _v)                     # 删除最后一行
                ed._ensure_blank_row()                     # 自动补空行
                ed.set([r"X:\GAL_A", r"X:\GAL_B", ""])     # 覆盖设置（含空行）
                got2 = ed.get()
                editor_ok = (
                    any("FakeGAL_1" in x for x in got)
                    and any("FakeGAL_2" in x for x in got)
                    and got2 == ["X:\\GAL_A", "X:\\GAL_B"]
                )
                editor_err = f"got={got} got2={got2}"
            except Exception as exc:  # noqa: BLE001
                editor_ok, editor_err = False, f"{type(exc).__name__}: {exc}"
            check("首启向导的「扫描范围」编辑器可加行/删行/取值", editor_ok, editor_err)

            # 回归测试：窗口内容不能把底部按钮顶出可视区域
            # （曾经的 bug：固定 720x620 且不可缩放，内容更高 → 「开始使用」被裁掉，点不到）
            d1.root.update_idletasks()
            d1.root.update()
            try:
                b = d1.btn_start
                got_h = b.winfo_height()
                want_h = b.winfo_reqheight()
                bottom = b.winfo_rooty() + got_h
                limit = d1.root.winfo_rooty() + d1.root.winfo_height()
                fits_ok = (
                    b.winfo_ismapped()
                    and got_h >= want_h
                    and bottom <= limit + 2
                )
                fits_err = (f"按钮高度={got_h}/需要{want_h} 按钮底部={bottom} "
                            f"窗口底部={limit} mapped={bool(b.winfo_ismapped())}")
            except Exception as exc:  # noqa: BLE001
                fits_ok, fits_err = False, f"{type(exc).__name__}: {exc}"
            check("首启向导的「开始使用」按钮完整可见（没被内容顶出窗口）", fits_ok, fits_err)

            # 回归测试：带着扫描范围真正走一遍提交（曾经这里会抛 AttributeError）
            try:
                d1.editor_lib.set([str(library)])
                d1.var_data_root.set(str(data_root))
                d1._confirm()          # 内部会 destroy 掉自己的 root
                submit_ok = bool(d1.result) and d1.result.get("library_roots") == [str(library)]
                submit_err = str(d1.result)
            except Exception as exc:  # noqa: BLE001
                submit_ok, submit_err = False, f"{type(exc).__name__}: {exc}"
                try:
                    d1.root.destroy()
                except Exception:
                    pass
            check("首启向导能带着扫描范围正常提交", submit_ok, submit_err)

            root2 = tk.Tk()
            root2.withdraw()

            d2 = ManualGameDialog(root2, default_backup_root=str(backup_root),
                                  data_root=str(data_root), rules=rules)
            d2.win.update_idletasks()
            # 回归测试：手动添加对话框里的“自动识别引擎”按钮要能真的执行
            try:
                d2.var_install.set(str(library / "FakeGalgame"))
                d2._detect_engine()
                engine_ok = d2.var_engine.get() == "Ren'Py"
                engine_err = f"识别结果={d2.var_engine.get()!r} {d2.var_engine_text.get()}"
            except Exception as exc:  # noqa: BLE001
                engine_ok, engine_err = False, f"{type(exc).__name__}: {exc}"
            check("手动添加：引擎由程序自动识别（不需要用户填）", engine_ok, engine_err)
            d2.win.destroy()

            d3 = SettingsDialog(root2, cfg2, str(data_root),
                                rules_path=str(data_root / "rules" / "detect_rules.json"))
            d3.win.update_idletasks()
            # 设置页的扫描范围编辑器同样要能用
            try:
                d3.editor_lib.add_row(r"Z:\SomeGAL")
                d3.editor_lib.set([str(library)])
                set_ok = d3.editor_lib.get() == [str(library)]
                set_err = str(d3.editor_lib.get())
            except Exception as exc:  # noqa: BLE001
                set_ok, set_err = False, f"{type(exc).__name__}: {exc}"
            check("设置页的「扫描范围」编辑器可用", set_ok, set_err)
            d3.win.destroy()

            d4 = CandidateDialog(root2, ([cand] if cand else []), [], "自检", rules=rules)
            d4.win.update_idletasks()
            # 候选窗口的筛选 / 只看有存档 / 重命名要能真的执行
            try:
                d4.var_filter.set("xyz-不存在的关键字")
                d4._refresh()
                d4.var_filter.set("")
                d4.var_only_saves.set(True)
                d4._refresh()
                d4.var_only_saves.set(False)
                d4._set_all(True)
                d4._select_high()
                d4._set_all(False)
                cand_ok, cand_err = True, ""
            except Exception as exc:  # noqa: BLE001
                cand_ok, cand_err = False, f"{type(exc).__name__}: {exc}"
            check("候选窗口的筛选 / 只看有存档 / 全选 / 高置信度按钮可用", cand_ok, cand_err)
            d4.win.destroy()

            root2.destroy()
            dialog_ok, dialog_err = True, ""
        except Exception as exc:  # noqa: BLE001
            dialog_ok, dialog_err = False, f"{type(exc).__name__}: {exc}"
        check("首启向导 / 手动添加 / 设置 / 候选确认 均能正常构造", dialog_ok, dialog_err)

        # ------------------------------------------------------------------
        # 这一节专门模拟“自己下的资源”：合集目录、exe 藏在 bin/GameData 里、
        # 只有 launcher.exe、存档直接躺在安装目录下……这些是最容易漏检的结构。
        print("[12] 自下资源目录结构识别")
        lib2 = root / "library2"
        _build_messy_library(lib2)

        cfg3 = AppConfig(library_roots=[str(lib2)], backup_root=str(backup_root), extra_save_dirs=[])
        cfg3.scan.scan_user_save_locations = False   # 自检时不碰真实用户目录
        cfg3.scan.detect_steam = False
        cfg3.scan.discover_system = False
        cfg3.scan.registry_save_search = False
        cfg3.scan.sniff_max_depth = 4
        res3 = GameScanner(rules, cfg3, GUARD).scan()
        by_name = {c.name: c for c in res3.candidates}
        print(f"      识别到 {len(res3.candidates)} 个：{sorted(by_name)}")

        check("合集目录下的游戏能被逐个识别（不再被父目录剪枝）",
              "游戏A 完整版" in by_name, str(sorted(by_name)))
        check("exe 藏在 bin\\ 里也能识别，且游戏名取上一层目录",
              "游戏B" in by_name and "bin" not in by_name, str(sorted(by_name)))
        check("只有 launcher.exe 的目录也能识别（不再被 exclude 规则漏掉）",
              "游戏C" in by_name, str(sorted(by_name)))
        check("exe 藏在 GameData\\ 里也能识别，且游戏名取上一层目录",
              "单机游戏D" in by_name and "GameData" not in by_name, str(sorted(by_name)))
        check("安装目录本身就是存档目录的情况能识别出存档路径",
              "游戏E" in by_name and bool(by_name["游戏E"].save_paths),
              str(by_name["游戏E"].save_paths) if "游戏E" in by_name else "未识别")

        if "游戏A 完整版" in by_name:
            sa = [Path(p).name.lower() for p in by_name["游戏A 完整版"].save_paths]
            check("游戏A 的 save\\ 被认作存档目录", "save" in sa, str(sa))
        if "单机游戏D" in by_name:
            sd = [Path(p).name.lower() for p in by_name["单机游戏D"].save_paths]
            check("游戏D 子目录里的 savedata\\ 被嗅探到", "savedata" in sd, str(sd))
        if "游戏E" in by_name and by_name["游戏E"].save_paths:
            check("游戏E 的存档路径 = 安装目录本身",
                  Path(by_name["游戏E"].save_paths[0]) == Path(by_name["游戏E"].install_dir))

        # ------------------------------------------------------------------
        # 系统痕迹发现 + Ludusavi 离线数据库
        print("[13] 系统发现与 Ludusavi 数据库")
        try:
            from galguard.manifest import LudusaviManifest
            from galguard import systemscan

            fake_game = library / "FakeGalgame"
            like, why = systemscan.looks_like_game_dir(fake_game, rules)
            check("“像不像游戏目录”判定：Ren'Py 假游戏被判为游戏", like, why)

            # --- 造一份迷你 manifest.yaml，验证解析与占位符展开 ---
            mf_path = root / "mini_manifest.yaml"
            mf_path.write_text(
                '"FakeGalgame":\n'
                '  files:\n'
                '    "<base>/save": {}\n'
                '    "<winAppData>/RenPy/FakeGalgame":\n'
                '      when:\n'
                '        - os: windows\n'
                '  installDir:\n'
                '    "<base>": {}\n'
                '"另外一款游戏":\n'
                '  files:\n'
                '    "<home>/Documents/另外一款游戏": {}\n',
                encoding="utf-8",
            )
            mf = LudusaviManifest()
            check("Ludusavi manifest 解析成功", mf.load(mf_path), "；".join(mf.errors))
            check("解析出 2 个游戏条目", len(mf) == 2, f"实际 {len(mf)}")
            check("按游戏名能查到条目", mf.lookup("FakeGalgame") is not None)

            paths, why2 = mf.save_paths_for("FakeGalgame", str(fake_game), extra_roots=[str(library)])
            check("占位符 <base> 展开到真实存在的存档目录",
                  any(Path(p) == fake_game / "save" for p in paths), str(paths))
            check("when 条件块没有被误当成路径条目",
                  all("when" not in p.lower() for p in paths), str(paths))
            check("中文游戏名条目也能解析", mf.lookup("另外一款游戏") is not None)

            # --- 注册表读取不应崩溃（只读，且这里只验证可用性） ---
            try:
                idx = systemscan.build_registry_uninstall_index(limit=50)
                check("注册表卸载项可读取（只读）", isinstance(idx, list))
            except Exception as exc:  # noqa: BLE001
                check("注册表卸载项可读取（只读）", False, f"{type(exc).__name__}: {exc}")

            check("不存在的快捷方式不会崩", systemscan.exe_from_lnk(root / "nope.lnk") is None)
        except Exception as exc:  # noqa: BLE001
            check("系统发现 / Ludusavi 模块可用", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        print("[14] 游戏列表多选 / 批量操作")
        try:
            # 注意：GameEntry 在文件顶部已经导入，这里**不要**再写局部 import，
            #       否则会让它变成局部变量，前面的代码就访问不到了。
            from galguard.service import AppService
            from galguard.ui_main import MainWindow

            for i in range(3):
                d = root / f"multi{i + 1}"
                d.mkdir(parents=True, exist_ok=True)
                cfg2.games.append(GameEntry(
                    name=f"多选测试{i + 1}", install_dir=str(d), save_paths=[str(d / "save")]
                ))

            svc = AppService(GUARD, store, cfg2, db, rules)
            w = MainWindow(svc, on_quit=lambda code: None)
            w.withdraw()
            w.refresh_tree()
            w.update_idletasks()

            ids = list(w.tree.get_children())
            w.set_all_checked(True)
            n_all = len(w.checked_games())
            sel_marks = all(w.tree.set(i, "sel") == "☑" for i in ids)

            w.toggle_checked(ids[0])
            n_after = len(w.checked_games())

            w.invert_checked()
            n_inv = len(w.checked_games())

            w.set_all_checked(False)
            n_none = len(w.checked_games())

            # 一个都没勾时，回退到“高亮选中”
            w.tree.selection_set(ids[1])
            fallback = [g.name for g in w.target_games()]

            # 勾选优先于高亮
            w.set_all_checked(True)
            prefer = len(w.target_games())

            has_backup_all = hasattr(w, "backup_all_games") and hasattr(svc, "backup_all")

            w.destroy()

            ok = (
                n_all == len(ids) and sel_marks and n_after == len(ids) - 1
                and n_inv == 1 and n_none == 0 and prefer == len(ids)
                and bool(fallback) and has_backup_all
            )
            check("游戏列表支持任意多选（☑ 列 / 全选 / 反选 / 高亮回退）", ok,
                  f"all={n_all} marks={sel_marks} after={n_after} inv={n_inv} "
                  f"none={n_none} prefer={prefer}/{len(ids)} fallback={fallback}")
            check("「备份所有游戏」入口存在", has_backup_all)
        except Exception as exc:  # noqa: BLE001
            check("游戏列表多选可用", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        # 这一节专门盯“存档路径识别太弱”：
        # 补丁目录被当存档、嵌套目录重复识别、一个游戏给出十几个路径。
        print("[15] 存档路径识别精度")
        try:
            from galguard import detect as D

            lib3 = root / "library3"
            g = lib3 / "精测游戏"
            (g / "renpy").mkdir(parents=True, exist_ok=True)
            (g / "save" / "2024存档").mkdir(parents=True, exist_ok=True)
            (g / "data").mkdir(parents=True, exist_ok=True)
            (g / "汉化补丁").mkdir(parents=True, exist_ok=True)
            (g / "精测游戏.exe").write_bytes(b"MZ" + b"\x00" * 512)

            (g / "save" / "save01.sav").write_bytes(b"S" * 32)              # 真存档（强特征）
            (g / "save" / "2024存档" / "save01.sav").write_bytes(b"S" * 32)   # 嵌套副本
            (g / "data" / "config.dat").write_bytes(b"C" * 16)               # 只有 1 个弱特征
            (g / "汉化补丁" / "save99.sav").write_bytes(b"P" * 16)            # 补丁目录

            ext = lib3 / "外部存档"
            ext.mkdir(parents=True, exist_ok=True)
            (ext / "save02.sav").write_bytes(b"E" * 16)
            # 游戏自己的配置文件里写明了存档在哪 —— 这是精度最高的一类线索
            (g / "game.ini").write_text(f"save_path={ext}\n", encoding="utf-8")

            paths, why = D.infer_save_paths(
                g, "Ren'Py", "精测游戏", rules,
                scan_user_locations=False, sniff_max_depth=3, max_paths=5,
            )
            names = [Path(p).name for p in paths]

            check("识别出真正的存档目录 save\\", "save" in names, f"识别到 {names}")
            check("采纳了游戏配置文件里写明的存档路径", "外部存档" in names, f"识别到 {names}")
            check("嵌套目录已折叠（save\\ 与 save\\2024存档 不会同时出现）",
                  "2024存档" not in names, f"识别到 {names}")
            check("「汉化补丁」这类补丁目录被排除",
                  "汉化补丁" not in names, f"识别到 {names}")
            check("只有 1 个 config.dat 的 data\\ 不被当成存档目录",
                  "data" not in names, f"识别到 {names}")

            p1, _w1 = D.infer_save_paths(
                g, "Ren'Py", "精测游戏", rules,
                scan_user_locations=False, sniff_max_depth=3, max_paths=1,
            )
            check("路径数量上限生效（max_paths=1 → 只给 1 个）", len(p1) == 1, str(p1))

            check("被舍弃的候选会写进判定依据里备查",
                  any("舍弃" in w for w in why), "\n".join(why[:6]))
        except Exception as exc:  # noqa: BLE001
            check("存档识别精度测试可运行", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        # 用户实测反馈的两个坑：
        #   ① 补丁目录被当成“游戏”（免DVD补丁备份 / 官方修正补丁备份 / 汉化补丁）
        #   ② 安装目录叫 "PC" 时，%LOCALAPPDATA% 里凡是含 "pc" 的目录全被当成存档
        print("[16] 补丁目录排除 + 名字匹配精度")
        try:
            from galguard import detect as D

            # --- ① 补丁目录名判定（既不能漏判，也不能误伤正经游戏目录）---
            should_exclude = [
                "免DVD补丁备份", "官方修正补丁备份", "汉化补丁备份", "补丁",
                "patch", "patch_v1.2", "backup", "备份",
                "Downloads", "Packages", "temp", "readme",
            ]
            should_keep = [
                "樱花，萌放 v1.08(已打补丁).7z", "樱花，萌放 v1.1(已打补丁)",
                "我们是不能延期了", "Patchwork", "HANA1 花吻在上", "哀鸿十日夜",
            ]
            missed = [n for n in should_exclude if not rules.is_excluded_dir_name(n)]
            hurt = [n for n in should_keep if rules.is_excluded_dir_name(n)]
            check("补丁 / 备份 / 系统垃圾目录名能被识别出来", not missed, f"漏判：{missed}")
            check("名字里带「已打补丁」的正经游戏目录不会被误排除", not hurt, f"误判：{hurt}")

            # --- ② 名字匹配：两字母安装目录名（PC）不能误伤 AppData 目录 ---
            pc_tokens = D._tokens("PC")
            false_hits = [
                n for n in ("TpcdMetadata", "XiaomiPCManager",
                            "Clipchamp.Clipchamp_yxz26nhyzhsrt",
                            "Microsoft.LockApp_cw5n1h2txyewy", "PCL", "downloads")
                if D.name_matches("PC", pc_tokens, n)
            ]
            check("安装目录叫「PC」时不再误命中 AppData 里含 pc 的无关目录",
                  not false_hits, f"误命中：{false_hits}")

            good_hits = [
                D.name_matches("PC", pc_tokens, "PC"),                      # 完全同名要认
                D.name_matches("dingtalk", D._tokens("dingtalk"), "DingTalk_133"),
                D.name_matches("哀鸿十日夜", D._tokens("哀鸿十日夜"), "哀鸿十日夜"),
                D.name_matches("樱花萌放", D._tokens("樱花萌放"), "樱花，萌放 v1.1"),
                D.name_matches("樱花，萌放", D._tokens("樱花，萌放"), "樱花萌放"),
            ]
            check("该匹配上的仍能匹配（含中文标点差异）", all(good_hits), str(good_hits))

            # --- ③ 真扫一遍：补丁目录不能出现在候选里，带「已打补丁」的游戏要在 ---
            lib4 = root / "library4"
            real = lib4 / "真游戏"
            (real / "免DVD补丁备份").mkdir(parents=True, exist_ok=True)
            (real / "汉化补丁").mkdir(parents=True, exist_ok=True)
            (real / "真游戏.exe").write_bytes(b"MZ" + b"\x00" * 512)
            (real / "免DVD补丁备份" / "setup.exe").write_bytes(b"MZ" + b"\x00" * 512)
            (real / "汉化补丁" / "patch.exe").write_bytes(b"MZ" + b"\x00" * 512)
            deep = lib4 / "樱花，萌放 v1.08(已打补丁).7z" / "樱花，萌放 v1.1(已打补丁)"
            deep.mkdir(parents=True, exist_ok=True)
            (deep / "game.exe").write_bytes(b"MZ" + b"\x00" * 512)

            cfg4 = AppConfig(library_roots=[str(lib4)], backup_root=str(backup_root))
            cfg4.scan.scan_user_save_locations = False
            cfg4.scan.detect_steam = False
            cfg4.scan.discover_system = False
            cfg4.scan.registry_save_search = False
            res4 = GameScanner(rules, cfg4, GUARD).scan()
            names4 = {c.name for c in res4.candidates}
            check("补丁目录不会被当成候选游戏",
                  not ({"免DVD补丁备份", "汉化补丁"} & names4), str(sorted(names4)))
            check("名字里带「已打补丁」的正经游戏目录仍能被识别",
                  any("樱花" in n for n in names4), str(sorted(names4)))
        except Exception as exc:  # noqa: BLE001
            check("补丁/名字匹配测试可运行", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        # ★ 最危险的一类 bug：程序把自己的备份当成游戏/存档再备份一遍。
        #   真出过：备份目录被扫进递归备份，体积指数增长；监控还会形成死循环。
        print("[17] 程序自己的数据/备份目录是禁区")
        try:
            orig_backup_root = GUARD.backup_root
            bm = BackupManager(GUARD, cfg2, db, rules, scanner)

            # ① 存档路径本身就在备份根目录里 → 整个跳过，一个文件都不复制
            broot = GUARD.backup_root
            nested = broot / "越界游戏" / "2024-01-01_00-00-00" / "files" / "save"
            nested.mkdir(parents=True, exist_ok=True)
            (nested / "save01.sav").write_bytes(b"X" * 32)

            g_bad = GameEntry(name="越界测试", install_dir=str(root / "_nowhere"),
                              save_paths=[str(nested)])
            r_bad = bm.create_snapshot(g_bad, trigger="manual", update_baseline=False)
            copied: list = []
            snap_bad = Path(r_bad.get("snapshot_path") or "")
            if snap_bad.is_dir():
                copied = [p for p in snap_bad.rglob("*") if p.is_file() and p.suffix == ".sav"]
            check("存档路径落在备份目录内 → 快照不复制任何存档（防止备份套备份）",
                  not copied, f"复制了 {len(copied)} 个 .sav")

            check("禁区判定：备份根/数据根本身及其子目录都被识别",
                  GUARD.is_protected(broot)
                  and GUARD.is_protected(broot / "a" / "b")
                  and GUARD.is_protected(GUARD.data_root / "logs"),
                  f"backup={broot} data={GUARD.data_root}")
            check("禁区判定：普通游戏目录不受影响",
                  not GUARD.is_protected(library)
                  and not GUARD.is_protected(library / "FakeGalgame" / "save"))

            # ② 存档目录里套着备份目录 → 闸门直接拒绝整条路径（最安全）
            outer = root / "layer_save"
            (outer / "backups" / "旧备份").mkdir(parents=True, exist_ok=True)
            (outer / "save01.sav").write_bytes(b"O" * 16)
            (outer / "backups" / "旧备份" / "save99.sav").write_bytes(b"B" * 16)
            g2 = GameEntry(name="嵌套测试", install_dir=str(outer),
                           save_paths=[str(outer)],
                           backup_root=str(outer / "backups"))
            r2 = bm.create_snapshot(g2, trigger="manual", update_baseline=False)
            snap2 = Path(r2.get("snapshot_path") or "")
            got_sav = [p.name for p in snap2.rglob("*.sav")] if snap2.is_dir() else []
            blocked2 = " ".join(r2.get("blocked_paths") or [])
            check("存档路径是备份根目录的祖先时，闸门整条拒绝（不产生递归备份）",
                  "save99.sav" not in got_sav and "save01.sav" not in got_sav
                  and "递归自备份" in blocked2,
                  f"got={got_sav} blocked={blocked2[:80]}")

            # ②b 即使把闸门关掉，**逐文件**那道防线也必须挡住旧备份
            import json as _json2

            rules_nogate = RuleSet(_json2.loads(_json2.dumps(rules.data)))
            rules_nogate.data["save_path_gate"]["enabled"] = False
            bm2 = BackupManager(GUARD, cfg2, db, rules_nogate, scanner)
            r2b = bm2.create_snapshot(g2, trigger="manual", update_baseline=False)
            snap2b = Path(r2b.get("snapshot_path") or "")
            got2 = [p.name for p in snap2b.rglob("*.sav")] if snap2b.is_dir() else []
            check("闸门关闭时，逐文件防线仍会跳过备份目录里的内容",
                  "save01.sav" in got2 and "save99.sav" not in got2, str(got2))
            GUARD.set_backup_root(orig_backup_root)

            # ③ 把备份目录当“游戏库根目录”加进来 → 直接拒绝并给出提示
            deep = GUARD.backup_root / "某游戏" / "2024-01-01_00-00-00" / "files"
            (deep / "save").mkdir(parents=True, exist_ok=True)
            (deep / "save" / "save01.sav").write_bytes(b"Y" * 16)
            (deep / "game.exe").write_bytes(b"MZ")

            cfg5 = AppConfig(library_roots=[str(GUARD.backup_root)],
                             backup_root=str(GUARD.backup_root))
            cfg5.scan.scan_user_save_locations = False
            cfg5.scan.detect_steam = False
            cfg5.scan.discover_system = False
            cfg5.scan.registry_save_search = False
            res5 = GameScanner(rules, cfg5, GUARD).scan()
            check("把备份目录当游戏库根目录会被拒绝并给出提示",
                  not res5.candidates and any("数据/备份目录" in e for e in res5.errors),
                  f"候选={len(res5.candidates)} 错误={res5.errors[:1]}")

            # ④ 备份根目录落在游戏库根目录里时，扫描要发出警告
            cfg6 = AppConfig(library_roots=[str(root)], backup_root=str(root / "backups"))
            cfg6.scan.scan_user_save_locations = False
            cfg6.scan.detect_steam = False
            cfg6.scan.discover_system = False
            cfg6.scan.registry_save_search = False
            res6 = GameScanner(rules, cfg6, GUARD).scan()
            check("备份根目录落在游戏库内时会警告（备份套备份风险）",
                  any("备份套备份" in e for e in res6.errors),
                  f"错误={res6.errors[:1]}")
        except Exception as exc:  # noqa: BLE001
            check("备份套备份防护测试可运行", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        # 准入闸门：断言全部用环境变量构造，换用户名 / 换盘符都必须成立
        # （对应交接任务书 §7.3 的用例）
        print("[18] 存档路径准入闸门")
        try:
            from galguard.pathgate import Decision, SavePathGate

            up = os.environ.get("USERPROFILE") or r"C:\Users\someone"
            la = os.environ.get("LOCALAPPDATA") or os.path.join(up, "AppData", "Local")
            ra = os.environ.get("APPDATA") or os.path.join(up, "AppData", "Roaming")

            backup_root = os.path.join(la, "GalgameSaveGuard", "backups")
            gate = SavePathGate(rules.data, backup_root=backup_root,
                                data_root=os.path.dirname(backup_root))

            cases_bad = [
                (up, "用户主目录本身"),
                (os.path.join(up, "Downloads"), "整个下载目录"),
                (os.path.join(up, "Desktop"), "桌面"),
                (os.path.join(la, "SomeGame", "logs"), "游戏目录下的 logs"),
                (la, "%LOCALAPPDATA% 本身"),
                (os.path.join(la, "Temp"), "Local\\Temp"),
                (os.path.join(la, "Packages"), "UWP 包目录"),
                (os.path.join(la, "Microsoft"), "Local\\Microsoft"),
                (os.path.join(la, "Temp", "PCL"), "启动器临时目录"),
                (os.path.join(la, "SomeApp", "EBWebView", "Default", "GPUCache"), "浏览器 GPUCache"),
                (os.path.join(la, "SomeApp", "Cache", "Cache_Data"), "缓存数据"),
                (os.path.join(la, "GalgameSaveGuard"), "备份根目录的祖先 -> 递归"),
                (os.path.join(la, "GalgameSaveGuard", "backups", "1st_beat"), "备份目录自身"),
                (os.path.join(ra, "npm-cache"), "npm 缓存"),
                ("", "空路径"),
            ]
            bad_fail = []
            for path, why in cases_bad:
                got, reason = gate.check(path)
                if got != Decision.REJECT:
                    bad_fail.append(f"{path or '<空>'} -> {got}（{why}）")
            check("闸门拒绝全部 %d 条危险路径（下载/临时/缓存/自身目录）" % len(cases_bad),
                  not bad_fail, "；".join(bad_fail[:4]))

            gal = r"E:\Galgame"
            cases_ok = [
                (os.path.join(gal, "GameA", "savedata"), "游戏库内 KiriKiri savedata"),
                (os.path.join(gal, "GameA", "userdata"), "游戏库内 BGI userdata"),
                (os.path.join(gal, "GameA", "Savcn"), "游戏库内 BGI Savcn"),
                (os.path.join(gal, "GameA", "save"), "游戏库内 save"),
                (os.path.join(gal, "sub", "GameB", "savedata_cn"), "游戏库内汉化存档"),
                (os.path.join(ra, "RenPy", "A Kiss For The Petals"), "%APPDATA%\\RenPy\\<游戏>"),
                (os.path.join(ra, "Sphere", "yosuga", "save"), "%APPDATA%\\<厂商>\\<游戏>\\save"),
                (os.path.join(ra, "SomeBrand", "SomeGame"), "%APPDATA%\\<厂商>\\<游戏>"),
                (os.path.join(up, "Saved Games", "WillPlus", "SomeGame"), "Saved Games\\<游戏>"),
                (os.path.join(la, "SomeGame"), "%LOCALAPPDATA%\\<游戏名>"),
            ]
            ok_fail = []
            for path, why in cases_ok:
                got, reason = gate.check(path)
                if got != Decision.OK:
                    ok_fail.append(f"{path} -> {got}: {reason}（{why}）")
            check("闸门放行全部 %d 条正常存档路径（Saved Games / AppData 厂商目录）"
                  % len(cases_ok), not ok_fail, "；".join(ok_fail[:4]))

            # 换用户名 / 换盘符，判定不得改变（证明没有硬编码）
            other = r"C:\Users\alice"
            fake_local = other + r"\AppData\Local"
            gate2 = SavePathGate(rules.data,
                                 backup_root=other + r"\AppData\Local\GalgameSaveGuard\backups")
            cross = [
                (other + r"\Downloads", Decision.REJECT),
                (other + r"\Downloads\gal\save", Decision.REJECT),
                (fake_local + r"\Temp", Decision.REJECT),
                (other + r"\AppData\Roaming\RenPy\SomeGame", Decision.OK),
                (r"D:\Games\SomeGame\savedata", Decision.OK),
            ]
            cross_fail = []
            for path, want in cross:
                got, reason = gate2.check(path)
                if got != want:
                    cross_fail.append(f"{path} -> {got}，期望 {want}")
            check("换用户名 / 换盘符后判定不变（无硬编码）", not cross_fail,
                  "；".join(cross_fail[:3]))

            # 体量熔断：一个超大目录要返回 CONFIRM 而不是 OK
            big = root / "huge_like"
            big.mkdir(parents=True, exist_ok=True)
            for i in range(12):
                (big / f"f{i}.bin").write_bytes(b"x" * (64 * 1024))
            gate_small = SavePathGate(
                {"save_path_gate": dict(rules.data["save_path_gate"], max_files=5, max_bytes=0)},
                backup_root=backup_root,
            )
            got, reason = gate_small.check(str(big))
            check("体量熔断：文件数超阈值时返回 CONFIRM（默认不备份）",
                  got == Decision.CONFIRM, f"{got}: {reason}")
            got_quick, _r = gate_small.check(str(big), quick=True)
            check("检测阶段用 quick=True 跳过体量统计（不拖慢扫描）",
                  got_quick == Decision.OK, got_quick)

            # ---- 任务书 §2.2 的 6 条伪造路径（现场复刻，必须全部拒绝） ----
            # 在临时目录里模拟一套“用户目录”，改环境变量后再造闸门，
            # 这样既复刻了真实场景，又不污染真机、也不依赖具体用户名。
            fake_up = root / "fake_user"
            fake_la = fake_up / "AppData" / "Local"
            fake_paths = {
                "TpcdMetadata": fake_la / "DingTalk_133" / "TpcdMetadata",
                "XiaomiPCManager": fake_la / "MI" / "XiaomiPCManager",
                "Clipchamp": fake_la / "Packages" / "Clipchamp.Clipchamp_yxz26nhyzhsrt",
                "LockApp": fake_la / "Packages" / "Microsoft.LockApp_cw5n1h2txyewy",
                "PCL": fake_la / "Temp" / "PCL",
                "Downloads": fake_up / "Downloads",
            }
            for _p in fake_paths.values():
                _p.mkdir(parents=True, exist_ok=True)
                (_p / "unrelated.dat").write_bytes(b"x" * 64)
            (fake_paths["XiaomiPCManager"] / "EBWebView" / "Default" / "GPUCache").mkdir(
                parents=True, exist_ok=True)
            (fake_paths["XiaomiPCManager"] / "EBWebView" / "Default" / "GPUCache"
             / "data_0").write_bytes(b"x" * 64)

            saved_env = {k: os.environ.get(k)
                         for k in ("USERPROFILE", "LOCALAPPDATA", "APPDATA")}
            try:
                os.environ["USERPROFILE"] = str(fake_up)
                os.environ["LOCALAPPDATA"] = str(fake_la)
                os.environ["APPDATA"] = str(fake_up / "AppData" / "Roaming")
                gate6 = SavePathGate(
                    rules.data,
                    backup_root=str(fake_la / "GalgameSaveGuard" / "backups"),
                    data_root=str(fake_la / "GalgameSaveGuard"),
                )
                fails6 = []
                for _label, _p in fake_paths.items():
                    _got, _why = gate6.check(str(_p))
                    if _got != Decision.REJECT:
                        fails6.append(f"{_label} -> {_got}（{_why or '被放行'}）")
                check("任务书 §2.2 的 6 条伪造路径被闸门全部拒绝", not fails6,
                      "；".join(fails6))

                # 同一套假的用户目录下，真实存档形状仍然必须放行
                real_up = fake_up / "Saved Games" / "WillPlus" / "SomeGame"
                real_up.mkdir(parents=True, exist_ok=True)
                for _n in ("Save000", "Save000Favorite", "Save000GS", "MessageFlag.dat"):
                    (real_up / _n).write_bytes(b"x" * 32)
                got_real, why_real = gate6.check(str(real_up))
                check("Saved Games\\<游戏>\\Save000* 这类真实存档能通过闸门",
                      got_real == Decision.OK, f"{got_real}: {why_real}")

                renpy = fake_up / "AppData" / "Roaming" / "RenPy" / "SomeGame"
                renpy.mkdir(parents=True, exist_ok=True)
                (renpy / "auto-1-LT1.save").write_bytes(b"x" * 32)
                (renpy / "persistent").write_bytes(b"x" * 32)
                got_ren, why_ren = gate6.check(str(renpy))
                check("%APPDATA%\\RenPy\\<游戏> 这类真实存档能通过闸门",
                      got_ren == Decision.OK, f"{got_ren}: {why_ren}")

                # 坏消息：一个“存在但没有任何存档证据”的 AppData 目录要被拦下
                bogus = fake_la / "SomeRandomApp"
                bogus.mkdir(parents=True, exist_ok=True)
                (bogus / "app.log").write_bytes(b"x" * 32)
                got_bad, why_bad = gate6.check(str(bogus))
                check("AppData 下存在但无存档证据的目录被拦下（无存档证据不放行）",
                      got_bad == Decision.REJECT, f"{got_bad}: {why_bad}")
            finally:
                for _k, _v in saved_env.items():
                    if _v is None:
                        os.environ.pop(_k, None)
                    else:
                        os.environ[_k] = _v
        except Exception as exc:  # noqa: BLE001
            check("准入闸门测试可运行", False, f"{type(exc).__name__}: {exc}")

        # ------------------------------------------------------------------
        # 检测来源开关：关掉某一项之后，对应的扫描阶段必须完全不执行
        print("[19] 检测来源开关")
        try:
            def _scan_with(**flags):
                """用第 [15] 节那套 fixture 扫一遍，可任意开关来源。"""
                c = AppConfig(library_roots=[str(root / "library3")],
                              backup_root=str(backup_root), extra_save_dirs=[])
                c.scan.detect_steam = False
                c.scan.discover_system = False
                c.scan.registry_save_search = False
                c.scan.scan_user_save_locations = False
                for k, v in flags.items():
                    setattr(c.scan, k, v)
                r = GameScanner(rules, c, GUARD).scan()
                return r, {Path(p).name for cc in r.candidates for p in cc.save_paths}

            _r_all, paths_all = _scan_with()
            check("来源全开时能拿到 save\\ 与游戏配置文件里的 外部存档",
                  "save" in paths_all and "外部存档" in paths_all, str(sorted(paths_all)))

            _r1, paths1 = _scan_with(scan_config_files=False)
            check("关掉「游戏配置文件」后，配置文件里的路径不再出现，其它路径不受影响",
                  "外部存档" not in paths1 and "save" in paths1, str(sorted(paths1)))

            _r2, paths2 = _scan_with(
                scan_install_subdirs=False, scan_install_root=False,
                scan_config_files=False, sniff_install_dir=False,
                sniff_sibling_dirs=False, steam_cloud_saves=False,
            )
            check("把本地存档来源全部关掉后，一条存档路径都不产生",
                  not paths2, str(sorted(paths2)))

            # 游戏来源的子开关：映射正确，且子开关全关时系统痕迹阶段整体跳过
            so = ScanOptions()
            so.find_shortcuts = False
            s = so.sources()
            check("关掉「快捷方式」不影响「注册表安装记录」与「最近使用」",
                  s.shortcuts is False and s.registry_games is True and s.recent_used is True,
                  str(s))
            so2 = ScanOptions()
            so2.discover_system = False
            s2 = so2.sources()
            check("关掉「系统痕迹发现」总开关后，注册表/快捷方式/最近使用全部失效",
                  not (s2.registry_games or s2.shortcuts or s2.recent_used), str(s2))

            c3 = AppConfig(library_roots=[], backup_root=str(backup_root))
            c3.scan.detect_steam = False
            c3.scan.discover_system = True       # 总开关开着……
            c3.scan.find_registry_games = False  # ……但三个子来源全关
            c3.scan.find_shortcuts = False
            c3.scan.find_recent_used = False
            c3.scan.registry_save_search = False
            c3.scan.scan_user_save_locations = False
            r3 = GameScanner(rules, c3, GUARD).scan()
            check("三个子来源全关时，系统痕迹阶段整体跳过（不读注册表）",
                  r3.system_found == 0 and not r3.candidates,
                  f"system_found={r3.system_found} 候选={len(r3.candidates)}")

            c4 = AppConfig(library_roots=[str(root / "library3")],
                           backup_root=str(backup_root))
            c4.scan.detect_steam = False
            c4.scan.discover_system = True
            c4.scan.find_registry_games = False
            c4.scan.find_recent_used = False
            c4.scan.registry_save_search = False
            c4.scan.scan_user_save_locations = False
            check("来源开关会写进 config.json 并原样读回",
                  AppConfig.from_dict(c4.to_dict()).scan.sources().shortcuts
                  == c4.scan.sources().shortcuts
                  and AppConfig.from_dict(c4.to_dict()).scan.find_shortcuts is True)
        except Exception as exc:  # noqa: BLE001
            check("检测来源开关测试可运行", False, f"{type(exc).__name__}: {exc}")

        db.close()

        # ------------------------------------------------------------------
        print()
        print("=" * 66)
        print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
        if FAIL:
            for name in FAIL:
                print(f"  × {name}")
        print("=" * 66)
        return 0 if not FAIL else 1

    finally:
        # 清理测试目录（同时验证临时文件都落在测试目录内）
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
