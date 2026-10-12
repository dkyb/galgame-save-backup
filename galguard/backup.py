# -*- coding: utf-8 -*-
"""快照备份引擎。

目录结构（每个游戏、每个快照独立）::

    <备份根目录>\\<游戏名>\\<YYYY-MM-DD_HH-MM-SS>\\
        manifest.json          # 完整元数据（游戏、引擎、exe、安装目录、原始存档路径、文件清单、大小、SHA256）
        原始存档路径.txt        # 人眼可读：这份备份到底来自哪个存档路径
        files\\...              # 存档文件的完整副本

规则要点：

* **只复制，不修改源存档**；复制失败自动重试 3 次。
* 先写 ``<快照名>.partial`` 临时目录，全部成功后再改名为最终目录名，
  避免留下“半个快照”被误认为是有效备份。
* 默认**不删除任何旧备份**；只有用户在设置里选择“最近 N 份 / 按天保留”时才清理，
  且清理前会校验目标路径确实位于备份根目录内。
* 备份完成后刷新该游戏的基线，于是“是否有更新”会重新变为“否”。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import APP_NAME, APP_VERSION, SCHEMA_VERSION
from .config import AppConfig
from .db import HistoryDB
from .detect import build_save_path_gate
from .fsutil import human_bytes, iter_save_files, retry_call, safe_dir_name, sha256_file
from .logsetup import get_logger
from .models import BaselineDiff, FileStamp, GameEntry, now_iso
from .pathgate import Decision
from .paths import PathGuard, is_within, normalize, path_key
from .rules import RuleSet
from .scanner import GameScanner

__all__ = ["BackupManager", "BackupError", "TRIGGERS"]

log = get_logger("backup")

#: 触发方式代码 → 中文说明（写进 manifest）
TRIGGERS: Dict[str, str] = {
    "baseline": "首次建立基线",
    "manual": "用户手动点击“立即备份”",
    "batch": "用户点击“备份有更新的游戏”",
    "auto": "静默监控检测到存档变化（自动）",
    "startup": "程序启动时的初始备份",
}


class BackupError(RuntimeError):
    pass


class BackupManager:
    def __init__(
        self,
        guard: PathGuard,
        config: AppConfig,
        db: HistoryDB,
        rules: RuleSet,
        scanner: GameScanner,
    ) -> None:
        self.guard = guard
        self.config = config
        self.db = db
        self.rules = rules
        self.scanner = scanner

    # ------------------------------------------------------------------
    # 差异比较
    # ------------------------------------------------------------------

    def diff_game(self, game: GameEntry, cancel: Optional[Any] = None) -> BaselineDiff:
        """把当前存档状态与数据库里的基线做比较。"""
        diff = BaselineDiff(game_id=game.id)
        try:
            baseline = self.db.get_baseline(game.id)
        except Exception as exc:  # noqa: BLE001
            diff.error = f"读取基线失败：{exc}"
            return diff

        current = self.scanner.scan_current(game, cancel=cancel)
        missing: List[str] = []
        missing_keys = set()
        for sp_raw in game.save_paths_norm():
            sp = normalize(sp_raw)
            if not sp.is_dir():
                missing.append(str(sp))
                k = path_key(sp)
                for bkey in baseline:
                    if bkey[0] == k:
                        missing_keys.add(bkey)

        compare_mtime = bool(self.config.scan.compare_mtime)

        for key, cur in current.items():
            base = baseline.get(key)
            if base is None:
                diff.added.append(cur)
                continue
            changed = False
            if int(cur.size) != int(base.size):
                changed = True
            elif compare_mtime and abs(float(cur.mtime) - float(base.mtime)) > 1.0:
                changed = True
            elif cur.sha256 and base.sha256 and cur.sha256 != base.sha256:
                changed = True
            if changed:
                diff.modified.append(cur)

        for key, base in baseline.items():
            if key in current or key in missing_keys:
                continue
            diff.removed.append(base)

        diff.missing_paths = missing
        return diff

    def update_baseline(self, game: GameEntry, cancel: Optional[Any] = None) -> int:
        """用当前状态刷新基线，返回基线文件数。"""
        stamps = self.scanner.build_baseline(game, cancel=cancel)
        self.db.replace_baseline(game.id, stamps)
        return len(stamps)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def snapshot_base_dir(self, game: GameEntry) -> Path:
        root = normalize(game.effective_backup_root(str(self.config.backup_root)))
        self.guard.set_backup_root(root)
        base = root / safe_dir_name(game.name, fallback=game.id)
        return self.guard.ensure_dir(base)

    def _unique_snapshot_name(self, base: Path, stamp: str) -> str:
        name = stamp
        i = 2
        while (base / name).exists():
            name = f"{stamp}_{i:02d}"
            i += 1
        return name

    def create_snapshot(
        self,
        game: GameEntry,
        trigger: str = "manual",
        cancel: Optional[Any] = None,
        update_baseline: bool = True,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """生成一个独立快照。返回结果字典（含 ok / snapshot_path / message）。"""
        result: Dict[str, Any] = {
            "ok": False,
            "game_id": game.id,
            "game_name": game.name,
            "trigger": trigger,
            "snapshot_path": "",
            "manifest_path": "",
            "file_count": 0,
            "total_bytes": 0,
            "message": "",
            "skipped": 0,
        }

        base = self.snapshot_base_dir(game)
        self._cleanup_stale_partials(base)

        ts = datetime.now()
        stamp = ts.strftime("%Y-%m-%d_%H-%M-%S")
        name = self._unique_snapshot_name(base, stamp)
        final_dir = base / name
        partial_dir = base / (name + ".partial")

        self.guard.check_write(partial_dir)
        try:
            partial_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            result["message"] = f"无法创建快照目录：{exc}"
            return result

        save_paths = game.save_paths_norm()

        # ★★ 准入闸门 —— 备份前最后、也是最硬的一道关卡。
        #     检测阶段是启发式“猜”，这里用结构化规则再核一遍整条路径：
        #       REJECT  → 绝不备份；
        #       CONFIRM → 规模超限（文件数/字节数超阈值），默认也不备份，
        #                 必须由用户在界面上确认过才会放行。
        gate = build_save_path_gate(self.rules)
        allow_oversize = set(getattr(self, "_allow_oversize", ()) or ())

        existing_paths: List[str] = []
        blocked_paths: List[str] = []
        oversize_paths: List[str] = []
        for _p in save_paths:
            _pn = normalize(_p)
            if not _pn.is_dir():
                continue
            if gate is not None:
                _verdict, _why = gate.check(str(_pn), quick=False)
                if _verdict == Decision.REJECT:
                    blocked_paths.append(f"{_pn}　→　{_why}")
                    log.warning("闸门拒绝存档路径 %s：%s", _pn, _why)
                    continue
                if _verdict == Decision.CONFIRM and str(_pn) not in allow_oversize:
                    oversize_paths.append(f"{_pn}　→　{_why}")
                    log.warning("存档路径规模超限，已跳过：%s（%s）", _pn, _why)
                    continue
            if self.guard.is_protected(_pn):
                blocked_paths.append(f"{_pn}　→　位于程序自己的数据/备份目录内")
                log.warning("存档路径位于程序自己的数据/备份目录内，已跳过：%s", _pn)
                continue
            existing_paths.append(str(_pn))

        result["blocked_paths"] = list(blocked_paths)
        result["oversize_paths"] = list(oversize_paths)

        if blocked_paths or oversize_paths:
            self.db.log_event(
                "error",
                "闸门拦截："
                + "；".join((blocked_paths + oversize_paths)[:6]),
                game.id,
            )
            if oversize_paths and not blocked_paths:
                result["message"] = "部分存档路径规模超限，已跳过（需要人工确认才会备份）"
            elif blocked_paths:
                result["message"] = "部分存档路径未通过准入闸门，已跳过"

        manifest_files: List[Dict[str, Any]] = []
        total_bytes = 0
        skipped = 0
        compute_hash = bool(self.config.scan.compute_sha256)
        max_bytes = max(0, int(self.config.scan.hash_max_mb)) * 1024 * 1024

        try:
            for idx, sp in enumerate(existing_paths):
                spn = normalize(sp)
                if len(existing_paths) == 1:
                    dest_root = partial_dir / "files"
                else:
                    dest_root = partial_dir / "files" / f"{idx:02d}_{safe_dir_name(spn.name, 'save')}"
                dest_root.mkdir(parents=True, exist_ok=True)

                files_before = len(manifest_files)
                bytes_before = total_bytes
                denied_streak = 0
                aborted_reason = ""

                for abs_p, rel in iter_save_files(spn, self.rules, cancel=cancel):
                    if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                        raise BackupError("用户取消了备份")
                    # ★ 第二道防线：逐个文件再挡一次 ——
                    #   万一存档目录里面套着备份目录（例如备份根目录设在游戏目录下），
                    #   这里会把它拦下来，绝不让备份把备份自己复制进去。
                    if self.guard.is_protected(abs_p):
                        skipped += 1
                        continue
                    dest = dest_root / Path(rel.replace("/", os.sep))
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        retry_call(
                            lambda a=abs_p, d=dest: shutil.copy2(a, d),
                            times=3,
                            delay=0.4,
                            on_error=lambda n, e: log.debug("复制重试 %d/3 %s：%s", n, abs_p, e),
                        )
                    except Exception as exc:  # noqa: BLE001
                        skipped += 1
                        log.warning("复制失败（已跳过）：%s → %s：%s", abs_p, dest, exc)
                        # ★ 独占文件熔断：连续 N 个文件“无权限访问”，
                        #   几乎可以断定这不是存档目录（多半是被进程独占的浏览器/应用缓存）。
                        if isinstance(exc, PermissionError):
                            denied_streak += 1
                            limit = (gate.abort_after_denied_errors if gate else 20) or 20
                            if denied_streak >= limit:
                                aborted_reason = (
                                    f"连续 {denied_streak} 个文件因权限被拒绝（多为被占用的缓存文件），"
                                    f"判定该路径不是存档目录，已中止：{spn}"
                                )
                                break
                        else:
                            denied_streak = 0
                        continue

                    try:
                        st = abs_p.stat()
                        size = int(st.st_size)
                        mtime = float(st.st_mtime)
                    except OSError:
                        size, mtime = 0, 0.0
                    digest = ""
                    if compute_hash and (max_bytes == 0 or size <= max_bytes):
                        digest = sha256_file(abs_p, cancel=cancel)
                    total_bytes += size
                    manifest_files.append({
                        "save_path": str(spn),
                        "rel_path": rel,
                        "archive_path": str(dest.relative_to(partial_dir)).replace("\\", "/"),
                        "size": size,
                        "mtime": mtime,
                        "mtime_text": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "",
                        "sha256": digest,
                    })

                if aborted_reason:
                    # 该路径被熔断：把这条路径已经复制进去的内容整体撤掉，
                    # 免得留下一份“半截的非存档目录”。
                    log.error("%s", aborted_reason)
                    blocked_paths.append(aborted_reason)
                    del manifest_files[files_before:]
                    total_bytes = bytes_before
                    self._rmtree_inside_backup_root(dest_root)
                    self.db.log_event("error", aborted_reason, game.id)

            if blocked_paths:
                result["blocked_paths"] = list(blocked_paths)
                result["message"] = "部分存档路径被闸门/熔断拦截，已跳过"

            manifest = self._build_manifest(
                game=game,
                trigger=trigger,
                created_at=ts.strftime("%Y-%m-%d %H:%M:%S"),
                save_paths=save_paths,
                existing_paths=existing_paths,
                files=manifest_files,
                total_bytes=total_bytes,
                skipped=skipped,
                snapshot_name=name,
            )
            manifest_path = partial_dir / "manifest.json"
            self.guard.check_write(manifest_path)
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)

            # 原始存档路径.txt —— UTF-8 BOM，方便记事本直接双击打开
            note_path = partial_dir / "原始存档路径.txt"
            self.guard.check_write(note_path)
            with open(note_path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write(self._build_note(game, trigger, ts, save_paths, existing_paths, len(existing_paths)))

            # 原子改名：partial → 正式目录
            os.replace(str(partial_dir), str(final_dir))
        except Exception as exc:  # noqa: BLE001
            log.exception("生成快照失败：%s", game.name)
            self._rmtree_inside_backup_root(partial_dir)
            self.db.add_backup(
                game_id=game.id, game_name=game.name, snapshot_path="", manifest_path="",
                trigger=trigger, file_count=0, total_bytes=0, status="failed", message=str(exc),
            )
            self.db.log_event("error", f"备份失败：{game.name} —— {exc}", game.id)
            result["message"] = f"备份失败：{exc}"
            return result

        record_id = self.db.add_backup(
            game_id=game.id,
            game_name=game.name,
            snapshot_path=str(final_dir),
            manifest_path=str(final_dir / "manifest.json"),
            trigger=trigger,
            file_count=len(manifest_files),
            total_bytes=total_bytes,
            status="ok",
            message=f"跳过 {skipped} 个文件" if skipped else "",
        )

        if update_baseline:
            try:
                self.update_baseline(game, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log.warning("刷新基线失败：%s", exc)

        try:
            self.apply_retention(game)
        except Exception as exc:  # noqa: BLE001
            log.warning("保留策略执行失败：%s", exc)

        self.db.log_event(
            "backup",
            f"已备份「{game.name}」：{len(manifest_files)} 个文件 / {human_bytes(total_bytes)}"
            f" → {final_dir.name}",
            game.id,
        )
        log.info("快照完成：%s → %s（%d 文件）", game.name, final_dir, len(manifest_files))

        result.update({
            "ok": True,
            "snapshot_path": str(final_dir),
            "manifest_path": str(final_dir / "manifest.json"),
            "file_count": len(manifest_files),
            "total_bytes": total_bytes,
            "skipped": skipped,
            "record_id": record_id,
            "message": result.get("message") or "完成",
        })
        return result

    # ------------------------------------------------------------------
    # manifest / 说明文件
    # ------------------------------------------------------------------

    def _build_manifest(
        self,
        game: GameEntry,
        trigger: str,
        created_at: str,
        save_paths: Sequence[str],
        existing_paths: Sequence[str],
        files: Sequence[Dict[str, Any]],
        total_bytes: int,
        skipped: int,
        snapshot_name: str,
    ) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": APP_NAME, "version": APP_VERSION},
            "snapshot": {
                "name": snapshot_name,
                "created_at": created_at,
                "trigger": trigger,
                "trigger_text": TRIGGERS.get(trigger, trigger),
            },
            "game": {
                "id": game.id,
                "name": game.name,
                "engine": game.engine,
                "exe": game.exe,
                "install_dir": game.install_dir,
                "notes": game.notes,
                "source": game.source,
            },
            "原始存档路径": list(save_paths),
            "origin_save_paths": list(save_paths),
            "available_save_paths": list(existing_paths),
            "unavailable_save_paths": [p for p in save_paths if p not in existing_paths],
            "files": list(files),
            "summary": {
                "file_count": len(files),
                "total_bytes": total_bytes,
                "total_text": human_bytes(total_bytes),
                "skipped_files": skipped,
                "save_path_count": len(save_paths),
                "sha256_computed": bool(self.config.scan.compute_sha256),
            },
        }

    def _build_note(
        self,
        game: GameEntry,
        trigger: str,
        ts: datetime,
        save_paths: Sequence[str],
        existing_paths: Sequence[str],
        path_count: int,
    ) -> str:
        lines: List[str] = []
        lines.append("=" * 68)
        lines.append("Galgame 存档守护 —— 本备份的来源说明")
        lines.append("=" * 68)
        lines.append("")
        lines.append(f"游戏名称　　：{game.name}")
        lines.append(f"游戏引擎　　：{game.engine or '（未识别）'}")
        lines.append(f"游戏主程序　：{game.exe or '（未记录）'}")
        lines.append(f"安装目录　　：{game.install_dir or '（未记录）'}")
        lines.append(f"备份时间　　：{ts.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"触发方式　　：{TRIGGERS.get(trigger, trigger)}")
        lines.append(f"备份工具　　：{APP_NAME} v{APP_VERSION}")
        if game.notes:
            lines.append(f"备注　　　　：{game.notes}")
        lines.append("")
        lines.append("-" * 68)
        lines.append("本备份来自以下原始存档路径：")
        lines.append("-" * 68)
        for i, sp in enumerate(save_paths):
            flag = "" if sp in existing_paths else "  【备份时该目录不存在】"
            lines.append(f"[{i + 1}] {sp}{flag}")
            if path_count == 1:
                lines.append("      快照内对应位置：files\\")
            else:
                label = safe_dir_name(normalize(sp).name, "save")
                lines.append(f"      快照内对应位置：files\\{i:02d}_{label}\\")
            lines.append("")
        lines.append("-" * 68)
        lines.append("说明：")
        lines.append("  1. 快照内的文件是从上述路径原样复制而来的只读副本，源存档未被修改。")
        lines.append("  2. 每个存档路径的文件都被完整保留（含子目录结构）。")
        lines.append("  3. 文件清单、大小与 SHA256 见同目录下的 manifest.json。")
        lines.append("  4. 恢复时把 files\\ 下的内容复制回对应的原始存档路径即可。")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 清理与保留策略
    # ------------------------------------------------------------------

    def _cleanup_stale_partials(self, base: Path) -> None:
        """清理上次崩溃/断电留下的 .partial 目录。"""
        try:
            for child in base.iterdir():
                if child.is_dir() and child.name.endswith(".partial"):
                    self._rmtree_inside_backup_root(child)
                    log.info("清理未完成的快照：%s", child)
        except OSError:
            pass

    def _rmtree_inside_backup_root(self, target: Path) -> None:
        """只有在确认目标位于备份根目录内时才允许删除。"""
        try:
            root = self.guard.backup_root
        except Exception:
            return
        if not is_within(target, root) or path_key(target) == path_key(root):
            log.error("拒绝删除非备份目录内的路径：%s", target)
            return
        shutil.rmtree(target, ignore_errors=True)

    def apply_retention(self, game: GameEntry) -> List[str]:
        """按保留策略删除旧快照，返回被删除的快照路径列表。"""
        policy = self.config.retention
        if policy.mode == "none":
            return []

        rows = self.db.list_backups(game.id, limit=100000)
        ok_rows = [r for r in rows if r.get("status") == "ok"]
        if not ok_rows:
            return []

        # 传入的已按 created_at DESC 排序
        keep_ids = set()
        if policy.mode == "count":
            keep_ids = {int(r["id"]) for r in ok_rows[: policy.count]}
        elif policy.mode == "days":
            cutoff = datetime.now() - timedelta(days=policy.days)
            for r in ok_rows:
                try:
                    when = datetime.strptime(str(r["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if when >= cutoff:
                    keep_ids.add(int(r["id"]))

        deleted: List[str] = []
        doomed_rows: List[Dict[str, Any]] = []
        for r in ok_rows:
            if int(r["id"]) in keep_ids:
                continue
            sp = Path(str(r.get("snapshot_path") or ""))
            if sp and sp.exists():
                if is_within(sp, self.guard.backup_root) and path_key(sp) != path_key(self.guard.backup_root):
                    shutil.rmtree(sp, ignore_errors=True)
                    deleted.append(str(sp))
                else:
                    log.error("保留策略跳过越界路径：%s", sp)
                    continue
            doomed_rows.append(r)

        self.db.delete_backup_rows([int(r["id"]) for r in doomed_rows])
        if deleted:
            log.info("保留策略（%s）删除了 %d 个旧快照：%s", policy.mode, len(deleted), game.name)
            self.db.log_event("retention", f"按保留策略删除 {len(deleted)} 个旧快照：{game.name}", game.id)
        return deleted

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def latest_snapshot(self, game: GameEntry) -> Optional[Dict[str, Any]]:
        return self.db.last_backup(game.id)
