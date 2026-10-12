# -*- coding: utf-8 -*-
"""游戏扫描器：库目录扫描、Steam 扫描、基线建立。

只读操作。所有耗时工作都设计成可被 ``cancel``（``threading.Event``）中断，
以便“彻底退出”时能迅速停下来。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from . import detect
from .config import AppConfig
from .fsutil import iter_save_files, sha256_file
from .logsetup import get_logger
from .models import Candidate, FileStamp, ScanResult
from .paths import PathGuard, is_protected, is_within, path_key, normalize
from .rules import RuleSet

__all__ = ["GameScanner"]

log = get_logger("scanner")

ProgressCb = Optional[Callable[[str, int], None]]


class GameScanner:
    def __init__(self, rules: RuleSet, config: AppConfig, guard: PathGuard,
                 manifest: Optional[Any] = None) -> None:
        self.rules = rules
        self.config = config
        self.guard = guard
        self.manifest = manifest

    # ------------------------------------------------------------------
    # 全量扫描
    # ------------------------------------------------------------------

    def scan(
        self,
        progress: ProgressCb = None,
        cancel: Optional[Any] = None,
        steam_info: Optional[detect.SteamInfo] = None,
    ) -> ScanResult:
        """扫描所有游戏库根目录 + Steam 库 + 常见存档位置，产出候选列表。"""
        t0 = time.time()
        res = ScanResult()
        seen: Dict[str, Candidate] = {}
        scanned_dirs = 0
        # 各类检测来源的开关（设置页「检测来源」可单独关闭）
        src = self.config.scan.sources()

        def _push(cands: Sequence[Candidate]) -> None:
            for c in cands:
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    return
                key = path_key(c.install_dir) if c.install_dir else c.key()
                if key in seen:
                    old = seen[key]
                    # 合并更丰富的信息
                    old.save_paths = list(dict.fromkeys(old.save_paths + c.save_paths))
                    old.save_evidence = list(dict.fromkeys(old.save_evidence + c.save_evidence))
                    old.reasons = list(dict.fromkeys(old.reasons + c.reasons))
                    old.score = max(old.score, c.score)
                    if not old.engine and c.engine:
                        old.engine = c.engine
                        old.engine_confidence = c.engine_confidence
                    if not old.exe and c.exe:
                        old.exe = c.exe
                else:
                    seen[key] = c

        # 1) 用户指定的游戏库根目录
        for root_raw in self.config.library_roots:
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                res.cancelled = True
                break
            root = normalize(root_raw)
            res.library_roots.append(str(root))
            stats: Dict[str, int] = {"visited": 0, "exe_dirs": 0}
            if self.guard.is_protected(root):
                res.errors.append(
                    f"游戏库根目录位于程序自己的数据/备份目录内，已跳过：{root}"
                )
                continue
            if not root.is_dir():
                res.errors.append(f"游戏库根目录不存在：{root}")
                res.root_reports.append({
                    "root": str(root), "exists": False, "visited": 0,
                    "exe_dirs": 0, "candidates": 0,
                    "hint": "路径不存在，请检查是否写错，或对应硬盘/移动盘没有挂载。",
                })
                continue
            self.guard.add_read_root(root)
            if progress is not None:
                progress(f"扫描游戏库：{root}", scanned_dirs)
            cands, n = detect.candidates_from_library_root(
                root,
                self.rules,
                max_depth=self.config.scan.max_depth,
                max_dirs=self.config.scan.max_dirs,
                extra_save_dirs=self.config.extra_save_dirs,
                scan_user_locations=self.config.scan.scan_user_save_locations,
                sniff_max_depth=self.config.scan.sniff_max_depth,
                max_save_paths=self.config.scan.max_save_paths,
                recent_days=self.config.scan.recent_days,
                sources=src,
                cancel=cancel,
                progress=progress,
                stats=stats,
            )
            scanned_dirs += n
            _push(cands)
            res.visited_dirs += int(stats.get("visited", 0))
            _hint = ""
            if int(stats.get("exe_dirs", 0)) == 0:
                _hint = ("没有找到含 .exe 的子目录：请确认这里是不是“装着一堆游戏文件夹”的那一层，"
                         "而不是某一个游戏的内部目录。")
            res.root_reports.append({
                "root": str(root),
                "exists": True,
                "visited": int(stats.get("visited", 0)),
                "exe_dirs": int(stats.get("exe_dirs", 0)),
                "candidates": len(cands),
                "hint": _hint,
            })
            # 顺便清理已不在任何游戏里的基线
            for c in cands:
                for p in c.save_paths:
                    self.guard.add_read_root(p)

        # 2) Steam
        if src.steam and not res.cancelled:
            info = steam_info or detect.discover_steam(self.rules)
            res.steam_libraries = [str(p) for p in info.libraries]
            for lib in info.libraries:
                self.guard.add_read_root(lib)
            if progress is not None:
                progress("扫描 Steam 库…", scanned_dirs)
            _push(detect.candidates_from_steam(
                info,
                self.rules,
                scan_user_locations=self.config.scan.scan_user_save_locations,
                extra_save_dirs=self.config.extra_save_dirs,
                sniff_max_depth=self.config.scan.sniff_max_depth,
                max_save_paths=self.config.scan.max_save_paths,
                recent_days=self.config.scan.recent_days,
            sources=src,
            ))
        elif steam_info is not None:
            res.steam_libraries = [str(p) for p in steam_info.libraries]

        # 3) 系统痕迹：注册表卸载项 / 开始菜单与桌面快捷方式 / 最近使用
        #    （三个子来源可以在「设置 → 检测来源」里单独关掉）
        if (src.registry_games or src.shortcuts or src.recent_used) and not res.cancelled:
            if progress is not None:
                progress("从注册表与快捷方式里查找游戏…", scanned_dirs)
            try:
                from . import systemscan

                sys_games: List[Any] = []
                if src.registry_games:
                    sys_games += systemscan.discover_registry_games(self.rules)
                if not res.cancelled and (src.shortcuts or src.recent_used):
                    sys_games += systemscan.discover_shortcut_games(
                        self.rules,
                        include_recent=src.recent_used,
                        only_recent=src.recent_used and not src.shortcuts,
                    )
                cands = detect.candidates_from_system_games(
                    sys_games,
                    self.rules,
                    extra_save_dirs=self.config.extra_save_dirs,
                    scan_user_locations=self.config.scan.scan_user_save_locations,
                    sniff_max_depth=self.config.scan.sniff_max_depth,
                    max_save_paths=self.config.scan.max_save_paths,
                    recent_days=self.config.scan.recent_days,
                    cancel=cancel,
                sources=src,
                )
                _push(cands)
                res.system_found = len(cands)
                for c in cands:
                    self.guard.add_read_root(c.install_dir)
                    for p in c.save_paths:
                        self.guard.add_read_root(p)
            except Exception as exc:  # noqa: BLE001
                log.warning("系统痕迹发现失败：%s", exc)
                res.errors.append(f"注册表/快捷方式发现失败：{exc}")

        # 4) 用户额外指定的存档目录：单独作为“候选存档位置”附加到同名候选上
        if self.config.extra_save_dirs and not res.cancelled:
            for raw in self.config.extra_save_dirs:
                p = normalize(raw)
                if not p.is_dir():
                    continue
                self.guard.add_read_root(p)
                name_key = detect._norm_name(p.name)
                for c in seen.values():
                    if name_key and name_key in detect._norm_name(c.name):
                        if str(p) not in c.save_paths:
                            c.save_paths.append(str(p))
                            c.save_evidence.append("用户指定的额外存档目录")

        all_cands = list(seen.values())

        # 5) Ludusavi 离线数据库：按游戏名补存档路径
        if self.manifest is not None and src.manifest and not res.cancelled:
            try:
                res.manifest_hits = detect.apply_manifest_to_candidates(
                    all_cands, self.manifest, extra_roots=self.config.library_roots
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Ludusavi 数据库匹配失败：%s", exc)

        # 6) 注册表里记录的存档路径（只读）
        if src.registry_saves and not res.cancelled:
            try:
                detect.apply_registry_save_paths(all_cands, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log.warning("注册表存档路径查找失败：%s", exc)

        # 7) 过滤掉评分过低的候选（但仍保留“有存档目录”的）
        res.candidates = sorted(
            [c for c in all_cands if c.score >= self.rules.min_game_dir_score or c.save_paths],
            key=lambda c: (-c.score, c.name.lower()),
        )
        # ★ 重要：备份根目录如果落在某个游戏库根目录里，
        #   扫描会把程序自己生成的备份当成“游戏/存档”再扫一遍（备份套备份）。
        for r in res.library_roots:
            for label, pr in (("备份根目录", getattr(self.guard, "_backup_root", None)),
                              ("数据根目录", getattr(self.guard, "_data_root", None))):
                if pr is None:
                    continue
                try:
                    inside = is_within(pr, r)
                except (OSError, ValueError):
                    inside = False
                if inside:
                    res.errors.append(
                        f"⚠ {label}位于游戏库根目录内：{pr}\n"
                        f"    这会让程序把自己的备份当成游戏/存档再扫一遍（备份套备份）。\n"
                        f"    建议把{label}移到游戏库之外，或从扫描范围里去掉 {r}。"
                    )

        res.scanned_dirs = scanned_dirs
        res.elapsed = time.time() - t0
        log.info("扫描完成：%s", res.summary())
        return res

    # ------------------------------------------------------------------
    # 基线建立
    # ------------------------------------------------------------------

    def build_baseline(
        self,
        game: Any,
        progress: Optional[Callable[[str], None]] = None,
        cancel: Optional[Any] = None,
    ) -> List[FileStamp]:
        """遍历游戏的所有存档路径，生成文件指纹列表（基线）。

        这是“当前状态”的唯一权威来源；基线写入 SQLite 后即成为比较基准。
        """
        stamps: List[FileStamp] = []
        compute_hash = bool(self.config.scan.compute_sha256)
        max_bytes = max(0, int(self.config.scan.hash_max_mb)) * 1024 * 1024

        for sp_raw in game.save_paths_norm():
            sp = normalize(sp_raw)
            self.guard.add_read_root(sp)
            if not sp.is_dir():
                log.debug("存档路径不存在，跳过基线：%s", sp)
                continue
            if progress is not None:
                progress(f"建立基线：{sp}")
            for abs_p, rel in iter_save_files(sp, self.rules, cancel=cancel):
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    return stamps
                if is_protected(abs_p):
                    continue
                try:
                    st = abs_p.stat()
                except OSError:
                    continue
                digest = ""
                if compute_hash and (max_bytes == 0 or st.st_size <= max_bytes):
                    digest = sha256_file(abs_p, cancel=cancel)
                stamps.append(FileStamp(
                    save_path=str(sp),
                    rel_path=rel,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    sha256=digest,
                ))
        return stamps

    # ------------------------------------------------------------------
    # 对比基线（“扫描存档更新”）
    # ------------------------------------------------------------------

    def scan_current(self, game: Any, cancel: Optional[Any] = None) -> Dict[tuple, FileStamp]:
        """返回游戏当前的文件指纹字典（不计算哈希的话会用基线里的哈希补齐）。"""
        current: Dict[tuple, FileStamp] = {}
        compute_hash = bool(self.config.scan.compute_sha256)
        max_bytes = max(0, int(self.config.scan.hash_max_mb)) * 1024 * 1024

        for sp_raw in game.save_paths_norm():
            sp = normalize(sp_raw)
            if not sp.is_dir():
                continue
            for abs_p, rel in iter_save_files(sp, self.rules, cancel=cancel):
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    return current
                if is_protected(abs_p):
                    continue
                if self.rules.is_temp_file(abs_p.name):
                    continue
                try:
                    st = abs_p.stat()
                except OSError:
                    continue
                digest = ""
                if compute_hash and (max_bytes == 0 or st.st_size <= max_bytes):
                    digest = sha256_file(abs_p, cancel=cancel)
                stamp = FileStamp(
                    save_path=str(sp),
                    rel_path=rel,
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    sha256=digest,
                )
                current[stamp.key] = stamp
        return current
