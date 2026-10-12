# -*- coding: utf-8 -*-
"""路径沙盒。

本模块是整个“零污染”承诺的技术核心：

* 程序在用户选定“总数据根目录”之前，``PathGuard`` 处于**未解锁**状态，
  任何 ``check_write`` 都会抛异常，因此程序不可能在用户选择之前写入任何文件。
* 解锁之后，**唯一**允许写入的区域是：总数据根目录 与其子目录、备份根目录 与其子目录。
* 所有需要写文件的地方都必须调用 ``GUARD.check_write(...)`` 或
  ``GUARD.ensure_dir(...)``，而不是直接 ``open()`` / ``mkdir()``。

被显式禁止的位置（即使它们存在也不允许写）：
    %APPDATA% / %LOCALAPPDATA% / %TEMP% / 文档 / 已保存的游戏 /
    程序安装目录 / 游戏安装目录 / 游戏存档目录 / 注册表 / 启动文件夹 / 计划任务
其中后三项即使给出路径也不会被本模块放行（由沙盒本身的“白名单”语义保证）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

PathLike = Union[str, os.PathLike]

__all__ = [
    "SandboxViolation",
    "normalize",
    "path_key",
    "is_within",
    "is_protected",
    "PathGuard",
    "GUARD",
]


class SandboxViolation(RuntimeError):
    """试图在未授权位置进行写入时抛出。"""


# ---------------------------------------------------------------------------
# 基础路径工具
# ---------------------------------------------------------------------------

def normalize(p: PathLike) -> Path:
    """展开环境变量与 ``~``，并转成绝对路径（不解析符号链接，避免网络盘卡顿）。"""
    raw = os.path.expandvars(str(p))
    raw = os.path.expanduser(raw)
    # 去掉用户可能粘贴进来的首尾引号
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    try:
        return Path(raw).absolute()
    except OSError:
        return Path(os.path.abspath(raw))


def path_key(p: PathLike) -> str:
    """用于比较的规范化字符串（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(str(normalize(p))))


def is_within(child: PathLike, root: PathLike) -> bool:
    """判断 ``child`` 是否等于 ``root`` 或位于 ``root`` 之下。"""
    c = path_key(child)
    r = path_key(root)
    if c == r:
        return True
    if not r.endswith(os.sep):
        r = r + os.sep
    return c.startswith(r)


def _prefix_of(root: PathLike) -> str:
    s = os.path.normcase(os.path.normpath(str(root)))
    return s if s.endswith(os.sep) else s + os.sep


def _under_prefixes(p: PathLike, prefixes: Iterable[str]) -> bool:
    s = os.path.normcase(os.path.normpath(str(p)))
    for pre in prefixes:
        if s == pre[:-1] or s.startswith(pre):
            return True
    return False


def _sensitive_roots() -> List[Path]:
    """收集“系统敏感目录”，仅用于生成友好的错误提示。"""
    names = [
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "SystemRoot",
    ]
    out: List[Path] = []
    for n in names:
        v = os.environ.get(n)
        if v:
            out.append(normalize(v))
    up = os.environ.get("USERPROFILE")
    if up:
        out.append(normalize(up) / "Documents")
        out.append(normalize(up) / "Saved Games")
        out.append(normalize(up) / "Desktop")
    return out


# ---------------------------------------------------------------------------
# PathGuard
# ---------------------------------------------------------------------------

