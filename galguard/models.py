# -*- coding: utf-8 -*-
"""数据模型。

只放纯数据结构，不含 IO。所有时间统一使用本地时间的 ISO 字符串。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .paths import path_key, normalize

__all__ = [
    "now_iso",
    "make_game_id",
    "FileStamp",
    "BaselineDiff",
    "GameEntry",
    "Candidate",
    "ScanResult",
    "EngineHit",
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def make_game_id(name: str, install_dir: str) -> str:
    """由「游戏名 + 安装目录」派生稳定 ID，重复添加同一个游戏时 ID 不变。"""
    raw = f"{name.strip().lower()}|{path_key(install_dir) if install_dir else ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 文件戳 / 基线
# ---------------------------------------------------------------------------

@dataclass
class FileStamp:
    """一个存档文件的指纹。"""

    save_path: str        # 它属于哪个“存档路径”（已规范化）
    rel_path: str         # 相对该存档路径的子路径（posix 风格）
    size: int = 0
    mtime: float = 0.0
    sha256: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (path_key(self.save_path), self.rel_path.lower())

    def to_row(self) -> Dict[str, Any]:
        return {
            "save_path": self.save_path,
            "rel_path": self.rel_path,
            "size": self.size,
            "mtime": self.mtime,
            "sha256": self.sha256,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "FileStamp":
        return cls(
            save_path=row["save_path"],
            rel_path=row["rel_path"],
            size=int(row.get("size", 0)),
            mtime=float(row.get("mtime", 0.0)),
            sha256=row.get("sha256", "") or "",
        )


@dataclass
class BaselineDiff:
    """当前状态与基线的差异。"""

    game_id: str = ""
    added: List[FileStamp] = field(default_factory=list)
    modified: List[FileStamp] = field(default_factory=list)
    removed: List[FileStamp] = field(default_factory=list)
    missing_paths: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def is_changed(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.modified) + len(self.removed)

    def summary(self) -> str:
        if self.error:
            return f"扫描出错：{self.error}"
        if not self.is_changed:
            return "无更新"
        parts = []
        if self.added:
            parts.append(f"新增 {len(self.added)}")
        if self.modified:
            parts.append(f"修改 {len(self.modified)}")
        if self.removed:
            parts.append(f"删除 {len(self.removed)}")
        if self.missing_paths:
            parts.append(f"{len(self.missing_paths)} 个存档目录不存在")
        return "，".join(parts)



# ---------------------------------------------------------------------------
# 检测来源开关
# ---------------------------------------------------------------------------

@dataclass
class DetectSources:
    """每一类“检测来源”的独立开关。

    设置页「检测来源」里的复选框直接对应这里的字段 —— 用户可以把任何
    自己不需要的来源关掉（例如不想让程序读注册表、不想扫 Steam）。
    关掉之后对应的扫描阶段**完全不执行**，既不产生候选也不产生存档路径。
    """

    # -- 从哪里找游戏 --
    steam: bool = True            # Steam 库（只读注册表 + libraryfolders.vdf + appmanifest）
    registry_games: bool = True   # 注册表安装记录（安装版游戏）
    shortcuts: bool = True        # 开始菜单 / 桌面快捷方式（绿色版、免安装版）
    recent_used: bool = True      # 「最近使用」记录（你真正运行过的程序）

    # -- 从哪里找存档 --
    install_subdirs: bool = True  # 安装目录下的常见存档目录（save / savedata / セーブ …）
    install_root: bool = True     # 安装目录本身就是存档目录（NScripter 这类）
    config_files: bool = True     # 游戏自己的配置文件里写明的路径
    sniff_install: bool = True    # 存档嗅探：安装目录子树
    sniff_siblings: bool = True   # 存档嗅探：安装目录旁边的目录
    user_locations: bool = True   # 只读扫描 文档 / 已保存的游戏 / AppData / LocalLow
    steam_cloud: bool = True      # Steam 云存档 userdata\<id>\<appid>\remote
    registry_saves: bool = True   # HKCU\Software 同名键里记录的路径
    manifest: bool = True         # Ludusavi 离线数据库

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DetectSources":
        d = d or {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: bool(v) for k, v in d.items() if k in known}
        return cls(**data)


# ---------------------------------------------------------------------------
# 游戏条目
# ---------------------------------------------------------------------------

@dataclass
class GameEntry:
    """用户确认加入监控的一个游戏。"""

    id: str = ""
    name: str = ""
    engine: str = ""
    exe: str = ""
    install_dir: str = ""
    save_paths: List[str] = field(default_factory=list)
    backup_root: str = ""          # 留空表示使用全局备份根目录
    notes: str = ""
    source: str = "auto"           # auto | manual
    enabled: bool = True
    monitor: bool = True           # 是否对该游戏启用静默监控
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_game_id(self.name, self.install_dir)

    # -- 便捷属性 -----------------------------------------------------------

    @property
    def primary_save_path(self) -> str:
        return self.save_paths[0] if self.save_paths else ""

    def save_paths_norm(self) -> List[str]:
        out: List[str] = []
        for p in self.save_paths:
            if not p:
                continue
            n = str(normalize(p))
            if n not in out:
                out.append(n)
        return out

    def effective_backup_root(self, default_root: str) -> str:
        return self.backup_root or default_root

    def safe_folder_name(self) -> str:
        """用于备份目录名的安全化游戏名。"""
        bad = '<>:"/\\|?*'
        name = "".join("_" if c in bad or ord(c) < 32 else c for c in self.name).strip()
        name = name.rstrip(". ")
        return name or self.id

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GameEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in (d or {}).items() if k in known}
        data.setdefault("name", "")
        data.setdefault("install_dir", "")
        entry = cls(**data)
        entry.save_paths = [str(x) for x in (data.get("save_paths") or [])]
        return entry


# ---------------------------------------------------------------------------
# 扫描候选
# ---------------------------------------------------------------------------

@dataclass
class EngineHit:
    """引擎识别的单次命中。"""

    engine: str
    weight: int
    reasons: List[str] = field(default_factory=list)


@dataclass
class Candidate:
    """自动检测得到的候选游戏（需用户确认才会写入游戏列表）。"""

    name: str
    install_dir: str
    exe: str = ""
    engine: str = ""
    engine_confidence: str = "低"
    save_paths: List[str] = field(default_factory=list)
    save_evidence: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    score: int = 0
    source: str = "library"       # library | steam | manual
    selected: bool = True

    def key(self) -> str:
        return make_game_id(self.name, self.install_dir)


@dataclass
class ScanResult:
    """一次全量扫描的结果。"""

    candidates: List[Candidate] = field(default_factory=list)
    library_roots: List[str] = field(default_factory=list)
    steam_libraries: List[str] = field(default_factory=list)
    scanned_dirs: int = 0
    elapsed: float = 0.0
    errors: List[str] = field(default_factory=list)
    cancelled: bool = False
    # -- 诊断信息（用来回答“为什么一个都没找到”） --------------------------
    visited_dirs: int = 0                                   # 实际遍历过的目录总数
    root_reports: List[Dict[str, Any]] = field(default_factory=list)
    system_found: int = 0                                   # 系统痕迹（注册表/快捷方式）发现的游戏数
    manifest_hits: int = 0                                  # Ludusavi 数据库命中的游戏数

    def summary(self) -> str:
        return (
            f"遍历 {self.visited_dirs} 个目录，其中 {self.scanned_dirs} 个含 exe；"
            f"共 {len(self.candidates)} 个候选，耗时 {self.elapsed:.1f} 秒"
            + (f"，{len(self.errors)} 个错误" if self.errors else "")
        )

    def diagnosis(self) -> str:
        """给出“为什么候选很少 / 一个都没有”的可读诊断。"""
        lines: List[str] = [self.summary(), ""]

        if not self.library_roots:
            lines.append(
                "⚠ 没有配置任何「游戏库根目录」，所以只扫描了 Steam 库。\n"
                "  自己在网上下载的 galgame，需要把「装着这些游戏文件夹的那一层」加进来，\n"
                "  例如 D:\\GAL、E:\\Galgame。设置路径：设置 → 扫描 → 游戏库根目录。"
            )
        else:
            for rep in self.root_reports:
                lines.append(
                    f"· {rep.get('root')}\n"
                    f"    存在={rep.get('exists')}　遍历目录={rep.get('visited', 0)}　"
                    f"含 exe 的目录={rep.get('exe_dirs', 0)}　产出候选={rep.get('candidates', 0)}"
                    + (f"\n    {rep.get('hint')}" if rep.get("hint") else "")
                )
            total_exe = sum(int(r.get("exe_dirs", 0) or 0) for r in self.root_reports)
            if total_exe == 0:
                lines.append("")
                lines.append(
                    "⚠ 这些目录里没有找到任何含 .exe 的子目录。常见原因：\n"
                    "  · 把「游戏库根目录」指到了某一个游戏里面 —— 应该指到“装着一堆游戏文件夹”的那一层\n"
                    "  · 游戏还是压缩包没解压，或者放在外接硬盘/网络盘上\n"
                    "  · 目录层级超过了「递归深度上限」（设置 → 扫描）"
                )

        if self.system_found:
            lines.append("")
            lines.append(f"· 从注册表卸载项 / 快捷方式 / 最近使用里额外发现了 {self.system_found} 个游戏")
        if self.manifest_hits:
            lines.append(f"· Ludusavi 存档数据库命中了 {self.manifest_hits} 个游戏的存档位置")

        for err in self.errors[:8]:
            lines.append(f"× {err}")
        return "\n".join(lines)

