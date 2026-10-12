# -*- coding: utf-8 -*-
"""系统级游戏发现（**全部只读**）。

自己下的资源（汉化版 / 绿色版 / 免安装 / 光盘版）通常满足这些条件之一：

* 不在 Steam 库里；
* 不在你配置的“游戏库根目录”里（可能散落在 D:\\、E:\\ 各处）；
* 装完就忘了在哪。

所以光靠“扫我指定的目录”是不够的。本模块从三类**系统痕迹**里把它们翻出来：

1. **注册表卸载项** —— 安装版游戏一般会登记 ``DisplayName`` / ``InstallLocation``；
   绿色版没有，所以还需要第 2 条。
2. **快捷方式** —— 开始菜单 / 桌面 / 最近使用（``Recent``）里的 ``.lnk``。
   ``Recent`` 尤其有用：它记录的是**你真正运行过的**程序，
   正好对应需求里的“已安装或玩过”。
3. **注册表 HKCU\\Software 下的同名键** —— 很多日式引擎会把存档目录写在
   ``HKCU\\Software\\<厂商>\\<游戏>`` 里，这是找存档的一条捷径。

所有注册表访问都是 ``KEY_READ``，不创建、不修改、不删除任何键值；
磁盘上只读 ``.lnk`` 文件内容。**没有任何写操作。**
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .logsetup import get_logger
from .paths import normalize, path_key
from .rules import RuleSet

__all__ = [
    "SystemGame",
    "discover_registry_games",
    "discover_shortcut_games",
    "find_registry_save_paths",
    "looks_like_game_dir",
    "build_registry_uninstall_index",
]

log = get_logger("systemscan")

#: 兜底用的通用词（不含任何品牌名）。
#: 真正的名单在 detect_rules.json 的 ``non_game_name_keywords`` /
#: ``non_game_exe_prefixes`` 里，使用者可以自己增删。
_GENERIC_NOT_A_GAME = (
    "update for", "security update", "hotfix", "redistributable", "runtime",
    "driver", "sdk", "toolkit", "service", "launcher", "updater",
    "uninstaller", "installer",
    "运行库", "支持库", "组件", "驱动", "工具箱", "加速器", "模拟器",
)

_GENERIC_NOT_GAME_EXE = (
    "unins", "uninstall", "setup", "install", "vcredist", "dxsetup", "dxwebsetup",
    "python", "pythonw", "javaw", "node", "npm", "git", "code", "devenv",
    "maintenancetool", "updater", "update", "crashpad", "elevate",
)

_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\[^\\/]+[\\/][^\\/]+")


@dataclass
class SystemGame:
    """从系统痕迹里发现的一个可能的游戏。"""

    name: str
    install_dir: str = ""
    exe: str = ""
    source: str = ""           # registry | shortcut | recent
    publisher: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# 通用判断
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[\s\-_\.·:：!！?？\[\]【】()（）]+", "", (s or "")).lower()


def _is_path_like(s: str) -> bool:
    if not s or len(s) < 4:
        return False
    s = s.strip().strip('"')
    return bool(_DRIVE_PATH_RE.match(s) or _UNC_RE.match(s))


def _strip_icon_to_path(raw: str) -> str:
    """把 ``"C:\\a\\b.exe",0`` 这种 DisplayIcon 值还原成路径。"""
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 1:
            return s[1:end]
    s = re.sub(r",\s*-?\d+\s*$", "", s)
    return s.strip().strip('"')


def _not_a_game_name(name: str, rules: "RuleSet | None" = None) -> bool:
    """名字像不像“非游戏软件”。名单优先取规则文件里的，取不到用通用兜底。"""
    low = (name or "").lower()
    if not low:
        return True
    if rules is not None:
        try:
            return rules.is_non_game_name(low)
        except Exception:  # noqa: BLE001
            pass
    return any(tok in low for tok in _GENERIC_NOT_A_GAME)


def _not_a_game_exe(exe_name: str, rules: "RuleSet | None" = None) -> bool:
    low = (exe_name or "").lower()
    if rules is not None:
        try:
            return rules.is_non_game_exe(low)
        except Exception:  # noqa: BLE001
            pass
    return any(low.startswith(tok) for tok in _GENERIC_NOT_GAME_EXE)


def _largest_file(d: Path, max_depth: int = 2, limit: int = 800) -> int:
    """粗略估计目录里最大文件的大小（用来判断“像不像一个游戏”）。"""
    biggest = 0
    stack: List[Tuple[Path, int]] = [(d, 0)]
    seen = 0
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    seen += 1
                    if seen > limit:
                        return biggest
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((Path(e.path), depth + 1))
                        elif e.is_file(follow_symlinks=False):
                            sz = e.stat().st_size
                            if sz > biggest:
                                biggest = sz
                    except OSError:
                        continue
        except OSError:
            continue
    return biggest


def looks_like_game_dir(d: Path, rules: RuleSet,
                        min_big_file: int = 20 * 1024 * 1024) -> Tuple[bool, str]:
    """判断一个目录“像不像游戏安装目录”。

    通过条件（任一）：
      * 引擎特征命中（权重 ≥ 2）
      * 存在已知的存档目录名子目录
      * 目录里（含 2 层子目录）有 ≥ ``min_big_file`` 的大文件（游戏一般有资源包）

    这个门槛是用来过滤“开始菜单里那一堆小工具”的。
    """
    try:
        if not d.is_dir():
            return False, "目录不存在"
    except OSError:
        return False, "目录不可访问"

    hits = rules.detect_engines(d)
    if hits and hits[0].weight >= 2:
        return True, f"引擎特征：{hits[0].engine}（{'，'.join(hits[0].reasons)}）"

    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and e.name.lower() in rules.save_dir_names:
                        return True, f"含存档目录名 {e.name}\\"
                except OSError:
                    continue
    except OSError:
        pass

    big = _largest_file(d, max_depth=2)
    if big >= min_big_file:
        return True, f"含 {big // (1024 * 1024)} MB 的大文件（像游戏资源）"

    return False, "没有游戏特征"


# ---------------------------------------------------------------------------
# 1) 注册表卸载项
# ---------------------------------------------------------------------------

_UNINSTALL_KEYS = [
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0x00000200),   # WOW64_32KEY
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0x00000100),   # WOW64_64KEY
]


def build_registry_uninstall_index(limit: int = 4000) -> List[Dict[str, str]]:
    """读取注册表卸载项（只读），返回 ``[{name, install_location, icon, publisher, key}]``。"""
    out: List[Dict[str, str]] = []
    if os.name != "nt":
        return out
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return out

    hives = [(winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")]
    seen_keys: set = set()

    for hive, hive_name in hives:
        for sub, extra in _UNINSTALL_KEYS:
            root_key = f"{hive_name}\\{sub}"
            if (root_key, extra) in seen_keys:
                continue
            seen_keys.add((root_key, extra))
            try:
                with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | extra) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for i in range(min(count, limit)):
                        try:
                            sub_name = winreg.EnumKey(root, i)
                        except OSError:
                            continue
                        try:
                            with winreg.OpenKey(root, sub_name, 0, winreg.KEY_READ | extra) as item:
                                def _get(v: str) -> str:
                                    try:
                                        data, _t = winreg.QueryValueEx(item, v)
                                        return str(data) if data is not None else ""
                                    except OSError:
                                        return ""

                                # 系统组件/补丁/子项直接跳过
                                if _get("SystemComponent") in ("1", "0x1"):
                                    continue
                                if _get("ParentKeyName") or _get("ParentDisplayName"):
                                    continue
                                rel = _get("ReleaseType").lower()
                                if rel in ("update", "hotfix", "security update", "servicepack"):
                                    continue
                                name = _get("DisplayName")
                                if not name:
                                    continue
                                out.append({
                                    "name": name,
                                    "install_location": _strip_icon_to_path(_get("InstallLocation")),
                                    "icon": _strip_icon_to_path(_get("DisplayIcon")),
                                    "publisher": _get("Publisher"),
                                    "key": f"{hive_name}\\{sub}\\{sub_name}",
                                })
                        except OSError:
                            continue
            except OSError:
                continue

    log.info("注册表卸载项：读取到 %d 条", len(out))
    return out


def discover_registry_games(rules: RuleSet, limit: int = 300) -> List[SystemGame]:
    """从注册表卸载项里挑出“像游戏”的条目。"""
    out: List[SystemGame] = []
    for item in build_registry_uninstall_index():
        if len(out) >= limit:
            break
        name = item["name"]
        if _not_a_game_name(name, rules):
            continue

        install_dir = ""
        exe = ""
        for cand in (item.get("install_location", ""), ""):
            if cand and os.path.isdir(cand):
                install_dir = cand
                break
        if not install_dir:
            icon = item.get("icon", "")
            if icon and _is_path_like(icon) and os.path.isfile(icon):
                if icon.lower().endswith(".exe"):
                    exe = icon
                install_dir = str(Path(icon).parent)
            elif icon and os.path.isdir(icon):
                install_dir = icon
        if not install_dir or not os.path.isdir(install_dir):
            continue

        d = normalize(install_dir)
        ok, why = looks_like_game_dir(d, rules)
        if not ok:
            continue

        if not exe:
            from .detect import find_game_exe

            found = find_game_exe(d, rules)
            exe = str(found) if found else ""

        out.append(SystemGame(
            name=name,
            install_dir=str(d),
            exe=exe,
            source="registry",
            publisher=item.get("publisher", ""),
            detail=f"注册表卸载项：{item.get('key', '')}（{why}）",
        ))
    log.info("注册表发现候选游戏：%d 个", len(out))
    return out


# ---------------------------------------------------------------------------
# 2) 快捷方式（.lnk）
# ---------------------------------------------------------------------------

def _shortcut_dirs() -> List[Path]:
    dirs: List[Path] = []
    up = os.environ.get("USERPROFILE", "")
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("ProgramData", "")
    candidates = [
        Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" if programdata else None,
        Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" if appdata else None,
        Path(up) / "Desktop" if up else None,
        Path(programdata) / "Desktop" if programdata else None,
        Path(appdata) / "Microsoft" / "Windows" / "Recent" if appdata else None,
    ]
    for c in candidates:
        if c is not None and c.is_dir():
            dirs.append(normalize(c))
    return dirs


def _strings_from_bytes(data: bytes, min_len: int = 4) -> List[str]:
    """从二进制里抽出可打印字符串（ASCII 与 UTF-16LE 两种）。"""
    out: List[str] = []

    # UTF-16LE：按 2 字节对齐扫描
    for offset in (0, 1):
        buf: List[str] = []
        for i in range(offset, len(data) - 1, 2):
            ch = data[i] | (data[i + 1] << 8)
            if 32 <= ch < 0x10000 and ch not in (0xFFFE, 0xFFFF):
                buf.append(chr(ch))
            else:
                if len(buf) >= min_len:
                    out.append("".join(buf))
                buf = []
        if len(buf) >= min_len:
            out.append("".join(buf))

    # ASCII
    buf = []
    for b in data:
        if 32 <= b < 127:
            buf.append(chr(b))
        else:
            if len(buf) >= min_len:
                out.append("".join(buf))
            buf = []
    if len(buf) >= min_len:
        out.append("".join(buf))
    return out


def exe_from_lnk(path: Path) -> Optional[str]:
    """从 ``.lnk`` 里尽力解析出目标 exe 路径（不依赖 pywin32）。

    ``.lnk`` 是二进制格式，目标路径会以 ANSI 与 UTF-16 两种形式出现在里面，
    这里直接扫描字符串并挑出最像路径的那个 —— 对快捷方式而言足够可靠。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > 4 * 1024 * 1024:
        return None

    best: Optional[str] = None
    for s in _strings_from_bytes(data):
        s = s.strip()
        if not s.lower().endswith(".exe"):
            continue
        if not _is_path_like(s):
            continue
        try:
            p = normalize(s)
        except OSError:
            continue
        if p.is_file():
            # 优先“确实存在”的路径；同分时取更长的
            if best is None or len(str(p)) > len(best):
                best = str(p)
    return best


