# -*- coding: utf-8 -*-
"""应用服务层。

界面（tkinter）与核心逻辑（扫描 / 备份 / 监控）之间的唯一桥梁：

* 所有耗时操作都在后台守护线程里跑，界面永不卡住。
* 后台线程通过 ``events`` 队列向界面投递消息；界面用 ``after()`` 轮询消费。
* 界面线程绝不直接触碰 SQLite / 文件系统重活。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .backup import BackupManager
from .config import AppConfig, ConfigStore
from .db import HistoryDB
from .detect import SteamInfo, discover_steam
from .fsutil import human_bytes
from .logsetup import get_logger
from .models import BaselineDiff, Candidate, GameEntry, ScanResult, make_game_id
from .monitor import SaveMonitor
from .paths import PathGuard, normalize
from .rules import RuleSet
from .scanner import GameScanner
from .tasks import DaemonPool

__all__ = ["AppService"]

log = get_logger("service")


class AppService:
    def __init__(
        self,
        guard: PathGuard,
        store: ConfigStore,
        config: AppConfig,
        db: HistoryDB,
        rules: RuleSet,
        manifest: Optional[Any] = None,
    ) -> None:
        self.guard = guard
        self.store = store
        self.config = config
        self.db = db
        self.rules = rules
        self.manifest = manifest

        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.pool = DaemonPool(workers=3, name="galguard-task")

        self.scanner = GameScanner(rules, config, guard, manifest)
        self.backups = BackupManager(guard, config, db, rules, self.scanner)
        self.monitor = SaveMonitor(
            rules=rules,
            on_change=self._on_fs_change,
            debounce_seconds=config.monitor.debounce_seconds,
            poll_interval_seconds=config.monitor.poll_interval_seconds,
            backend=config.monitor.backend,
        )

        self._lock = threading.RLock()
        self._cancels: Dict[str, threading.Event] = {}
        self._diffs: Dict[str, BaselineDiff] = {}
        self._backing_up: set = set()
        self._busy = False
        self.steam_info: Optional[SteamInfo] = None
        self._shutting_down = False

    # ==================================================================
    # 事件通道
    # ==================================================================

    def post(self, event: Dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:  # pragma: no cover
            pass

    def emit_log(self, text: str, level: str = "info") -> None:
        self.post({"type": "log", "level": level, "text": text})
        if level == "error":
            log.error(text)
        elif level == "warn":
            log.warning(text)
        else:
            log.info(text)

    def poll_events(self, max_items: int = 200) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _ in range(max_items):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    def _set_busy(self, value: bool, text: str = "") -> None:
        with self._lock:
            self._busy = value
        self.post({"type": "busy", "value": value, "text": text})

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def _cancel_event(self, scope: str) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._cancels[scope] = ev
        return ev

    def cancel(self, scope: str) -> None:
        with self._lock:
            ev = self._cancels.get(scope)
        if ev is not None:
            ev.set()

    # ==================================================================
    # 配置持久化
    # ==================================================================

    def save_config(self) -> None:
        try:
            self.store.save(self.config)
        except Exception as exc:  # noqa: BLE001
            self.emit_log(f"保存配置失败：{exc}", "error")

    def apply_config(self, new_config: AppConfig) -> None:
        """应用设置页保存的新配置（含备份根目录切换）。"""
        old_manifest = (self.config.scan.manifest_path or "") if self.config else ""
        with self._lock:
            self.config = new_config
            self.scanner.config = new_config
            self.backups.config = new_config
            self.monitor.debounce_seconds = max(0, int(new_config.monitor.debounce_seconds))
            self.monitor.poll_interval_seconds = max(2, int(new_config.monitor.poll_interval_seconds))
            self.monitor.backend_pref = new_config.monitor.backend
        self.guard.set_backup_root(new_config.backup_root)
        self.guard.ensure_dir(self.guard.backup_root)
        self.save_config()
        if (new_config.scan.manifest_path or "") != old_manifest:
            self.reload_manifest()
        self.post({"type": "config_changed"})

    # ==================================================================
    # Ludusavi 离线存档数据库
    # ==================================================================

    def reload_manifest(self) -> bool:
        """按当前配置加载 / 卸载 Ludusavi manifest.yaml。"""
        from .manifest import LudusaviManifest

        path = (self.config.scan.manifest_path or "").strip()
        if not path:
            self.manifest = None
            self.scanner.manifest = None
            self.emit_log("未配置 Ludusavi 存档数据库（可选）")
            self.post({"type": "manifest_state"})
            return False

        mf = LudusaviManifest()
        if not mf.load(Path(path)):
            self.emit_log("Ludusavi 数据库加载失败：" + "；".join(mf.errors or ["未知错误"]), "error")
            self.post({"type": "manifest_state"})
            return False
        self.manifest = mf
        self.scanner.manifest = mf
        self.emit_log(f"Ludusavi 存档数据库已加载：{len(mf)} 个游戏")
        self.post({"type": "manifest_state"})
        return True

    # ==================================================================
    # 游戏列表管理
    # ==================================================================

    def games(self) -> List[GameEntry]:
        with self._lock:
            return list(self.config.games)

    def get_game(self, game_id: str) -> Optional[GameEntry]:
        with self._lock:
            return self.config.get_game(game_id)

    def game_stats(self) -> Dict[str, Dict[str, Any]]:
        try:
            return self.db.game_stats()
        except Exception:  # noqa: BLE001
            return {}

    def diff_of(self, game_id: str) -> Optional[BaselineDiff]:
        with self._lock:
            return self._diffs.get(game_id)

    def add_candidates(self, candidates: Sequence[Candidate]) -> int:
        """把用户勾选的候选加入游戏列表，并后台建立基线。"""
        added: List[GameEntry] = []
        with self._lock:
            for c in candidates:
                gid = make_game_id(c.name, c.install_dir)
                if self.config.get_game(gid) is not None:
                    continue
                entry = GameEntry(
                    id=gid,
                    name=c.name,
                    engine=c.engine,
                    exe=c.exe,
                    install_dir=c.install_dir,
                    save_paths=list(c.save_paths),
                    source="steam" if c.source == "steam" else "auto",
                )
                self.config.games.append(entry)
                added.append(entry)
        if added:
            for g in added:
                for sp in g.save_paths_norm():
                    self.guard.add_read_root(sp)
            self.save_config()
            self.post({"type": "games_changed"})
            self.emit_log(f"已加入 {len(added)} 个游戏，正在建立存档基线…")
            self._build_baselines_async([g.id for g in added])
        return len(added)

    def add_manual_game(self, entry: GameEntry) -> bool:
        with self._lock:
            if self.config.get_game(entry.id) is None and any(
                g.name == entry.name and g.install_dir == entry.install_dir for g in self.config.games
            ):
                return False
            self.config.upsert_game(entry)
        for sp in entry.save_paths_norm():
            self.guard.add_read_root(sp)
        self.save_config()
        self.post({"type": "games_changed"})
        self._build_baselines_async([entry.id])
        self.emit_log(f"已保存游戏「{entry.name}」并开始建立基线")
        return True

    def update_game(self, entry: GameEntry) -> None:
        with self._lock:
            self.config.upsert_game(entry)
        self.save_config()
        self.post({"type": "games_changed"})
        self._build_baselines_async([entry.id])
        self.emit_log(f"已更新「{entry.name}」")

    def remove_game(self, game_id: str, delete_backups: bool = False) -> None:
        with self._lock:
            entry = self.config.remove_game(game_id)
            self._diffs.pop(game_id, None)
        if entry is None:
            return
        try:
            self.db.drop_game(game_id)
        except Exception:  # noqa: BLE001
            log.exception("删除基线失败")
        self.save_config()
        if delete_backups:
            # 删除备份属于“写备份根目录”，在允许范围内
            rows = self.db.list_backups(game_id, limit=100000)
            from .paths import is_within, path_key  # 局部导入避免循环

            for r in rows:
                p = Path(str(r.get("snapshot_path") or ""))
                try:
                    if p.exists() and is_within(p, self.guard.backup_root) and \
                            path_key(p) != path_key(self.guard.backup_root):
                        import shutil

                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass
            self.db.delete_backup_rows([int(r["id"]) for r in rows])
        self.post({"type": "games_changed"})
        self.emit_log(f"已删除游戏「{entry.name}」")

    # ==================================================================
    # 基线
    # ==================================================================

    def _build_baselines_async(self, ids: Sequence[str]) -> None:
        ids = list(ids)
        if not ids:
            return

        def work() -> int:
            total = 0
            for gid in ids:
                game = self.get_game(gid)
                if game is None:
                    continue
                ev = self._cancel_event(f"baseline:{gid}")
                try:
                    n = self.backups.update_baseline(game, cancel=ev)
                    total += n
                    self.post({"type": "progress", "scope": "baseline",
                               "text": f"已建立基线：{game.name}（{n} 个文件）"})
                except Exception as exc:  # noqa: BLE001
                    self.emit_log(f"建立基线失败：{game.name}：{exc}", "error")
            return total

        self._set_busy(True, "正在建立存档基线…")
        self.pool.submit(
            work,
            name="build-baseline",
            on_done=lambda n: (
                self._set_busy(False),
                self.emit_log(f"基线建立完成，共记录 {n} 个存档文件"),
                self.post({"type": "baselines_done", "count": n}),
                self.refresh_updates(ids, quiet=True),
            ),
            on_error=lambda e: (self._set_busy(False), self.emit_log(f"建立基线失败：{e}", "error")),
        )

    def ensure_baselines(self) -> None:
        """为还没有基线的游戏补建基线（用于旧配置或中断恢复）。"""
        missing: List[str] = []
        for g in self.games():
            try:
                if not self.db.has_baseline(g.id):
                    missing.append(g.id)
            except Exception:  # noqa: BLE001
                continue
        if missing:
            self.emit_log(f"{len(missing)} 个游戏缺少基线，正在补建…")
            self._build_baselines_async(missing)

    # ==================================================================
    # 扫描候选
    # ==================================================================

    def scan_candidates(self, include_steam: bool = True) -> None:
        if self.busy:
            self.emit_log("已有任务在运行，请稍候…", "warn")
            return
        if not self.config.library_roots and not include_steam:
            self.emit_log("尚未设置游戏库根目录，无法扫描。请到「设置」里添加。", "warn")
            return

        ev = self._cancel_event("scan")
        self._set_busy(True, "正在扫描游戏库…")
        self.emit_log("开始扫描游戏库…")

        def progress(text: str, count: int) -> None:
            self.post({"type": "progress", "scope": "scan", "text": text, "count": count})

        def work() -> ScanResult:
            try:
                self.steam_info = discover_steam(self.rules) if self.config.scan.detect_steam else None
            except Exception as exc:  # noqa: BLE001
                log.warning("Steam 检测失败：%s", exc)
                self.steam_info = None
            return self.scanner.scan(progress=progress, cancel=ev, steam_info=self.steam_info)

        self.pool.submit(
            work,
            name="scan-candidates",
            on_done=self._on_scan_done,
            on_error=lambda e: (self._set_busy(False), self.emit_log(f"扫描失败：{e}", "error")),
        )

    def _on_scan_done(self, result: ScanResult) -> None:
        self._set_busy(False)
        self.emit_log(result.summary())
        for err in result.errors[:10]:
            self.emit_log(err, "warn")
        self.post({"type": "scan_done", "result": result})

    # ==================================================================
    # 更新检测
    # ==================================================================

    def refresh_updates(self, ids: Optional[Sequence[str]] = None, quiet: bool = False) -> None:
        targets = list(ids) if ids else [g.id for g in self.games()]
        if not targets:
            if not quiet:
                self.emit_log("游戏列表为空，无需扫描。", "warn")
            return
        if self.busy and not quiet:
            self.emit_log("已有任务在运行，请稍候…", "warn")
            return

        ev = self._cancel_event("update-scan")
        if not quiet:
            self._set_busy(True, "正在对比存档更新…")
            self.emit_log("开始扫描存档更新…")

        def work() -> Dict[str, BaselineDiff]:
            out: Dict[str, BaselineDiff] = {}
            for gid in targets:
                if ev.is_set() or self._shutting_down:
                    break
                game = self.get_game(gid)
                if game is None:
                    continue
                self.post({"type": "progress", "scope": "update", "text": f"检查：{game.name}"})
                try:
                    out[gid] = self.backups.diff_game(game, cancel=ev)
                except Exception as exc:  # noqa: BLE001
                    d = BaselineDiff(game_id=gid, error=str(exc))
                    out[gid] = d
                    self.emit_log(f"检查「{game.name}」失败：{exc}", "error")
            return out

        def done(diffs: Dict[str, BaselineDiff]) -> None:
            with self._lock:
                self._diffs.update(diffs)
            changed = [gid for gid, d in diffs.items() if d.is_changed]
            self._set_busy(False)
            if not quiet:
                self.emit_log(
                    f"存档更新扫描完成：{len(changed)} 个游戏有更新"
                    + (f"（{', '.join(self._name_of(i) for i in changed[:5])}…）" if changed else "")
                )
            self.post({"type": "updates_done", "changed": changed,
                       "diffs": {k: v.summary() for k, v in diffs.items()}})

        self.pool.submit(
            work,
            name="update-scan",
            on_done=done,
            on_error=lambda e: (self._set_busy(False), self.emit_log(f"更新扫描失败：{e}", "error")),
        )

    def _name_of(self, game_id: str) -> str:
        g = self.get_game(game_id)
        return g.name if g else game_id

    def changed_game_ids(self) -> List[str]:
        with self._lock:
            return [gid for gid, d in self._diffs.items() if d.is_changed]

    # ==================================================================
    # 备份
    # ==================================================================

    def backup_game(self, game_id: str, trigger: str = "manual") -> None:
        self._backup_many([game_id], trigger)

    def backup_updated(self) -> None:
        ids = self.changed_game_ids()
        if not ids:
            self.emit_log("没有检测到有更新的游戏。可先点「扫描存档更新」。", "warn")
            self.post({"type": "backups_done", "results": []})
            return
        self._backup_many(ids, "batch")

    def backup_all(self) -> None:
        self._backup_many([g.id for g in self.games()], "batch")

    def _backup_many(self, ids: Sequence[str], trigger: str) -> None:
        if self._shutting_down:
            return
        targets: List[str] = []
        with self._lock:
            for gid in ids:
                if gid in self._backing_up:
                    continue
                if self.get_game(gid) is None:
                    continue
                self._backing_up.add(gid)
                targets.append(gid)
        if not targets:
            self.post({"type": "backups_done", "results": []})
            return

        ev = self._cancel_event("backup")
        self._set_busy(True, f"正在备份 {len(targets)} 个游戏…")
        self.emit_log(f"开始备份 {len(targets)} 个游戏（{trigger}）…")

        def work() -> List[Dict[str, Any]]:
            results: List[Dict[str, Any]] = []
            for gid in targets:
                if ev.is_set() or self._shutting_down:
                    break
                game = self.get_game(gid)
                if game is None:
                    continue
                self.post({"type": "progress", "scope": "backup", "text": f"备份中：{game.name}"})
                try:
                    res = self.backups.create_snapshot(game, trigger=trigger, cancel=ev)
                except Exception as exc:  # noqa: BLE001
                    res = {"ok": False, "game_id": gid, "game_name": game.name,
                           "message": str(exc), "file_count": 0, "total_bytes": 0,
                           "snapshot_path": ""}
                    self.emit_log(f"备份「{game.name}」失败：{exc}", "error")
                results.append(res)
                if res.get("ok"):
                    self.emit_log(
                        f"✔ 已备份「{res['game_name']}」：{res['file_count']} 个文件 / "
                        f"{human_bytes(int(res.get('total_bytes', 0)))}"
                    )
            return results

        def done(results: List[Dict[str, Any]]) -> None:
            with self._lock:
                for gid in targets:
                    self._backing_up.discard(gid)
                    d = self._diffs.get(gid)
                    if d is not None and any(
                        r.get("game_id") == gid and r.get("ok") for r in results
                    ):
                        self._diffs[gid] = BaselineDiff(game_id=gid)
            self._set_busy(False)
            ok = [r for r in results if r.get("ok")]
            if ok:
                self.emit_log(f"备份完成：成功 {len(ok)} / {len(results)} 个游戏")
            elif results:
                self.emit_log("本轮没有任何备份成功，请查看日志。", "warn")
            self.post({"type": "backups_done", "results": results})

        def failed(e: BaseException) -> None:
            with self._lock:
                for gid in targets:
                    self._backing_up.discard(gid)
            self._set_busy(False)
            self.emit_log(f"备份任务异常：{e}", "error")
            self.post({"type": "backups_done", "results": []})

        self.pool.submit(work, name="backup", on_done=done, on_error=failed)

    # ==================================================================
    # 监控
    # ==================================================================

    def start_monitoring(self) -> None:
        games = [g for g in self.games() if g.enabled and g.monitor]
        if not games:
            self.emit_log("没有启用监控的游戏。请先添加游戏并勾选「启用监控」。", "warn")
            self.post({"type": "monitor_state"})
            return
        self.monitor.start(games)
        self.emit_log(f"静默监控已启动：{self.monitor.status_text()}")
        self.post({"type": "monitor_state"})

    def stop_monitoring(self) -> None:
        self.monitor.stop()
        self.emit_log("静默监控已停止")
        self.post({"type": "monitor_state"})

    def pause_monitoring(self) -> None:
        self.monitor.pause()
        self.post({"type": "monitor_state"})

    def resume_monitoring(self) -> None:
        self.monitor.resume()
        self.post({"type": "monitor_state"})

    def toggle_monitoring(self) -> None:
        if not self.monitor.is_running:
            self.start_monitoring()
        elif self.monitor.is_paused:
            self.resume_monitoring()
        else:
            self.pause_monitoring()

    def reload_monitoring(self) -> None:
        if self.monitor.is_running:
            self.monitor.set_games([g for g in self.games() if g.enabled and g.monitor])
            self.post({"type": "monitor_state"})

    def _on_fs_change(self, game_id: str) -> None:
        """监控线程回调：存档目录发生变化 → 自动生成快照。"""
        if self._shutting_down:
            return
        game = self.get_game(game_id)
        if game is None or not game.enabled:
            return
        self.emit_log(f"检测到存档变化：「{game.name}」，开始自动备份…")
        self._backup_many([game_id], "auto")

    # ==================================================================
    # 退出
    # ==================================================================

    def shutdown(self) -> None:
        """停止一切后台活动（不负责关闭窗口/托盘）。"""
        self._shutting_down = True
        with self._lock:
            for ev in self._cancels.values():
                try:
                    ev.set()
                except Exception:
                    pass
        try:
            self.monitor.stop()
        except Exception:  # noqa: BLE001
            log.exception("停止监控失败")
        try:
            self.pool.stop(drain=True, timeout=4.0)
        except Exception:  # noqa: BLE001
            log.exception("停止任务池失败")
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭数据库失败")

    # ==================================================================
    # 杂项
    # ==================================================================

    def data_root(self) -> str:
        return str(self.guard.data_root)

    def backup_root(self) -> str:
        return str(self.guard.backup_root)

    def open_in_explorer(self, path: str) -> None:
        """用资源管理器打开目录（只读操作，不写任何东西）。"""
        p = normalize(path)
        if not p.exists():
            self.emit_log(f"目录不存在：{p}", "warn")
            return
        try:
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:  # pragma: no cover
                import subprocess

                subprocess.Popen(["xdg-open", str(p)])
        except OSError as exc:
            self.emit_log(f"无法打开目录：{exc}", "error")

    def open_save_dir(self, game_id: str) -> None:
        game = self.get_game(game_id)
        if game is None:
            return
        paths = [p for p in game.save_paths_norm() if normalize(p).is_dir()]
        if not paths:
            self.emit_log(f"「{game.name}」没有可用的存档目录", "warn")
            return
        self.open_in_explorer(paths[0])

    def open_backup_dir(self, game_id: str) -> None:
        game = self.get_game(game_id)
        if game is None:
            return
        root = normalize(game.effective_backup_root(self.backup_root()))
        target = root / game.safe_folder_name()
        if not target.is_dir():
            target = root
        self.open_in_explorer(str(target))
