# -*- coding: utf-8 -*-
"""SQLite 历史库：``<总数据根目录>\\data\\history.db``。

包含三张表：
    * ``baseline_files`` —— 每个游戏存档的基线（文件清单 + 大小 + 时间 + 可选哈希）
    * ``backups``        —— 备份历史（快照路径、时间、触发方式、文件数、字节数）
    * ``events``         —— 轻量事件日志（供界面显示）

数据库文件以及 WAL/SHM 附属文件都在数据根目录内，符合零污染要求。
所有操作加锁，可安全地被多个工作线程调用。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import FileStamp, now_iso
from .paths import PathGuard, path_key

__all__ = ["HistoryDB"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS baseline_files (
    game_id   TEXT NOT NULL,
    save_path TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    mtime     REAL    NOT NULL DEFAULT 0,
    sha256    TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (game_id, save_path, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_baseline_game ON baseline_files(game_id);

CREATE TABLE IF NOT EXISTS backups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       TEXT NOT NULL,
    game_name     TEXT NOT NULL DEFAULT '',
    snapshot_path TEXT NOT NULL,
    manifest_path TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT 'manual',
    file_count    INTEGER NOT NULL DEFAULT 0,
    total_bytes   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ok',
    message       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_backups_game ON backups(game_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'info',
    game_id TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT ''
);
"""


class HistoryDB:
    def __init__(self, guard: PathGuard) -> None:
        self.guard = guard
        self.path: Path = guard.data_root / "data" / "history.db"
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # -- 生命周期 -----------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self.guard.ensure_dir(self.path.parent)
            # check_same_thread=False + 自建 RLock：允许多线程访问
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                ("1",),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                # 合并 WAL，减少 -wal/-shm 残留
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # -- 基线 ---------------------------------------------------------------

    def replace_baseline(self, game_id: str, stamps: Sequence[FileStamp]) -> None:
        with self._lock:
            c = self._c()
            c.execute("DELETE FROM baseline_files WHERE game_id = ?", (game_id,))
            c.executemany(
                "INSERT OR REPLACE INTO baseline_files"
                "(game_id, save_path, rel_path, size, mtime, sha256) VALUES (?,?,?,?,?,?)",
                [
                    (game_id, s.save_path, s.rel_path, int(s.size), float(s.mtime), s.sha256 or "")
                    for s in stamps
                ],
            )
            c.commit()

    def get_baseline(self, game_id: str) -> Dict[tuple, FileStamp]:
        with self._lock:
            c = self._c()
            rows = c.execute(
                "SELECT save_path, rel_path, size, mtime, sha256 FROM baseline_files WHERE game_id = ?",
                (game_id,),
            ).fetchall()
        out: Dict[tuple, FileStamp] = {}
        for r in rows:
            st = FileStamp(
                save_path=r["save_path"],
                rel_path=r["rel_path"],
                size=int(r["size"]),
                mtime=float(r["mtime"]),
                sha256=r["sha256"] or "",
            )
            out[st.key] = st
        return out

    def has_baseline(self, game_id: str) -> bool:
        with self._lock:
            c = self._c()
            row = c.execute(
                "SELECT 1 FROM baseline_files WHERE game_id = ? LIMIT 1", (game_id,)
            ).fetchone()
        return row is not None

    def baseline_count(self, game_id: str) -> int:
        with self._lock:
            c = self._c()
            row = c.execute(
                "SELECT COUNT(*) AS n FROM baseline_files WHERE game_id = ?", (game_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def drop_game(self, game_id: str) -> None:
        with self._lock:
            c = self._c()
            c.execute("DELETE FROM baseline_files WHERE game_id = ?", (game_id,))
            c.commit()

    # -- 备份历史 -----------------------------------------------------------

    def add_backup(
        self,
        game_id: str,
        game_name: str,
        snapshot_path: str,
        manifest_path: str,
        trigger: str,
        file_count: int,
        total_bytes: int,
        status: str = "ok",
        message: str = "",
        created_at: Optional[str] = None,
    ) -> int:
        with self._lock:
            c = self._c()
            cur = c.execute(
                "INSERT INTO backups(game_id, game_name, snapshot_path, manifest_path, created_at,"
                " trigger, file_count, total_bytes, status, message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    game_id,
                    game_name,
                    snapshot_path,
                    manifest_path,
                    created_at or now_iso(),
                    trigger,
                    int(file_count),
                    int(total_bytes),
                    status,
                    message,
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)

    def list_backups(self, game_id: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._lock:
            c = self._c()
            if game_id:
                rows = c.execute(
                    "SELECT * FROM backups WHERE game_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (game_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM backups ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def last_backup(self, game_id: str) -> Optional[Dict[str, Any]]:
        rows = self.list_backups(game_id, limit=1)
        return rows[0] if rows else None

    def backup_count(self, game_id: str) -> int:
        with self._lock:
            c = self._c()
            row = c.execute(
                "SELECT COUNT(*) AS n FROM backups WHERE game_id = ? AND status = 'ok'", (game_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def game_stats(self) -> Dict[str, Dict[str, Any]]:
        """一次性取出每个游戏的备份数与最近备份时间（避免 N+1 查询）。"""
        with self._lock:
            c = self._c()
            rows = c.execute(
                "SELECT game_id, COUNT(*) AS n, MAX(created_at) AS last_at"
                " FROM backups WHERE status = 'ok' GROUP BY game_id"
            ).fetchall()
        return {
            r["game_id"]: {"count": int(r["n"]), "last_at": r["last_at"] or ""} for r in rows
        }

    def delete_backup_rows(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        with self._lock:
            c = self._c()
            c.executemany("DELETE FROM backups WHERE id = ?", [(i,) for i in ids])
            c.commit()

    # -- 事件 ---------------------------------------------------------------

    def log_event(self, kind: str, message: str, game_id: str = "") -> None:
        try:
            with self._lock:
                c = self._c()
                c.execute(
                    "INSERT INTO events(ts, kind, game_id, message) VALUES (?,?,?,?)",
                    (now_iso(), kind, game_id, message),
                )
                c.commit()
        except sqlite3.Error:
            # 事件日志失败不应影响主流程
            pass

    def recent_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            c = self._c()
            rows = c.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    # -- 一致性检查工具（调试用） --------------------------------------------

    def prune_missing_paths(self, valid_save_paths: Sequence[str]) -> int:
        """删除基线中已不再属于任何游戏的存档路径记录。返回删除行数。"""
        keys = {path_key(p) for p in valid_save_paths}
        with self._lock:
            c = self._c()
            rows = c.execute("SELECT DISTINCT save_path FROM baseline_files").fetchall()
            doomed = [r["save_path"] for r in rows if path_key(r["save_path"]) not in keys]
            for sp in doomed:
                c.execute("DELETE FROM baseline_files WHERE save_path = ?", (sp,))
            c.commit()
        return len(doomed)