def discover_shortcut_games(rules: RuleSet, limit: int = 300,
                            include_recent: bool = True,
                            only_recent: bool = False) -> List[SystemGame]:
    """从开始菜单 / 桌面 / 最近使用里发现游戏。"""
    out: List[SystemGame] = []
    seen_exe: set = set()
    dirs = _shortcut_dirs()

    for base in dirs:
        if len(out) >= limit:
            break
        is_recent = base.name.lower() == "recent"
        if is_recent and not include_recent:
            continue
        if only_recent and not is_recent:
            continue
        try:
            walker: Iterable[Path] = (Path(p) for p in _iter_files(base, max_files=4000))
        except OSError:
            continue
        for lnk in walker:
            if len(out) >= limit:
                break
            if lnk.suffix.lower() != ".lnk":
                continue
            exe = exe_from_lnk(lnk)
            if not exe:
                continue
            k = path_key(exe)
            if k in seen_exe:
                continue
            seen_exe.add(k)

            ep = Path(exe)
            if _not_a_game_exe(ep.name, rules):
                continue
            low = str(ep).lower()
            if "\\windows\\" in low or "\\windowsapps\\" in low:
                continue
            install_dir = ep.parent
            ok, why = looks_like_game_dir(install_dir, rules)
            if not ok:
                continue

            name = lnk.stem if not is_recent else ep.stem
            name = rules.strip_game_name(name) or ep.stem
            # 名称过滤：排除常见的非游戏软件（Toolkit / SDK / 加速器 / 网盘 …）
            if _not_a_game_name(name, rules) or _not_a_game_name(install_dir.name, rules):
                continue
            out.append(SystemGame(
                name=name,
                install_dir=str(install_dir),
                exe=exe,
                source="recent" if is_recent else "shortcut",
                detail=("最近使用记录" if is_recent else f"快捷方式：{lnk.name}") + f"（{why}）",
            ))
    log.info("快捷方式发现候选游戏：%d 个", len(out))
    return out


