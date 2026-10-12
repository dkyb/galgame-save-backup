# -*- coding: utf-8 -*-
"""文件系统小工具：哈希、安全遍历、失败重试、人类可读大小。

刻意保持“只读 + 只复制”的语义：本模块不提供任何删除源文件的函数。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .logsetup import get_logger
from .paths import normalize, path_key

__all__ = [
    "sha256_file",
    "clear_hash_cache",
    "iter_save_files",
    "count_tree",
    "human_bytes",
    "safe_dir_name",
    "retry_call",
    "walk_files",
]

log = get_logger("fsutil")

_HASH_CACHE: Dict[Tuple[str, int, int], str] = {}
_HASH_CACHE_LOCK = threading.Lock()
_HASH_CACHE_MAX = 4096


def sha256_file(path: Path, chunk_size: int = 1024 * 1024,
                cancel: Optional[Any] = None) -> str:
    """计算文件 SHA256，带内存缓存（缓存不落盘）。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path_key(path), int(st.st_size), int(st.st_mtime))
    with _HASH_CACHE_LOCK:
        hit = _HASH_CACHE.get(key)
    if hit is not None:
        return hit

    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    return ""
                block = fh.read(chunk_size)
                if not block:
                    break
                h.update(block)
    except OSError as exc:
        log.debug("计算哈希失败 %s：%s", path, exc)
        return ""

    digest = h.hexdigest()
    with _HASH_CACHE_LOCK:
        if len(_HASH_CACHE) > _HASH_CACHE_MAX:
            _HASH_CACHE.clear()
        _HASH_CACHE[key] = digest
    return digest


def clear_hash_cache() -> None:
    with _HASH_CACHE_LOCK:
        _HASH_CACHE.clear()


def walk_files(root: Path, cancel: Optional[Any] = None) -> Iterator[Tuple[Path, str]]:
    """遍历目录下所有文件，产出 ``(绝对路径, 相对路径 posix 风格)``。

    * 不跟随符号链接 / 目录联接（避免递归死循环与越界读取）。
    * 目录不可读时跳过，不抛异常。
    """
    root = normalize(root)
    if not root.is_dir():
        return
    stack: List[Path] = [root]
    while stack:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            return
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    abs_p = Path(e.path)
                    try:
                        rel = os.path.relpath(str(abs_p), str(root)).replace("\\", "/")
                    except ValueError:
                        rel = abs_p.name
                    yield abs_p, rel
            except OSError:
                continue


def iter_save_files(root: Path, rules: Any, cancel: Optional[Any] = None
                    ) -> Iterator[Tuple[Path, str]]:
    """遍历存档目录，自动跳过临时文件与垃圾文件。"""
    for abs_p, rel in walk_files(root, cancel=cancel):
        try:
            if rules is not None and rules.is_temp_file(abs_p.name):
                continue
        except Exception:
            pass
        yield abs_p, rel


def count_tree(root: Path) -> Tuple[int, int]:
    """返回 ``(文件数, 总字节数)``。"""
    n = 0
    total = 0
    for p, _ in walk_files(root):
        n += 1
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return n, total


def human_bytes(n: int) -> str:
    step = 1024.0
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < step or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= step
    return f"{v:.1f} TB"


_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_dir_name(name: str, fallback: str = "unnamed") -> str:
    """把游戏名转成安全的目录名。"""
    out = _BAD_NAME.sub("_", str(name or "")).strip()
    out = out.rstrip(". ")
    if not out:
        out = fallback
    # 规避 Windows 保留名
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if out.upper() in reserved:
        out = "_" + out
    return out[:120]


def retry_call(fn: Callable[[], Any], times: int = 3, delay: float = 0.4,
               on_error: Optional[Callable[[int, Exception], None]] = None) -> Any:
    """失败重试；全部失败时抛出最后一次异常。"""
    last: Optional[Exception] = None
    for i in range(max(1, times)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 复制失败原因可能五花八门
            last = exc
            if on_error is not None:
                try:
                    on_error(i + 1, exc)
                except Exception:
                    pass
            if i + 1 < times:
                time.sleep(delay * (i + 1))
    assert last is not None
    raise last
