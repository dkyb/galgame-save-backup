# -*- coding: utf-8 -*-
"""配置读写。

配置文件固定为 ``<总数据根目录>\\config.json``，不存在任何其它位置的副本。
写入前会经过 :class:`galguard.paths.PathGuard` 白名单校验。
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SCHEMA_VERSION
from .models import DetectSources, GameEntry
from .paths import PathGuard, normalize

__all__ = [
    "CONFIG_FILENAME",
    "ScanOptions",
    "RetentionPolicy",
    "MonitorOptions",
    "AppConfig",
    "ConfigStore",
]

CONFIG_FILENAME = "config.json"

# 备份时默认忽略的临时/垃圾文件
DEFAULT_IGNORE_GLOBS: List[str] = [
    "*.tmp",
    "*.temp",
    "*.part",
    "*.partial",
    "*.crdownload",
    "*.download",
    "~$*",
    "*.lock",
    "*.pid",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
]


@dataclass
class ScanOptions:
    """扫描选项。"""

    compute_sha256: bool = True       # 是否计算 SHA256
    compare_mtime: bool = True        # 是否比较修改时间
    hash_max_mb: int = 64             # 超过该大小的文件不算哈希（避免卡顿）
    max_depth: int = 5                # 游戏库递归深度上限
    max_dirs: int = 20000             # 单次扫描最多遍历的目录数（安全阀）
    detect_steam: bool = True         # 是否扫描 Steam 库
    scan_user_save_locations: bool = True  # 是否只读扫描文档/已保存的游戏/AppData
    sniff_max_depth: int = 3          # “存档嗅探”在安装目录里的递归深度
    recent_days: int = 0              # >0 时，额外把最近 N 天内改过的存档目录纳入候选
    discover_system: bool = True      # 从注册表卸载项 / 开始菜单快捷方式 / 最近使用里发现游戏
    registry_save_search: bool = True  # 在 HKCU\Software 同名键里找存档路径（只读）
    manifest_path: str = ""           # Ludusavi manifest.yaml 路径（离线导入的存档位置数据库）
    max_save_paths: int = 5           # 每个游戏最多自动选中几个存档路径（其余只作提示）

    # --- 各类检测来源的独立开关（设置页「检测来源」可直接开关） -------------
    # 关掉之后对应的扫描阶段完全不执行，既不产生候选也不产生存档路径。
    find_registry_games: bool = True   # 注册表安装记录
    find_shortcuts: bool = True        # 开始菜单 / 桌面快捷方式
    find_recent_used: bool = True      # 「最近使用」记录
    scan_install_subdirs: bool = True  # 安装目录下的常见存档目录
    scan_install_root: bool = True     # 安装目录本身就是存档目录
    scan_config_files: bool = True     # 游戏自己配置文件里写明的路径
    sniff_install_dir: bool = True     # 存档嗅探：安装目录子树
    sniff_sibling_dirs: bool = True    # 存档嗅探：安装目录旁边的目录
    steam_cloud_saves: bool = True     # Steam 云存档 userdata
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ScanOptions":
        d = d or {}
        return cls(
            compute_sha256=bool(d.get("compute_sha256", True)),
            compare_mtime=bool(d.get("compare_mtime", True)),
            hash_max_mb=int(d.get("hash_max_mb", 64) or 64),
            max_depth=int(d.get("max_depth", 5) or 5),
            max_dirs=int(d.get("max_dirs", 20000) or 20000),
            detect_steam=bool(d.get("detect_steam", True)),
            scan_user_save_locations=bool(d.get("scan_user_save_locations", True)),
            sniff_max_depth=max(1, int(d.get("sniff_max_depth", 3) or 3)),
            recent_days=max(0, int(d.get("recent_days", 0) or 0)),
            discover_system=bool(d.get("discover_system", True)),
            registry_save_search=bool(d.get("registry_save_search", True)),
            manifest_path=str(d.get("manifest_path", "") or ""),
            max_save_paths=max(1, int(d.get("max_save_paths", 5) or 5)),
            find_registry_games=bool(d.get("find_registry_games", True)),
            find_shortcuts=bool(d.get("find_shortcuts", True)),
            find_recent_used=bool(d.get("find_recent_used", True)),
            scan_install_subdirs=bool(d.get("scan_install_subdirs", True)),
            scan_install_root=bool(d.get("scan_install_root", True)),
            scan_config_files=bool(d.get("scan_config_files", True)),
            sniff_install_dir=bool(d.get("sniff_install_dir", True)),
            sniff_sibling_dirs=bool(d.get("sniff_sibling_dirs", True)),
            steam_cloud_saves=bool(d.get("steam_cloud_saves", True)),
        )

    def sources(self) -> DetectSources:
        """把散落的开关归拢成一个 :class:`DetectSources` 交给扫描器。

        ``discover_system`` 是「系统痕迹发现」的总开关：
        它关掉时，注册表 / 快捷方式 / 最近使用三个子项一律不生效。
        """
        master = bool(self.discover_system)
        return DetectSources(
            steam=bool(self.detect_steam),
            registry_games=master and bool(self.find_registry_games),
            shortcuts=master and bool(self.find_shortcuts),
            recent_used=master and bool(self.find_recent_used),
            install_subdirs=bool(self.scan_install_subdirs),
            install_root=bool(self.scan_install_root),
            config_files=bool(self.scan_config_files),
            sniff_install=bool(self.sniff_install_dir),
            sniff_siblings=bool(self.sniff_sibling_dirs),
            user_locations=bool(self.scan_user_save_locations),
            steam_cloud=bool(self.steam_cloud_saves),
            registry_saves=bool(self.registry_save_search),
            manifest=True,
        )


@dataclass
class RetentionPolicy:
    """备份保留策略。默认不自动删除任何备份。"""

    mode: str = "none"      # none | count | days
    count: int = 30         # 每个游戏保留最近 N 份
    days: int = 30          # 保留最近 N 天

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RetentionPolicy":
        d = d or {}
        mode = str(d.get("mode", "none") or "none").lower()
        if mode not in ("none", "count", "days"):
            mode = "none"
        return cls(
            mode=mode,
            count=max(1, int(d.get("count", 30) or 30)),
            days=max(1, int(d.get("days", 30) or 30)),
        )


@dataclass
class MonitorOptions:
    """文件监控选项。"""

    debounce_seconds: int = 5         # 防抖秒数
    poll_interval_seconds: int = 5    # 无 watchdog 时的轮询间隔
    backend: str = "auto"             # auto | watchdog | polling

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "MonitorOptions":
        d = d or {}
        backend = str(d.get("backend", "auto") or "auto").lower()
        if backend not in ("auto", "watchdog", "polling"):
            backend = "auto"
        return cls(
            debounce_seconds=max(0, int(d.get("debounce_seconds", 5) or 0)),
            poll_interval_seconds=max(2, int(d.get("poll_interval_seconds", 5) or 5)),
            backend=backend,
        )


@dataclass
class AppConfig:
    """全局配置。"""

    version: int = SCHEMA_VERSION
    data_root: str = ""
    backup_root: str = ""
    library_roots: List[str] = field(default_factory=list)
    extra_save_dirs: List[str] = field(default_factory=list)
    ignore_globs: List[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_GLOBS))
    scan: ScanOptions = field(default_factory=ScanOptions)
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    monitor: MonitorOptions = field(default_factory=MonitorOptions)
    close_action: str = "tray"           # tray（最小化到托盘）| exit（直接彻底退出）
    auto_start_monitor: bool = True      # 程序启动后自动开始静默监控
    tray_notifications: bool = False     # 备份完成后是否弹托盘气泡
    log_level: str = "INFO"
    games: List[GameEntry] = field(default_factory=list)

    # -- 便捷方法 -----------------------------------------------------------

    def get_game(self, game_id: str) -> Optional[GameEntry]:
        for g in self.games:
            if g.id == game_id:
                return g
        return None

    def remove_game(self, game_id: str) -> Optional[GameEntry]:
        for i, g in enumerate(self.games):
            if g.id == game_id:
                return self.games.pop(i)
        return None

    def upsert_game(self, game: GameEntry) -> None:
        for i, g in enumerate(self.games):
            if g.id == game.id:
                self.games[i] = game
                return
        self.games.append(game)

    def all_save_paths(self) -> List[str]:
        out: List[str] = []
        for g in self.games:
            for p in g.save_paths_norm():
                if p not in out:
                    out.append(p)
        return out

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["scan"] = self.scan.to_dict()
        d["retention"] = self.retention.to_dict()
        d["monitor"] = self.monitor.to_dict()
        d["games"] = [g.to_dict() for g in self.games]
        return d

    def clone(self) -> "AppConfig":
        return AppConfig.from_dict(copy.deepcopy(self.to_dict()))

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "AppConfig":
        d = d or {}
        cfg = cls(
            version=int(d.get("version", SCHEMA_VERSION) or SCHEMA_VERSION),
            data_root=str(d.get("data_root", "") or ""),
            backup_root=str(d.get("backup_root", "") or ""),
            library_roots=[str(x) for x in (d.get("library_roots") or [])],
            extra_save_dirs=[str(x) for x in (d.get("extra_save_dirs") or [])],
            ignore_globs=[str(x) for x in (d.get("ignore_globs") or DEFAULT_IGNORE_GLOBS)],
            scan=ScanOptions.from_dict(d.get("scan")),
            retention=RetentionPolicy.from_dict(d.get("retention")),
            monitor=MonitorOptions.from_dict(d.get("monitor")),
            close_action=str(d.get("close_action", "tray") or "tray").lower(),
            auto_start_monitor=bool(d.get("auto_start_monitor", True)),
            tray_notifications=bool(d.get("tray_notifications", False)),
            log_level=str(d.get("log_level", "INFO") or "INFO").upper(),
            games=[GameEntry.from_dict(x) for x in (d.get("games") or [])],
        )
        if cfg.close_action not in ("tray", "exit"):
            cfg.close_action = "tray"
        return cfg


class ConfigStore:
    """``<data_root>\\config.json`` 的读写器。"""

    def __init__(self, guard: PathGuard) -> None:
        self.guard = guard

    @property
    def path(self) -> Path:
        return self.guard.data_root / CONFIG_FILENAME

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> AppConfig:
        raw = self.guard.read_json(self.path, default=None)
        cfg = AppConfig.from_dict(raw)
        # 以运行时实际选择的根目录为准，避免用户手工拷贝配置后路径错乱
        cfg.data_root = str(self.guard.data_root)
        cfg.backup_root = str(self.guard.backup_root)
        if not cfg.library_roots:
            cfg.library_roots = []
        return cfg

    def save(self, cfg: AppConfig) -> Path:
        cfg.data_root = str(self.guard.data_root)
        if not cfg.backup_root:
            cfg.backup_root = str(self.guard.backup_root)
        return self.guard.write_json(self.path, cfg.to_dict())

    # -- 首次建立目录结构 ----------------------------------------------------

    def bootstrap_dirs(self) -> Dict[str, Path]:
        """在数据根目录内建立标准子目录结构。"""
        dirs = {}
        for name in ("logs", "cache", "temp", "data", "rules", "exports"):
            dirs[name] = self.guard.ensure_dir(self.guard.data_root / name)
        dirs["backups"] = self.guard.ensure_dir(self.guard.backup_root)
        return dirs


def resolve_roots(cfg: AppConfig, guard: PathGuard) -> None:
    """把配置里的备份根目录应用到守卫（若与数据根不同则加入白名单）。"""
    if cfg.backup_root:
        guard.set_backup_root(normalize(cfg.backup_root))