def _iter_files(root: Path, max_files: int = 4000) -> Iterator[str]:
    n = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    n += 1
                    if n > max_files:
                        return
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            yield e.path
                    except OSError:
                        continue
        except OSError:
            continue


# ---------------------------------------------------------------------------
# 3) 注册表里的存档路径
# ---------------------------------------------------------------------------

def _collect_path_values(key: Any, winreg: Any, depth: int = 0,
                         max_depth: int = 2, out: Optional[List[Tuple[str, str]]] = None,
                         limit: int = 400) -> List[Tuple[str, str]]:
    res = out if out is not None else []
    if depth > max_depth or len(res) >= limit:
        return res
    try:
        n_sub, n_val, _ = winreg.QueryInfoKey(key)
    except OSError:
        return res
    for i in range(min(n_val, 200)):
        try:
            name, value, _t = winreg.EnumValue(key, i)
        except OSError:
            continue
        if not isinstance(value, str) or not value:
            continue
        cand = value.strip().strip('"')
        if not _is_path_like(cand):
            continue
        try:
            p = normalize(cand)
        except OSError:
            continue
        if p.is_dir():
            res.append((str(p), f"注册表值 {name}"))
            if len(res) >= limit:
                return res
    for i in range(min(n_sub, 60)):
        try:
            sub_name = winreg.EnumKey(key, i)
        except OSError:
            continue
        try:
            with winreg.OpenKey(key, sub_name, 0, winreg.KEY_READ) as sub:
                _collect_path_values(sub, winreg, depth + 1, max_depth, res, limit)
        except OSError:
            continue
    return res