class PathGuard:
    """写入白名单守卫（进程内单例，见文件末尾 ``GUARD``）。"""

    def __init__(self) -> None:
        self._data_root: Optional[Path] = None
        self._backup_root: Optional[Path] = None
        # 额外的只读目录（例如游戏库根目录、存档目录），永远不会被写入
        self._read_roots: List[Path] = []
        self._sensitive = _sensitive_roots()
        #: 数据根 / 备份根的规范化前缀缓存（用于快速判断“这是程序自己的目录”）
        self._protected_prefixes: Optional[List[str]] = None

    # -- 状态 ---------------------------------------------------------------

    @property
    def unlocked(self) -> bool:
        """是否已经由用户指定了数据根目录。"""
        return self._data_root is not None

    @property
    def data_root(self) -> Path:
        self._require_unlocked()
        assert self._data_root is not None
        return self._data_root

    @property
    def backup_root(self) -> Path:
        self._require_unlocked()
        assert self._backup_root is not None
        return self._backup_root

    @property
    def read_roots(self) -> Sequence[Path]:
        return tuple(self._read_roots)

    def _require_unlocked(self) -> None:
        if self._data_root is None:
            raise SandboxViolation(
                "总数据根目录尚未由用户指定，程序此时不允许写入任何文件。"
            )

    # -- 解锁 ---------------------------------------------------------------

    def set_data_root(self, p: PathLike) -> Path:
        root = normalize(p)
        self._data_root = root
        if self._backup_root is None:
            self._backup_root = root / "backups"
        self._protected_prefixes = None
        return root

    def set_backup_root(self, p: PathLike) -> Path:
        root = normalize(p)
        self._backup_root = root
        self._protected_prefixes = None
        return root

    def add_read_root(self, p: PathLike) -> None:
        """登记一个只读目录（游戏库 / 存档目录 / Steam 库）。"""
        try:
            path = normalize(p)
        except OSError:
            return
        if all(path_key(path) != path_key(x) for x in self._read_roots):
            self._read_roots.append(path)

    def clear_read_roots(self) -> None:
        self._read_roots.clear()

    # -- 查询 ---------------------------------------------------------------

    def writable_roots(self) -> List[Path]:
        roots: List[Path] = []
        if self._data_root is not None:
            roots.append(self._data_root)
        if self._backup_root is not None and all(
            path_key(self._backup_root) != path_key(r) for r in roots
        ):
            roots.append(self._backup_root)
        return roots

    def is_writable(self, p: PathLike) -> bool:
        return any(is_within(p, r) for r in self.writable_roots())

    # -- “禁区”：程序自己的数据根 / 备份根 ----------------------------------

    def protected_prefixes(self) -> List[str]:
        """返回数据根 / 备份根的规范化前缀（带缓存，便于在热循环里快速判断）。"""
        if self._protected_prefixes is None:
            self._protected_prefixes = [_prefix_of(r) for r in self.writable_roots()]
        return self._protected_prefixes

    def is_protected(self, p: PathLike) -> bool:
        """``p`` 是否位于程序自己的数据根 / 备份根内。

        ★ 这类目录**绝不能被当成游戏目录、存档目录或待备份文件**，
        否则会“备份套备份”无限膨胀，甚至触发监控回路
        （备份写入 → 被监控到 → 再备份一次）。
        """
        if not self.unlocked:
            return False
        try:
            return _under_prefixes(p, self.protected_prefixes())
        except (OSError, ValueError):
            return False

    def check_write(self, p: PathLike) -> Path:
        """校验写入路径；不合法时抛 :class:`SandboxViolation`。"""
        path = normalize(p)
        if not self.unlocked:
            raise SandboxViolation(
                f"用户尚未指定总数据根目录，拒绝写入：{path}"
            )
        if not self.is_writable(path):
            hint = ""
            for s in self._sensitive:
                if is_within(path, s):
                    hint = f"（该位置属于系统敏感目录：{s}）"
                    break
            raise SandboxViolation(
                "零污染策略拒绝写入未授权路径：\n"
                f"  {path}\n"
                f"  当前允许写入的根目录只有：\n"
                + "\n".join(f"    - {r}" for r in self.writable_roots())
                + hint
            )
        return path

    def check_read(self, p: PathLike) -> Path:
        """读取路径只做规范化；读取范围由调用方决定（扫描只读，不写）。"""
        return normalize(p)

    # -- 便捷写操作（全部经过白名单） -----------------------------------------

    def ensure_dir(self, p: PathLike) -> Path:
        path = self.check_write(p)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def subdir(self, name: str, root: Optional[PathLike] = None) -> Path:
        """在数据根目录（或指定白名单根）下取得并创建子目录。"""
        base = normalize(root) if root is not None else self.data_root
        return self.ensure_dir(base / name)

    def write_text(self, p: PathLike, text: str, encoding: str = "utf-8") -> Path:
        path = self.check_write(p)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换，降低“写一半被中断”的概率（临时文件同样在白名单内）
        tmp = path.with_name(path.name + ".tmp")
        self.check_write(tmp)
        with open(tmp, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path

    def write_json(self, p: PathLike, obj: object) -> Path:
        return self.write_text(p, json.dumps(obj, ensure_ascii=False, indent=2))

    def read_json(self, p: PathLike, default=None):
        path = self.check_read(p)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"<PathGuard data_root={self._data_root} backup_root={self._backup_root} "
            f"unlocked={self.unlocked}>"
        )


#: 进程内全局守卫
GUARD = PathGuard()


def is_protected(p: PathLike) -> bool:
    """模块级快捷方式：``p`` 是否位于程序自己的数据根 / 备份根内。"""
    return GUARD.is_protected(p)
