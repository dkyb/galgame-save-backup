# -*- coding: utf-8 -*-
"""Ludusavi 存档位置数据库（manifest.yaml）离线导入。

**为什么要有这个模块**

目前最主流的开源存档备份工具 Ludusavi，它的做法是：
把 [PCGamingWiki](https://www.pcgamingwiki.com) 上人工维护的“存档位置”数据
定期编译成一份 ``manifest.yaml``（见
[mtkennerly/ludusavi-manifest](https://github.com/mtkennerly/ludusavi-manifest)），
然后按**游戏标题**匹配，把 ``<base>`` / ``<winAppData>`` 这类占位符展开成真实路径。

这份社区数据库覆盖了大量商业游戏；对**自下资源的汉化 galgame** 覆盖率一般，
但它是最省事、最权威的一条补充途径，所以本工具支持**离线导入**：

    1. 你自己去下载 manifest.yaml（本程序不联网、不下载）
    2. 在「设置 → 扫描 → Ludusavi 存档数据库」里选中它
    3. 之后每次扫描，程序会用游戏名去数据库里查存档位置，
       命中的路径如果在本机真实存在，就直接作为候选存档路径

**实现说明**：为了避免引入 PyYAML 依赖（也为了控制体积和速度），
这里用面向该文件格式的**逐行解析器**。manifest.yaml 的结构非常规整：

.. code-block:: yaml

    "Game Name":
      files:
        "<base>/save": {}
        "<winAppData>/Foo/*": {}
      registry:
        "HKEY_CURRENT_USER/Software/Foo": {}
      installDir:
        "<base>": {}

只取 ``files`` / ``installDir`` / ``registry`` 三段的键，
缩进层数固定（0=游戏名，2=段名，4=路径），
``when:`` 之类的条件块缩进更深，不会被误当成路径。
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .logsetup import get_logger
from .paths import normalize, path_key

__all__ = ["LudusaviManifest", "MANIFEST_FILENAME"]

log = get_logger("manifest")

MANIFEST_FILENAME = "ludusavi_manifest.yaml"

_SECTION_NAMES = {"files", "installDir", "registry", "arguments", "id", "steam"}
_PATH_SECTIONS = {"files", "installDir"}

_GAME_RE = re.compile(r'^"((?:[^"\\]|\\.)*)":\s*$')
_SECTION_RE = re.compile(r"^  ([A-Za-z][A-Za-z0-9_]*):\s*$")
_ENTRY_RE = re.compile(r'^    "((?:[^"\\]|\\.)*)":')


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


class LudusaviManifest:
    """一份内存里的 Lutdusavi 数据库。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path: Optional[Path] = path
        #: 原始名 -> {"files": [...], "installDir": [...], "registry": [...]}
        self.games: Dict[str, Dict[str, List[str]]] = {}
        #: 归一化名 -> 原始名（用于模糊匹配）
        self._norm_index: Dict[str, str] = {}
        self.loaded_at: str = ""
        self.source_size: int = 0
        self.errors: List[str] = []

    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return bool(self.games)

    def __len__(self) -> int:
        return len(self.games)

    @staticmethod
    def _norm_name(s: str) -> str:
        # 去掉罗马数字/版本后缀、标点、空格，便于“标题对不上”时仍能匹配
        s = (s or "").lower()
        s = re.sub(r"[\u2122\u00ae\u00a9]", "", s)
        s = re.sub(r"\b(goty|definitive|deluxe|ultimate|complete|enhanced|remastered|edition)\b", "", s)
        s = re.sub(r"[\s\-_\.·:：!！?？'\"\[\]【】()（）,，/\\]+", "", s)
        return s

    # ------------------------------------------------------------------

    def load(self, path: Path, max_bytes: int = 256 * 1024 * 1024) -> bool:
        """加载 manifest.yaml。返回是否成功。"""
        path = Path(path)
        self.errors = []
        if not path.is_file():
            self.errors.append(f"文件不存在：{path}")
            return False
        try:
            size = path.stat().st_size
        except OSError as exc:
            self.errors.append(str(exc))
            return False
        if size > max_bytes:
            self.errors.append(f"文件过大（{size // 1048576} MB），已拒绝加载")
            return False

        games: Dict[str, Dict[str, List[str]]] = {}
        cur_game: Optional[str] = None
        cur_section: Optional[str] = None

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    # 顶层：游戏名（缩进 0）
                    if not line.startswith(" "):
                        m = _GAME_RE.match(line.rstrip("\n"))
                        if m:
                            cur_game = _unescape(m.group(1))
                            games.setdefault(cur_game, {})
                            cur_section = None
                        continue
                    if cur_game is None:
                        continue
                    # 第二层：段名（缩进 2）
                    if line.startswith("  ") and not line.startswith("    "):
                        m = _SECTION_RE.match(line.rstrip("\n"))
                        if m:
                            sec = m.group(1)
                            cur_section = sec if sec in _PATH_SECTIONS else None
                            if cur_section:
                                games[cur_game].setdefault(cur_section, [])
                        continue
                    # 第三层：路径条目（缩进 4）
                    if cur_section and line.startswith("    ") and not line.startswith("      "):
                        m = _ENTRY_RE.match(line)
                        if m:
                            val = _unescape(m.group(1))
                            games[cur_game].setdefault(cur_section, []).append(val)
        except OSError as exc:
            self.errors.append(f"读取失败：{exc}")
            return False

        # 丢掉没有任何路径的游戏
        games = {k: v for k, v in games.items() if v.get("files") or v.get("installDir")}

        self.games = games
        self.path = path
        self.source_size = size
        self._norm_index = {}
        for name in games:
            k = self._norm_name(name)
            if k and k not in self._norm_index:
                self._norm_index[k] = name
        import time

        self.loaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
        log.info("Ludusavi 数据库已加载：%d 个游戏（%.1f MB）", len(games), size / 1048576)
        return True

    def save_index(self, index_path: Path) -> None:
        """把“归一化名 -> 原始名”的索引写到数据根目录（加速下次加载）。"""
        try:
            index_path.write_text(
                json.dumps(self._norm_index, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------

    def lookup(self, game_name: str) -> Optional[Dict[str, List[str]]]:
        """按游戏名查存档定义。先精确、再归一化、最后子串模糊匹配。"""
        if not self.games or not game_name:
            return None
        if game_name in self.games:
            return self.games[game_name]

        key = self._norm_name(game_name)
        if not key or len(key) < 3:
            return None
        hit = self._norm_index.get(key)
        if hit:
            return self.games[hit]

        # 模糊：数据库里最长的一个能包含它的键
        best_name = ""
        best_len = 0
        for nk, orig in self._norm_index.items():
            if len(nk) < 3:
                continue
            if nk in key or key in nk:
                if len(nk) > best_len:
                    best_len = len(nk)
                    best_name = orig
        if best_name and best_len >= max(4, len(key) - 4):
            return self.games[best_name]
        return None

    # ------------------------------------------------------------------

    def _placeholders(self, install_dir: str, extra_roots: Sequence[str] = ()) -> Dict[str, List[str]]:
        up = os.environ.get("USERPROFILE", "")
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        programdata = os.environ.get("ProgramData", "")
        public = os.environ.get("PUBLIC", "")
        windir = os.environ.get("SystemRoot", r"C:\Windows")

        roots: List[str] = []
        for r in extra_roots:
            if r:
                roots.append(str(normalize(r)))
        if install_dir:
            parent = str(Path(install_dir).parent)
            if all(path_key(parent) != path_key(x) for x in roots):
                roots.append(parent)
        if not roots and install_dir:
            roots.append(install_dir)

        return {
            "<base>": [install_dir] if install_dir else [],
            "<home>": [up] if up else [],
            "<winAppData>": [appdata] if appdata else [],
            "<winLocalAppData>": [local] if local else [],
            "<winLocalAppDataLow>": [str(Path(up) / "AppData" / "LocalLow")] if up else [],
            "<winDocuments>": [str(Path(up) / "Documents")] if up else [],
            "<winSavedGames>": [str(Path(up) / "Saved Games")] if up else [],
            "<winPublic>": [public] if public else [],
            "<winProgramData>": [programdata] if programdata else [],
            "<winDir>": [windir] if windir else [],
            "<osUserName>": [os.environ.get("USERNAME", "")],
            "<root>": roots,
        }

    def expand(self, template: str, install_dir: str,
               extra_roots: Sequence[str] = ()) -> List[str]:
        """把一条 manifest 路径模板展开成本机的真实路径列表（只返回存在的）。"""
        if not template:
            return []
        ph = self._placeholders(install_dir, extra_roots)
        out: List[str] = []
        for combo in _expand_placeholders(template, ph):
            for real in _resolve_pattern(combo):
                if all(path_key(real) != path_key(x) for x in out):
                    out.append(real)
        return out

    def save_paths_for(self, game_name: str, install_dir: str = "",
                       extra_roots: Sequence[str] = ()) -> Tuple[List[str], List[str]]:
        """返回 ``(存在的存档路径, 判定依据)``。"""
        entry = self.lookup(game_name)
        if not entry:
            return [], []
        found: List[str] = []
        why: List[str] = []
        for tmpl in list(entry.get("files", [])) + list(entry.get("installDir", [])):
            for real in self.expand(tmpl, install_dir, extra_roots):
                if all(path_key(real) != path_key(x) for x in found):
                    found.append(real)
                    why.append(f"Ludusavi 数据库：{tmpl}")
        return found, why

    def install_dir_hints(self, game_name: str) -> List[str]:
        entry = self.lookup(game_name)
        if not entry:
            return []
        return list(entry.get("installDir", []))


def _expand_placeholders(template: str, ph: Dict[str, List[str]],
                         limit: int = 12) -> List[str]:
    """按占位符笛卡尔积展开模板（最多 ``limit`` 个组合）。"""
    parts: List[Tuple[str, List[str]]] = []
    rest = template
    while rest:
        pos = rest.find("<")
        if pos < 0:
            parts.append((rest, [rest]))
            break
        end = rest.find(">", pos)
        if end < 0:
            parts.append((rest, [rest]))
            break
        if pos > 0:
            parts.append((rest[:pos], [rest[:pos]]))
        token = rest[pos:end + 1]
        values = ph.get(token)
        if values is None:
            # 未知占位符：原样保留，避免猜错
            parts.append((token, [token]))
        else:
            vals = [v for v in values if v]
            parts.append((token, vals or [token]))
        rest = rest[end + 1:]

    out: List[str] = [""]
    for _tok, vals in parts:
        nxt: List[str] = []
        for prefix in out:
            for v in vals:
                nxt.append(prefix + v)
                if len(nxt) >= limit:
                    break
            if len(nxt) >= limit:
                break
        out = nxt
        if not out:
            break
    return out


def _resolve_pattern(path_str: str) -> List[str]:
    """把可能含通配符的路径解析成本机真实存在的**目录**列表。"""
    if not path_str:
        return []
    s = path_str.replace("/", os.sep)
    # Ludusavi 里 `<base>/save/*` 表示“save 目录里的所有文件”，
    # 对我们来说目标目录就是它的父目录。
    s = re.sub(r"[\\/]\*$", "", s)
    if "*" in s or "?" in s or "[" in s:
        out: List[str] = []
        try:
            for hit in _glob.glob(s):
                if os.path.isdir(hit):
                    out.append(str(normalize(hit)))
        except (OSError, re.error):
            return []
        return out
    try:
        p = normalize(s)
    except OSError:
        return []
    return [str(p)] if p.is_dir() else []