def find_registry_save_paths(game_name: str, install_dir: str = "",
                             limit: int = 8) -> List[Tuple[str, str]]:
    """在 ``HKCU\\Software`` 下找与该游戏同名的键，并取出里面的路径值。

    很多日式引擎会把“存档目录”直接写在这里，是一条很有效的捷径。
    只读 ``KEY_READ``。
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []

    key_target = _norm(game_name)
    if len(key_target) < 3:
        return []
    folder_key = _norm(Path(install_dir).name) if install_dir else ""

    found: List[Tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software", 0, winreg.KEY_READ) as software:
            n_sub = winreg.QueryInfoKey(software)[0]
            vendors = []
            for i in range(min(n_sub, 400)):
                try:
                    vendors.append(winreg.EnumKey(software, i))
                except OSError:
                    continue
            for vendor in vendors:
                try:
                    with winreg.OpenKey(software, vendor, 0, winreg.KEY_READ) as vkey:
                        n2 = winreg.QueryInfoKey(vkey)[0]
                        names = [""]
                        for i in range(min(n2, 200)):
                            try:
                                names.append(winreg.EnumKey(vkey, i))
                            except OSError:
                                continue
                        for sub_name in names:
                            cand_key = _norm(vendor) + _norm(sub_name)
                            if key_target not in cand_key and (
                                not folder_key or folder_key not in cand_key
                            ):
                                continue
                            try:
                                target = (
                                    winreg.OpenKey(vkey, sub_name, 0, winreg.KEY_READ)
                                    if sub_name else vkey
                                )
                            except OSError:
                                continue
                            with target:
                                for p, why in _collect_path_values(target, winreg):
                                    full = f"{why}（HKCU\\Software\\{vendor}\\{sub_name}）"
                                    if all(path_key(p) != path_key(x[0]) for x in found):
                                        found.append((p, full))
                                    if len(found) >= limit:
                                        return found
                except OSError:
                    continue
    except OSError:
        return found
    return found
