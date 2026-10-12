# -*- coding: utf-8 -*-
"""自动检测（基于文件系统，**不监控任何游戏进程**）。

四大信息来源：

1. 用户指定的“游戏库根目录”及其子目录 —— 有限深度遍历，不全盘扫描。
2. Steam —— **只读**注册表取 Steam 安装路径，解析 ``libraryfolders.vdf``
   与 ``appmanifest_*.acf``，以及 ``userdata\\<steamid>\\<appid>\\remote``。
3. 常见用户级存档位置 —— 文档 / 已保存的游戏 / AppData / LocalLow（只读）。
4. 引擎启发式 —— 见 :mod:`galguard.rules`。

!!! 重要局限（README 中也会写明）!!!
    * 自动检测是**启发式**的，一定会有漏检和误检。
    * 部分游戏把存档放在注册表、加密容器或随机命名目录里，本工具无法推断。
    * 因此程序始终提供“手动添加游戏”兜底，并允许在设置里挂多个游戏库根目录。

本模块只做**读取**，任何情况下都不会写游戏目录或系统目录。
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .logsetup import get_logger
from .models import Candidate, DetectSources, EngineHit
from .pathgate import Decision, SavePathGate
from .paths import GUARD, is_protected, path_key, normalize
from .rules import RuleSet

__all__ = [
    "SteamInfo",
    "discover_steam",
    "read_registry_steam_path",
    "iter_candidate_game_dirs",
    "find_game_exe",
    "guess_game_name",
    "infer_save_paths",
    "looks_like_save_dir",
]

log = get_logger("detect")


# ---------------------------------------------------------------------------
# Steam（只读注册表）
# ---------------------------------------------------------------------------

@dataclass
class SteamInfo:
    steam_dir: Optional[Path] = None
    libraries: List[Path] = field(default_factory=list)
    userdata_dirs: List[Path] = field(default_factory=list)
    apps: List[Dict[str, Any]] = field(default_factory=list)   # appid/name/installdir/install_path

    @property
    def available(self) -> bool:
        return bool(self.libraries)


def read_registry_steam_path() -> Optional[Path]:
    """只用 ``KEY_READ`` 打开注册表读取 Steam 安装路径。

    本程序**从不**调用 ``CreateKey`` / ``SetValue``，注册表操作全部只读。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None

    probes = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for hive, sub, value in probes:
        for extra in (0, getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | extra) as key:
                    data, _ = winreg.QueryValueEx(key, value)
                    if data and os.path.isdir(data):
                        return normalize(data)
            except OSError:
                continue
    return None


def _parse_vdf_paths(text: str) -> List[str]:
    """从 libraryfolders.vdf 里抽出所有库路径。"""
    out: List[str] = []
    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
        raw = m.group(1).replace("\\\\", "\\")
        if raw:
            out.append(raw)
    # 老版本 vdf 用 "1" "D:\\SteamLibrary" 这种数字键
    for m in re.finditer(r'"\d+"\s*"([A-Za-z]:\\\\[^"]+)"', text):
        raw = m.group(1).replace("\\\\", "\\")
        if raw:
            out.append(raw)
    return out


def _find_steam_dir_by_guess() -> Optional[Path]:
    guesses = [
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam",
        r"D:\Program Files (x86)\Steam",
        r"E:\Steam",
    ]
    for g in guesses:
        p = Path(g)
        if (p / "steamapps").is_dir():
            return p
    return None


def discover_steam(rules: RuleSet, scan_userdata: bool = True) -> SteamInfo:
    """发现 Steam 库与已安装应用（只读）。"""
    info = SteamInfo()
    steam_dir = read_registry_steam_path() or _find_steam_dir_by_guess()
    if steam_dir is None or not steam_dir.is_dir():
        return info
    info.steam_dir = steam_dir

    libs: List[Path] = [steam_dir]
    for vdf in (steam_dir / "steamapps" / "libraryfolders.vdf",
                steam_dir / "config" / "libraryfolders.vdf"):
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for raw in _parse_vdf_paths(text):
                p = normalize(raw)
                if p.is_dir() and all(path_key(p) != path_key(x) for x in libs):
                    libs.append(p)

    info.libraries = [p for p in libs if (p / "steamapps").is_dir()]

    # 解析 appmanifest_*.acf
    for lib in info.libraries:
        apps_dir = lib / "steamapps"
        try:
            entries = list(os.scandir(apps_dir))
        except OSError:
            continue
        for e in entries:
            if not e.is_file():
                continue
            low = e.name.lower()
            if not (low.startswith("appmanifest_") and low.endswith(".acf")):
                continue
            try:
                text = Path(e.path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            appid = ""
            name = ""
            installdir = ""
            m = re.search(r'"appid"\s*"(\d+)"', text)
            if m:
                appid = m.group(1)
            m = re.search(r'"name"\s*"([^"]*)"', text)
            if m:
                name = m.group(1)
            m = re.search(r'"installdir"\s*"([^"]*)"', text)
            if m:
                installdir = m.group(1)
            if not appid:
                continue
            install_path = apps_dir / "common" / installdir if installdir else None
            info.apps.append({
                "appid": appid,
                "name": name or (installdir or appid),
                "installdir": installdir,
                "install_path": str(install_path) if install_path else "",
                "library": str(lib),
                "exists": bool(install_path and install_path.is_dir()),
            })

    if scan_userdata:
        ud_root = steam_dir / "userdata"
        if ud_root.is_dir():
            try:
                for e in os.scandir(ud_root):
                    if e.is_dir() and e.name.isdigit():
                        info.userdata_dirs.append(normalize(e.path))
            except OSError:
                pass

    log.info("Steam 库：%d 个，应用：%d 个", len(info.libraries), len(info.apps))
    return info


def _steam_userdata_save_dirs(
    info: SteamInfo, appid: str, rules: RuleSet
) -> List[Path]:
    """``userdata\\<steamid>\\<appid>\\remote`` 之类的云存档目录。"""
    out: List[Path] = []
    if not appid:
        return out
    subs = rules.steam_userdata_subdirs
    for ud in info.userdata_dirs:
        app_dir = ud / appid
        if not app_dir.is_dir():
            continue
        for sub in subs:
            p = app_dir / sub
            if p.is_dir():
                out.append(normalize(p))
        if not out:
            # 没有 remote/local 就直接用 appid 目录本身
            out.append(normalize(app_dir))
    return out


# ---------------------------------------------------------------------------
# 安装目录发现
# ---------------------------------------------------------------------------

def find_game_exe(install_dir: Path, rules: RuleSet,
                  allow_excluded_fallback: bool = True) -> Optional[Path]:
    """在安装目录顶层找一个最像“游戏主程序”的 exe。

    优先挑“不像安装器/启动器”的 exe；如果整个目录里只有
    ``launcher.exe`` / ``启动游戏.exe`` 这类被排除的名字，
    **兜底仍然返回最大的那个 exe** —— 自下资源里这种结构很常见，
    宁可多认一个，也不要整款游戏漏掉。
    """
    best: Optional[Path] = None
    best_size = -1
    fb: Optional[Path] = None
    fb_size = -1
    try:
        entries = list(os.scandir(install_dir))
    except OSError:
        return None
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            if not e.name.lower().endswith(".exe"):
                continue
            size = e.stat().st_size
        except OSError:
            continue
        if rules.is_excluded_exe(e.name):
            if size > fb_size:
                fb_size = size
                fb = normalize(e.path)
            continue
        if size > best_size:
            best_size = size
            best = normalize(e.path)

    if best is None and allow_excluded_fallback:
        best = fb
    return best


def _dir_has_exe(d: Path, rules: RuleSet, allow_excluded: bool = True) -> bool:
    """目录顶层是否含有 exe。"""
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    if not e.name.lower().endswith(".exe"):
                        continue
                    if allow_excluded or not rules.is_excluded_exe(e.name):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def guess_game_name(install_dir: Path, exe: Optional[Path], rules: RuleSet,
                    fallback: str = "", stop_at: Optional[Path] = None) -> str:
    """推断游戏名。

    依次尝试：
      1. 安装目录名（清洗后）
      2. 如果目录名是 bin / game / x64 / GameData 这类通用名，
         就沿父目录往上找（最多 4 层，且不越过游戏库根目录）
      3. exe 文件名
      4. fallback

    这样 ``D:\\Galgame\\CLANNAD\\bin\\game.exe`` 不会再被命名成 "bin"。
    """
    stop_key = path_key(stop_at) if stop_at is not None else None
    cur: Optional[Path] = install_dir
    for _ in range(5):
        if cur is None:
            break
        if stop_key is not None and path_key(cur) == stop_key:
            break
        raw = cur.name
        if raw and not rules.is_generic_dir(raw):
            cleaned = rules.strip_game_name(raw)
            if cleaned and len(cleaned) >= 2:
                return cleaned
        parent = cur.parent
        if parent == cur:      # 已经到盘符根，防止死循环
            break
        cur = parent

    if exe is not None:
        cleaned = rules.strip_game_name(exe.stem)
        if cleaned and len(cleaned) >= 2:
            return cleaned
    return fallback or (install_dir.name if install_dir else "") or "未命名游戏"


def iter_candidate_game_dirs(
    root: Path,
    rules: RuleSet,
    max_depth: int = 5,
    max_dirs: int = 20000,
    cancel: Optional[Any] = None,
    stats: Optional[Dict[str, int]] = None,
) -> Iterator[Path]:
    """有限深度遍历游戏库根目录，产出“顶层含 exe 的目录”。

    与旧版的区别（都是为了尽量不漏掉自下资源）：

    * **不再“一见到 exe 就停止下沉”**：如果某个目录含 exe，但它下面还有
      各自直接含 exe 的子目录（典型的“合集 / 系列 / 特典”结构），
      那些子目录同样会被产出。
    * **游戏库根目录本身含 exe 时也会被产出**，
      这样把“游戏库根目录”直接指到某一个游戏文件夹也能识别。
    * 每个目录只往下看一层来判断“子目录里有没有 exe”，开销很小。

    ``stats`` 用于回填诊断信息（``visited`` / ``exe_dirs``）。
    """
    root = normalize(root)
    if not root.is_dir() or is_protected(root):
        return
    stack: List[Tuple[Path, int]] = [(root, 0)]
    visited = 0
    if stats is not None:
        stats.setdefault("visited", 0)
        stats.setdefault("exe_dirs", 0)
    while stack:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            return
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        visited += 1
        if stats is not None:
            stats["visited"] = visited
        if visited > max_dirs:
            log.warning("达到目录数量上限 %d，提前结束遍历：%s", max_dirs, root)
            return

        has_exe = False
        subdirs: List[Path] = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if rules.is_ignored_dir(e.name):
                        continue
                    d = normalize(e.path)
                    if is_protected(d):
                        # ★ 绝不进入程序自己的数据根 / 备份根
                        continue
                    subdirs.append(d)
                elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(".exe"):
                    # 这里刻意不套用 install_exe_exclude：
                    # 只要目录里有 exe（哪怕是 launcher.exe），它就可能是游戏目录。
                    has_exe = True
            except OSError:
                continue

        if has_exe:
            # 「免DVD补丁备份」「汉化补丁」这类目录里经常也有 exe，
            # 但它们不是游戏，不能当成候选（仍然会往下找，免得漏掉里面的真游戏）。
            if not rules.is_excluded_dir_name(cur.name) and not is_protected(cur):
                if stats is not None:
                    stats["exe_dirs"] = stats.get("exe_dirs", 0) + 1
                yield cur
            # 继续下沉到“自己直接含 exe”的子目录，兼容合集/系列/特典目录
            if depth < max_depth:
                for d in subdirs:
                    if rules.is_excluded_dir_name(d.name):
                        continue
                    if _dir_has_exe(d, rules, allow_excluded=True):
                        stack.append((d, depth + 1))
            continue

        if depth < max_depth:
            for d in subdirs:
                stack.append((d, depth + 1))



def read_app_info(install_dir: Path) -> Tuple[str, str]:
    """读 Unity 的 ``<产品名>_Data/app.info``，返回 ``(公司名, 产品名)``。

    这是 Unity 的标准元数据文件（纯文本，前两行依次是 companyName / productName）。
    有了它就能**确定性地**拼出 ``%USERPROFILE%\\AppData\\LocalLow\\<公司>\\<产品>``，
    不必再去枚举整个 ``%LOCALAPPDATA%``（那正是 v1 误检灾难的根源）。
    读不到就返回两个空串，不会影响其它检测途径。
    """
    install_dir = normalize(install_dir)
    if not install_dir.is_dir():
        return "", ""
    try:
        entries = list(os.scandir(install_dir))
    except OSError:
        return "", ""
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
            if not e.name.lower().endswith("_data"):
                continue
            info = Path(e.path) / "app.info"
            if not info.is_file():
                continue
            lines = [
                x.strip() for x in
                info.read_text(encoding="utf-8", errors="ignore").splitlines()
                if x.strip()
            ]
            if len(lines) >= 2:
                return lines[0], lines[1]
            if lines:
                return "", lines[0]
        except OSError:
            continue
    return "", ""


def build_save_path_gate(rules: RuleSet) -> Optional[SavePathGate]:
    """按当前规则与运行时的数据根/备份根构造准入闸门。"""
    try:
        data_root = GUARD.data_root if GUARD.unlocked else None
        backup_root = GUARD.backup_root if GUARD.unlocked else None
    except Exception:  # noqa: BLE001
        data_root = backup_root = None
    try:
        return SavePathGate(rules, backup_root=backup_root, data_root=data_root)
    except Exception:  # noqa: BLE001
        log.debug("构造准入闸门失败", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 存档路径推断
# ---------------------------------------------------------------------------

#: 内置的存档特征文件（规则文件里可以用 save_file_globs 覆盖/扩充）
_SAVE_FILE_PATTERNS = [
    "save*.dat", "save*.sav", "*.sav", "*.savdata", "savedata*", "global.sav",
    "*save*.dat", "*.rpgsave", "save*.bin", "config*.dat", "syssave*",
]


def looks_like_save_dir(d: Path, rules: Optional[RuleSet] = None) -> bool:
    """目录里是否直接含有**足够强的**存档证据。

    规则：至少 1 个强特征（save*.sav / *.rpgsave / savedata* …），
    或者 2 个以上弱特征（config*.dat / system*.dat …）。
    后者是为了排除游戏自己的数据目录 —— 它们常有 config.dat，但那不是存档。
    """
    strong, weak, _newest = save_stats(d, rules)
    return strong >= 1 or weak >= 2


def save_stats(d: Path, rules: Optional[RuleSet] = None) -> Tuple[int, int, float]:
    """统计目录里的强/弱存档特征文件数量，以及最新修改时间。

    返回 ``(强特征数, 弱特征数, 最新 mtime)``。
    """
    strong = 0
    weak = 0
    newest = 0.0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    name = e.name
                    if rules is not None:
                        is_strong = rules.looks_like_save_file(name)
                        is_weak = rules.looks_like_weak_save_file(name)
                    else:
                        low = name.lower()
                        is_strong = any(fnmatch.fnmatch(low, p) for p in _SAVE_FILE_PATTERNS)
                        is_weak = False
                    if not (is_strong or is_weak):
                        continue
                    if is_strong:
                        strong += 1
                    else:
                        weak += 1
                    st = e.stat()
                    if st.st_mtime > newest:
                        newest = float(st.st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return strong, weak, newest


def _is_within_path(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之下（不含相等）。"""
    try:
        c = path_key(child)
        p = path_key(parent)
    except OSError:
        return False
    if c == p:
        return False
    if not p.endswith(os.sep):
        p += os.sep
    return c.startswith(p)



#: 归一化游戏/目录名时要去掉的“分隔符” —— 注意必须包含中日文标点，
#: 否则 "樱花，萌放" 和 "樱花萌放" 会被当成两个不同的名字（真踩过这个坑）。
_NAME_SEP_RE = re.compile(
    r"[\s\-_\.·,，、。;；:：!！?？…—～~《》「」『』\[\]【】()（）{}<>\"'`|/\\+]+"
)


def _norm_name(s: str) -> str:
    """把名字压成可比较的形式：小写、去掉标点与空白（中英日标点都算）。"""
    return _NAME_SEP_RE.sub("", s or "").lower()


def _has_cjk(s: str) -> bool:
    return any(("\u4e00" <= ch <= "\u9fff") or ("\u3040" <= ch <= "\u30ff") for ch in (s or ""))


def _tokens(s: str) -> Set[str]:
    """把名字切成“词”（按非字母数字/非中日文切开）。"""
    return {t for t in re.split(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff]+", (s or "").lower()) if t}


def name_matches(key: str, key_tokens: Set[str], cand_name: str) -> bool:
    """判断候选目录名是否“像”目标游戏名。

    ``key`` 传原始名字即可（内部会归一化）。

    ★ 这条规则是精度的关键。旧版用无脑子串匹配，结果安装目录叫 ``PC`` 时，
    ``%LOCALAPPDATA%`` 下的 ``TpcdMetadata`` / ``XiaomiPCManager`` /
    ``Clipchamp`` / ``LockApp`` 全都因为含有 "pc" 而被当成这个游戏的存档目录。

    现在的规则：

    * 归一化后**完全同名** → 直接算命中（再短也认，这是精确匹配）
    * 含中日文 → 长度 ≥ 2 且互为子串（中文子串本身有信息量）
    * 纯 ASCII → 长度必须 ≥ 4，且必须是**词级别**匹配
      （"dingtalk" 命中 "DingTalk_133"，但 "pc" 不会命中 "Clipchamp"）
    """
    key = _norm_name(key)
    if not key_tokens:
        key_tokens = _tokens(key)
    cand_key = _norm_name(cand_name)
    if not key or not cand_key:
        return False
    if key == cand_key:
        return True
    if _has_cjk(key):
        if len(key) < 2:
            return False
        return key in cand_key or cand_key in key
    if len(key) < 4:
        return False
    cand_tokens = _tokens(cand_name)
    if key in cand_tokens or (key_tokens & cand_tokens):
        return True
    return cand_key in key or key in cand_key


def _iter_matching_named_dirs(roots: Sequence[Path], target: str,
                              max_depth: int = 2, limit: int = 4000) -> Iterator[Path]:
    """在给定根目录下最多两层里找“名字像目标游戏”的目录。"""
    key = _norm_name(target)
    if not key or len(key) < 2:
        return
    key_tokens = _tokens(target)
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        stack: List[Tuple[Path, int]] = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        seen += 1
                        if seen > limit:
                            return
                        if not e.is_dir(follow_symlinks=False):
                            continue
                        if name_matches(key, key_tokens, e.name):
                            yield normalize(e.path)
                            continue
                        if depth + 1 < max_depth:
                            stack.append((normalize(e.path), depth + 1))
            except OSError:
                continue


def find_dirs_with_save_files(
    root: Path,
    rules: RuleSet,
    max_depth: int = 3,
    limit: int = 4000,
    recent_days: int = 0,
    cancel: Optional[Any] = None,
    need_weak: int = 2,
) -> List[Tuple[Path, str, float, int, int]]:
    """在 ``root`` 子树里找出“**直接**含有存档特征文件”的目录。

    返回 ``[(目录, 命中样例文件, 最新 mtime, 强特征数, 弱特征数), ...]``，
    按 mtime 降序。

    判定门槛：**强特征 ≥ 1 或 弱特征 ≥ ``need_weak``**。
    这样 ``data\\config.dat`` 这种只有 1 个弱特征的引擎资源目录不会被误判，
    而真正的存档目录（``save01.sav`` 之类）照样能命中。

    ``recent_days > 0`` 时只保留最近这么多天内有改动的目录。
    """
    root = normalize(root)
    out: List[Tuple[Path, str, float, int, int]] = []
    if not root.is_dir() or is_protected(root):
        return out
    cutoff = (time.time() - recent_days * 86400.0) if recent_days > 0 else 0.0
    stack: List[Tuple[Path, int]] = [(root, 0)]
    seen = 0
    while stack:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            return out
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        subdirs: List[Path] = []
        sample = ""
        strong = 0
        weak = 0
        newest = 0.0
        for e in entries:
            seen += 1
            if seen > limit:
                return out
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth and not rules.is_ignored_dir(e.name):
                        d = normalize(e.path)
                        if not is_protected(d):
                            subdirs.append(d)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                name = e.name
                is_strong = rules.looks_like_save_file(name)
                is_weak = (not is_strong) and rules.looks_like_weak_save_file(name)
                if not (is_strong or is_weak):
                    continue
                if not sample:
                    sample = name
                if is_strong:
                    strong += 1
                else:
                    weak += 1
                st = e.stat()
                if st.st_mtime > newest:
                    newest = float(st.st_mtime)
            except OSError:
                continue

        for d in subdirs:
            stack.append((d, depth + 1))

        if strong >= 1 or weak >= max(1, int(need_weak)):
            if cutoff and newest and newest < cutoff:
                continue
            if is_protected(cur):
                continue
            out.append((normalize(cur), sample, newest, strong, weak))
    out.sort(key=lambda t: -t[2])
    return out


#: 从游戏自己的配置文件里找路径时，允许读取的扩展名与大小
_CONFIG_EXTS = (
    ".ini", ".cfg", ".conf", ".config", ".txt", ".json", ".xml", ".dat",
    ".bin", ".properties", ".settings", ".opt", ".set",
)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]{1,80}\\)*[^\\/:*?\"<>|\r\n]{0,80}")
_ENV_PATH_RE = re.compile(r"%(?:APPDATA|LOCALAPPDATA|USERPROFILE|PROGRAMDATA)%\\[^\"'<>\r\n|*?]{1,200}",
                          re.IGNORECASE)


def find_paths_in_game_configs(
    install_dir: Path,
    rules: RuleSet,
    max_files: int = 60,
    max_size: int = 512 * 1024,
    limit: int = 8,
) -> List[Tuple[str, str]]:
    """在游戏自己的配置文件里找它写下的存档路径。

    很多引擎会把“存档目录”明文写在安装目录下的 ini / cfg / dat 里 ——
    **这是精度最高的一类线索，因为是游戏自己说的**，比我们猜名字可靠得多。

    只读、限制文件大小、限制扩展名；找到的路径还必须真实存在，
    并且里面确实有存档特征文件，才会被采用。
    """
    out: List[Tuple[str, str]] = []
    install_dir = normalize(install_dir)
    if not install_dir.is_dir() or is_protected(install_dir):
        return out

    candidates: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(install_dir):
            rel = os.path.relpath(dirpath, install_dir)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= 2:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if not rules.is_ignored_dir(d)]
            for fn in filenames:
                if fn.lower().endswith(_CONFIG_EXTS):
                    candidates.append(Path(dirpath) / fn)
            if len(candidates) > max_files * 4:
                break
    except OSError:
        return out

    # 小文件优先（配置一般都很小，大 .dat 是资源包）
    def _size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 1 << 60

    candidates.sort(key=_size)

    checked = 0
    seen_paths: Set[str] = set()
    for path in candidates:
        if checked >= max_files or len(out) >= limit:
            break
        try:
            if path.stat().st_size > max_size:
                continue
        except OSError:
            continue
        checked += 1
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        text = ""
        for enc in ("utf-8-sig", "utf-16", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not text:
            continue

        found = _WIN_PATH_RE.findall(text) + _ENV_PATH_RE.findall(text)
        for item in found[:40]:
            try:
                expanded = os.path.expandvars(item.strip().strip('"').strip())
                p = normalize(expanded)
            except OSError:
                continue
            # 配置里往往写的是文件路径，取它所在目录
            if p.is_file():
                p = p.parent
            if not p.is_dir():
                continue
            if is_protected(p):
                continue
            # 配置里常会写一堆无关路径（下载目录、临时目录…），先按名字排掉
            if rules.is_excluded_save_path_name(p.name):
                continue
            # 必须真的像存档目录，否则就是把“游戏安装路径”之类的配置项捡进来了
            strong, weak, _n = save_stats(p, rules)
            if strong < 1 and weak < 2:
                continue
            k = path_key(p)
            if k in seen_paths:
                continue
            seen_paths.add(k)
            out.append((str(p), f"游戏配置文件 {path.name} 里写明的路径"))
            if len(out) >= limit:
                break
    return out


def sniff_save_dirs(
    install_dir: Path,
    game_name: str,
    rules: RuleSet,
    engine: str = "",
    max_depth: int = 3,
    recent_days: int = 0,
    user_roots: Sequence[Path] = (),
    cancel: Optional[Any] = None,
    sources: Optional[DetectSources] = None,
) -> List[Tuple[str, str, int]]:
    """主动“嗅探”存档目录，返回 ``[(路径, 判定依据, 分数), ...]``（按分数降序）。

    这是专门给**自下资源**（汉化版 / 绿色版 / 合集）准备的兜底手段：
    不依赖引擎识别，直接去找“哪里有一堆长得像存档的文件”。

    搜索范围（全部只读）：

    1. 安装目录自身的子树（默认 3 层）
    2. 安装目录的**兄弟目录**（有些版本会把存档放在安装目录旁边）
    3. 用户级位置（文档 / 已保存的游戏 / AppData / LocalLow）里名字匹配的目录
    4. 用户级位置里最近改过且含存档文件的目录（只有 ``recent_days > 0`` 才做）

    所有候选都会被“补丁 / 备份 / 破解”目录名过滤掉。
    """
    src = sources or DetectSources()
    scored: Dict[str, Tuple[int, str, str]] = {}   # path_key -> (score, path, reason)
    gate = build_save_path_gate(rules)

    def _add(p: Path, score: int, reason: str) -> None:
        if is_protected(p):
            log.debug("跳过程序自己的数据/备份目录：%s", p)
            return
        if gate is not None:
            _verdict, _why = gate.check(p, quick=True)
            if _verdict != Decision.OK:
                log.debug("闸门拒绝候选 %s：%s", p, _why)
                return
        if rules.is_excluded_save_path_name(p.name):
            log.debug("跳过疑似补丁/备份目录：%s", p)
            return
        try:
            s = str(normalize(p))
        except OSError:
            return
        k = path_key(s)
        old = scored.get(k)
        if old is None or score > old[0]:
            scored[k] = (score, s, reason)

    save_names = set(rules.save_dir_names)
    name_key = _norm_name(game_name)

    # 1) 安装目录子树
    for d, hit, _m, strong, weak in (
        find_dirs_with_save_files(install_dir, rules, max_depth=max_depth,
                                  limit=4000, cancel=cancel)
        if src.sniff_install else ()
    ):
        low = d.name.lower()
        evidence = f"{strong} 个存档文件" + (f"、{weak} 个配置类文件" if weak else "")
        if path_key(d) == path_key(install_dir) and low not in save_names:
            # 安装目录本身就是存档目录（NScripter / ONScripter 这类）
            _add(d, 60, f"安装目录内直接存在存档文件（如 {hit}）")
        elif low in save_names:
            _add(d, 80, f"安装目录下的 {d.name}\\ 内含存档文件（如 {hit}）")
        else:
            _add(d, 52, f"安装目录子树里的 {d.name}\\ 内含{evidence}（如 {hit}）")

    # 2) 兄弟目录
    try:
        if not src.sniff_siblings:
            raise StopIteration
        parent = install_dir.parent
        if parent.is_dir() and path_key(parent) != path_key(install_dir):
            key = _norm_name(game_name)
            key_tokens = _tokens(game_name)
            with os.scandir(parent) as it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    nm_low = e.name.lower()
                    # 兄弟目录要么名字就是存档目录名，要么带上了游戏名的一部分
                    name_hit = bool(
                        key and len(key) >= 2
                        and name_matches(key, key_tokens, e.name)
                        and _norm_name(e.name) != key      # 同名的那个就是游戏本体，跳过
                    )
                    if nm_low in save_names or name_hit:
                        p = normalize(e.path)
                        if looks_like_save_dir(p, rules):
                            _add(p, 58, f"安装目录旁边的 {e.name}\\ 内含存档文件")
    except (OSError, StopIteration):
        pass

    # 3) 用户级位置：名字匹配
    roots = list(user_roots) if src.user_locations else []
    if name_key and len(name_key) >= 2:
        for p in _iter_matching_named_dirs(roots, game_name, max_depth=2):
            if looks_like_save_dir(p, rules):
                _add(p, 72, f"用户目录下名字匹配「{game_name}」且含存档文件")
            else:
                _add(p, 38, f"用户目录下名字匹配「{game_name}」")

    # 4) 用户级位置：最近改过 + 含存档文件（需显式开启，避免误报其它游戏的存档）
    if recent_days > 0 and src.user_locations:
        for r in roots:
            for d, hit, _m, _s, _w in find_dirs_with_save_files(
                r, rules, max_depth=2, limit=3000, recent_days=recent_days, cancel=cancel
            ):
                _add(d, 30, f"最近 {recent_days} 天内有改动且含存档文件（如 {hit}）")

    return [(p, reason, score)
            for _k, (score, p, reason) in sorted(scored.items(), key=lambda kv: -kv[1][0])]


def infer_save_paths(
    install_dir: Path,
    engine: str,
    game_name: str,
    rules: RuleSet,
    extra_save_dirs: Sequence[str] = (),
    steam_save_dirs: Sequence[Path] = (),
    scan_user_locations: bool = True,
    sniff_max_depth: int = 3,
    recent_days: int = 0,
    max_paths: int = 5,
    sources: Optional[DetectSources] = None,
) -> Tuple[List[str], List[str]]:
    """推断存档路径（高精度版）。

    流程：**收集候选 → 逐个打分 → 排除补丁/备份 → 父子去重 → 取前 N 个**。

    和旧版最大的区别（旧版是“匹配到就全塞进去”，所以会一口气给出十几个路径）：

    * 每个候选带分数，只有真正像存档的才排前面；
    * 名字像“补丁 / 备份 / 破解 / 缓存”的目录**直接排除**；
    * **父子折叠**：`save\\` 和 `save\\backup\\` 同时命中时只保留 `save\\`；
    * **数量上限** ``max_paths``（默认 5），其余候选写进判定依据里备查；
    * 额外增加一条高精度来源：**游戏自己的配置文件里写明的路径**。

    返回 ``(存档路径列表, 判定依据列表)``。
    """
    src = sources or DetectSources()
    sig = rules.signature_for(engine) or {}
    scored: Dict[str, Tuple[int, str, str]] = {}     # path_key -> (score, path, reason)
    dropped: List[str] = []
    gate = build_save_path_gate(rules)

    # Unity / CatSystem2 这类引擎用 ${app_company} / ${app_product} 占位符
    # 表达“厂商 / 产品”这一层，值从游戏自己的 app.info 里读出来。
    _app_company, _app_product = read_app_info(install_dir)

    def _expand_anchor(s: str) -> str:
        return (str(s)
                .replace("${app_company}", _app_company)
                .replace("${app_product}", _app_product))

    def _add(p: Path, score: int, why: str) -> None:
        try:
            if not p.is_dir():
                return
            if is_protected(p):
                dropped.append(f"{p}（在程序自己的数据/备份目录里，已排除）")
                return
            # ★ 准入闸门：整条路径（含每一级祖先）过一遍硬规则
            if gate is not None:
                _verdict, _why = gate.check(p, quick=True)
                if _verdict != Decision.OK:
                    dropped.append(f"{p}（闸门拒绝：{_why}）")
                    return
            if rules.is_excluded_save_path_name(p.name):
                dropped.append(f"{p}（疑似补丁/备份目录，已排除）")
                return
            s = str(normalize(p))
        except OSError:
            return
        k = path_key(s)
        old = scored.get(k)
        if old is None:
            scored[k] = (score, s, why)
        elif score > old[0]:
            scored[k] = (score, s, why)
            dropped.append(f"{s}（重复来源，已合并）")

    # ---------------------------------------------------------------- 收集
    # 1) 安装目录内的常见存档子目录（引擎签名 + 通用存档目录名）
    sub_names: List[str] = list(sig.get("save_subdirs", []) or []) + list(rules.save_dir_names)
    if src.install_subdirs:
        for sub in sub_names:
            if sub == ".":
                continue
            cand = install_dir / Path(sub.replace("/", os.sep))
            if not cand.is_dir():
                continue
            low = cand.name.lower()
            if low in rules.save_dir_names:
                _add(cand, 85, f"安装目录下的 {sub}/（常见存档目录名）")
            elif looks_like_save_dir(cand, rules):
                _add(cand, 70, f"安装目录下的 {sub}/")

    # 2) 安装目录本身就是存档目录（NScripter / ONScripter 这类）
    if src.install_root and looks_like_save_dir(install_dir, rules):
        _add(install_dir, 58, "安装目录内直接存在存档文件（这类引擎把存档放在游戏根目录）")

    # 3) Steam 云存档（几乎不会错）
    if src.steam_cloud:
        for p in steam_save_dirs:
            _add(p, 95, "Steam userdata 云存档目录")

    # 4) 游戏自己的配置文件里写明的路径（精度最高的一类线索）
    if src.config_files:
        try:
            for p, why in find_paths_in_game_configs(install_dir, rules):
                _add(Path(p), 88, why)
        except Exception:  # noqa: BLE001
            log.debug("读取游戏配置文件失败", exc_info=True)

    # 5) 主动嗅探（自下资源的兜底）
    sniffed: List[Tuple[str, str, int]] = []
    if src.sniff_install or src.sniff_siblings or src.user_locations:
        try:
            sniffed = sniff_save_dirs(
                install_dir,
                game_name,
                rules,
                engine=engine,
                max_depth=max(1, int(sniff_max_depth)),
                recent_days=max(0, int(recent_days)),
                user_roots=(rules.user_save_roots()
                            if (scan_user_locations and src.user_locations) else []),
                sources=src,
            )
        except Exception:  # noqa: BLE001 - 嗅探失败不能影响主流程
            log.debug("存档嗅探失败", exc_info=True)
    for p, why, score in sniffed[:10]:
        _add(Path(p), score, why)

    # 6) 用户级位置（只读扫描）
    if scan_user_locations and src.user_locations:
        user_roots = rules.user_save_roots()
        # ★ 只用**有信息量**的名字去匹配：
        #   安装目录如果叫 "PC" / "bin" / "GameData" 这种通用名，
        #   拿它去 %LOCALAPPDATA% 里子串匹配会命中一大堆无关目录。
        targets = {
            t for t in (game_name, rules.strip_game_name(game_name), install_dir.name)
            if t and not rules.is_generic_dir(t)
        }
        looked: Set[str] = set()
        for t in sorted(targets, key=len, reverse=True):
            if not t or t in looked:
                continue
            looked.add(t)
            for p in _iter_matching_named_dirs(user_roots, t):
                if rules.is_excluded_save_path_name(p.name):
                    continue
                # 名字匹配上的目录，必须真的像存档目录才收；
                # 否则 AppData 下会混进一堆同名/近名的软件目录。
                exact = _norm_name(p.name) == _norm_name(t)
                if looks_like_save_dir(p, rules):
                    _add(p, 62, f"用户目录下名字匹配「{t}」且含存档文件")
                elif exact and len(_norm_name(t)) >= 4:
                    _add(p, 30, f"用户目录下与「{t}」完全同名（未发现存档文件）")

        # 引擎专属位置：Ren'Py / Unity / RPG Maker 等
        appdata = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        local_low = os.environ.get("USERPROFILE")
        # Unity：用 app.info 里的 公司/产品 确定性拼接 LocalLow 路径
        for sub in sig.get("locallow_subdirs", []) or []:
            expanded = _expand_anchor(sub)
            if not expanded or "${" in expanded or not local_low:
                continue
            base = normalize(Path(local_low) / "AppData" / "LocalLow")
            if base.is_dir():
                cand = base / Path(expanded.replace("/", os.sep))
                if cand.is_dir():
                    _add(cand, 86, f"LocalLow\\{expanded}（由 app.info 确定性推导）")

        # RPG Maker 等：只做 %LOCALAPPDATA%\<游戏名> 的**精确同名**匹配，
        # 绝不枚举整个 %LOCALAPPDATA%（那正是 v1 的误检灾难）
        if sig.get("localappdata_exact") and local:
            for _t in (game_name, install_dir.name):
                if not _t or rules.is_generic_dir(_t):
                    continue
                cand = normalize(Path(local) / _t)
                if cand.is_dir():
                    _add(cand, 72, f"%LOCALAPPDATA%\\{_t}（精确同名目录）")

        for sub in sig.get("appdata_subdirs", []) or []:
            if appdata:
                expanded = _expand_anchor(sub)
                if not expanded or "${" in expanded:
                    continue
                base = normalize(Path(appdata) / expanded)
                if base.is_dir():
                    for p in _iter_matching_named_dirs([base], game_name, max_depth=1):
                        _add(p, 78, f"%APPDATA%\\{expanded} 下的同名目录")
        for sub in sig.get("localappdata_subdirs", []) or []:
            # ★ “.”／空 等于把整个 %LOCALAPPDATA% 当存档根 —— 直接忽略。
            #   v1 的规则里 Unity / RPG Maker 就是这么写的，是误检灾难的源头。
            if str(sub).strip() in (".", "", "/", "\\"):
                log.warning("规则里 localappdata_subdirs 出现 %r（过宽），已忽略", sub)
                continue
            if local:
                base = normalize(Path(local) / sub)
                if base.is_dir():
                    for p in _iter_matching_named_dirs([base], game_name, max_depth=2):
                        _add(p, 68, "%LOCALAPPDATA% 下的同名目录")
        for sub in sig.get("documents_subdirs", []) or []:
            up = os.environ.get("USERPROFILE")
            if up:
                base = normalize(Path(up) / "Documents")
                if base.is_dir():
                    for p in _iter_matching_named_dirs([base], game_name, max_depth=2):
                        _add(p, 60, "文档目录下的同名存档目录")

    # 7) 用户自定义的额外存档目录
    for raw in extra_save_dirs or ():
        if not raw:
            continue
        p = normalize(raw)
        if not p.is_dir():
            continue
        if path_key(install_dir) == path_key(p) or _norm_name(install_dir.name) == _norm_name(p.name):
            _add(p, 66, "用户在设置中指定的额外存档目录")
        elif _norm_name(game_name) and _norm_name(game_name) in _norm_name(p.name):
            _add(p, 55, "用户在设置中指定的额外存档目录（名字匹配）")

    # ---------------------------------------------------------------- 打分
    now = time.time()
    items: List[Tuple[int, str, str]] = []
    for _k, (score, path, why) in scored.items():
        p = Path(path)
        strong, weak, newest = save_stats(p, rules)
        notes: List[str] = []
        if strong:
            score += 14
            notes.append(f"{strong} 个存档文件")
        if weak:
            notes.append(f"{weak} 个配置类文件")
        if strong + weak >= 3:
            score += 8
        # 最近改动过 = 你真的在玩它 → 强正信号
        if newest:
            age_days = (now - newest) / 86400.0
            if age_days <= 30:
                score += 26
                notes.append("最近 30 天内改动过")
            elif age_days <= 180:
                score += 14
                notes.append("半年内改动过")
            elif age_days > 730:
                score -= 18
                notes.append("两年多没动过")
        # 越靠近安装目录越可信
        try:
            rel = p.relative_to(install_dir)
            depth = len(rel.parts)
            if depth == 1:
                score += 12
            elif depth == 2:
                score += 4
            else:
                score -= 4 * (depth - 2)
        except ValueError:
            pass
        final_why = why + (f"（{'；'.join(notes)}）" if notes else "")
        items.append((score, path, final_why))

    items.sort(key=lambda t: (-t[0], t[1]))

    # ------------------------------------------------- 父子折叠 + 数量上限
    inst_key = path_key(install_dir)
    kept: List[Tuple[int, str, str]] = []
    for score, path, why in items:
        p = Path(path)
        if any(_is_within_path(p, Path(kp)) for _s, kp, _w in kept):
            dropped.append(f"{path}（是 {Path(next(kp for _s, kp, _w in kept if _is_within_path(p, Path(kp))))} 的子目录，已折叠）")
            continue
        # 之前收过安装目录本身，现在来了它里面的具体子目录 → 换掉安装目录
        if path_key(p) != inst_key:
            before = len(kept)
            kept = [k for k in kept if not _is_within_path(Path(k[1]), p) or path_key(k[1]) != inst_key]
            if len(kept) != before:
                dropped.append(f"{install_dir}（下面已有更具体的存档目录，改为只备份该子目录）")
        kept.append((score, path, why))

    limit = max(1, int(max_paths))
    final = kept[:limit]
    for _s, path, _w in kept[limit:]:
        dropped.append(f"{path}（分数较低，未自动选中；可用「指定存档目录…」手动加）")

    found = [p for _s, p, _w in final]
    evidence = [w for _s, _p, w in final]
    if dropped:
        evidence.append("—— 以下候选被舍弃 ——")
        evidence.extend(dropped[:12])
    return found, evidence


# ---------------------------------------------------------------------------
# 组合：把 Steam 应用 / 库目录转成候选
# ---------------------------------------------------------------------------

def candidates_from_steam(info: SteamInfo, rules: RuleSet,
                          scan_user_locations: bool = True,
                          extra_save_dirs: Sequence[str] = (),
                          sniff_max_depth: int = 3,
                          recent_days: int = 0,
                          max_save_paths: int = 5,
                          sources: Optional[DetectSources] = None) -> List[Candidate]:
    out: List[Candidate] = []
    for app in info.apps:
        if not app.get("exists"):
            continue
        install_dir = normalize(app["install_path"])
        exe = find_game_exe(install_dir, rules)
        hits = rules.detect_engines(install_dir)
        engine = hits[0].engine if hits else ""
        name = rules.strip_game_name(app.get("name") or install_dir.name)
        saves, evidence = infer_save_paths(
            install_dir, engine, name, rules,
            extra_save_dirs=extra_save_dirs,
            steam_save_dirs=_steam_userdata_save_dirs(info, str(app.get("appid", "")), rules),
            scan_user_locations=scan_user_locations,
            sniff_max_depth=sniff_max_depth,
            recent_days=recent_days,
            max_paths=max_save_paths,
            sources=sources,
        )
        cand = Candidate(
            name=name,
            install_dir=str(install_dir),
            exe=str(exe) if exe else "",
            engine=engine,
            engine_confidence=rules.confidence_label(hits[0] if hits else None),
            save_paths=saves,
            save_evidence=evidence,
            reasons=[f"Steam AppID {app.get('appid')}"] + (hits[0].reasons if hits else []),
            score=5 + (hits[0].weight if hits else 0) + (2 if saves else 0),
            source="steam",
        )
        out.append(cand)
    return out


def candidates_from_library_root(
    root: Path,
    rules: RuleSet,
    max_depth: int = 5,
    max_dirs: int = 20000,
    extra_save_dirs: Sequence[str] = (),
    scan_user_locations: bool = True,
    sniff_max_depth: int = 3,
    recent_days: int = 0,
    max_save_paths: int = 5,
    sources: Optional[DetectSources] = None,
    cancel: Optional[Any] = None,
    progress: Optional[Any] = None,
    stats: Optional[Dict[str, int]] = None,
) -> Tuple[List[Candidate], int]:
    """扫描一个游戏库根目录，返回 ``(候选列表, 已遍历目录数)``。"""
    out: List[Candidate] = []
    scanned = 0
    for game_dir in iter_candidate_game_dirs(root, rules, max_depth, max_dirs, cancel, stats):
        scanned += 1
        if progress is not None:
            progress(str(game_dir), scanned)
        try:
            exe = find_game_exe(game_dir, rules)
            hits = rules.detect_engines(game_dir)
            engine = hits[0].engine if hits else ""
            # stop_at=root：游戏名最多往上找到游戏库根目录为止，
            # 免得把库根目录的名字（比如 "Galgame"）当成游戏名。
            name = guess_game_name(game_dir, exe, rules, stop_at=root)
            saves, evidence = infer_save_paths(
                game_dir, engine, name, rules,
                extra_save_dirs=extra_save_dirs,
                scan_user_locations=scan_user_locations,
                sniff_max_depth=sniff_max_depth,
                recent_days=recent_days,
                max_paths=max_save_paths,
                sources=sources,
            )
            score = (2 if exe else 0) + (hits[0].weight if hits else 0) + (2 if saves else 0)
            out.append(Candidate(
                name=name,
                install_dir=str(game_dir),
                exe=str(exe) if exe else "",
                engine=engine,
                engine_confidence=rules.confidence_label(hits[0] if hits else None),
                save_paths=saves,
                save_evidence=evidence,
                reasons=(hits[0].reasons if hits else ["该目录顶层含 exe"]),
                score=score,
                source="library",
            ))
        except OSError as exc:
            log.debug("扫描 %s 失败：%s", game_dir, exc)
    return out, scanned


# ---------------------------------------------------------------------------
# 系统痕迹（注册表卸载项 / 快捷方式 / 最近使用）→ 候选
# ---------------------------------------------------------------------------

def candidates_from_system_games(
    system_games: Sequence[Any],
    rules: RuleSet,
    extra_save_dirs: Sequence[str] = (),
    scan_user_locations: bool = True,
    sniff_max_depth: int = 3,
    recent_days: int = 0,
    max_save_paths: int = 5,
    sources: Optional[DetectSources] = None,
    cancel: Optional[Any] = None,
) -> List[Candidate]:
    """把 :mod:`galguard.systemscan` 发现的结果转成候选游戏。"""
    out: List[Candidate] = []
    for sg in system_games:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            break
        try:
            install_dir = normalize(sg.install_dir)
            if not install_dir.is_dir():
                continue
            exe: Optional[Path] = None
            if sg.exe:
                try:
                    cand_exe = normalize(sg.exe)
                    if cand_exe.is_file():
                        exe = cand_exe
                except OSError:
                    exe = None
            if exe is None:
                exe = find_game_exe(install_dir, rules)

            hits = rules.detect_engines(install_dir)
            engine = hits[0].engine if hits else ""
            name = rules.strip_game_name(getattr(sg, "name", "") or "") or install_dir.name
            saves, evidence = infer_save_paths(
                install_dir, engine, name, rules,
                extra_save_dirs=extra_save_dirs,
                scan_user_locations=scan_user_locations,
                sniff_max_depth=sniff_max_depth,
                recent_days=recent_days,
                max_paths=max_save_paths,
                    sources=sources,
            )
            reasons = [f"系统发现：{getattr(sg, 'detail', '')}"]
            if hits:
                reasons += list(hits[0].reasons)
            out.append(Candidate(
                name=name,
                install_dir=str(install_dir),
                exe=str(exe) if exe else "",
                engine=engine,
                engine_confidence=rules.confidence_label(hits[0] if hits else None),
                save_paths=saves,
                save_evidence=evidence,
                reasons=reasons,
                # 系统发现的评分略低于“游戏库”来源，列表排序时靠后一些
                score=3 + (hits[0].weight if hits else 0) + (2 if saves else 0),
                source=getattr(sg, "source", "system") or "system",
                # 系统痕迹本来就是个“线索”，默认只勾选那些**已经找到存档路径**的；
                # 其余留给用户自己看，免得一股脑塞进列表。
                selected=bool(saves),
            ))
        except OSError as exc:
            log.debug("处理系统候选失败：%s", exc)
    return out


def apply_manifest_to_candidates(
    candidates: Sequence[Candidate],
    manifest: Any,
    extra_roots: Sequence[str] = (),
) -> int:
    """用 Ludusavi 数据库给候选补充存档路径，返回命中的候选数。"""
    if manifest is None or not getattr(manifest, "is_loaded", False):
        return 0
    hits = 0
    for c in candidates:
        try:
            paths, why = manifest.save_paths_for(c.name, c.install_dir, extra_roots)
        except Exception:  # noqa: BLE001
            log.debug("Ludusavi 查询失败：%s", c.name, exc_info=True)
            continue
        added = 0
        for p, w in zip(paths, why):
            if all(path_key(p) != path_key(x) for x in c.save_paths):
                c.save_paths.append(p)
                c.save_evidence.append(w)
                added += 1
        if added:
            hits += 1
    if hits:
        log.info("Ludusavi 数据库为 %d 个候选补充了存档路径", hits)
    return hits


def apply_registry_save_paths(
    candidates: Sequence[Candidate],
    per_game_limit: int = 4,
    max_games: int = 200,
    cancel: Optional[Any] = None,
) -> int:
    """在 ``HKCU\\Software`` 里找与游戏同名的键，取出其中记录的存档路径。

    很多日式引擎会把存档目录直接写在注册表里，这是找存档的一条捷径。
    只读 ``KEY_READ``，不写任何键值。
    """
    from . import systemscan  # 局部导入，避免与 systemscan 形成循环

    hits = 0
    for i, c in enumerate(candidates):
        if i >= max_games:
            break
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            break
        try:
            found = systemscan.find_registry_save_paths(
                c.name, c.install_dir, limit=per_game_limit
            )
        except Exception:  # noqa: BLE001
            log.debug("注册表存档查找失败：%s", c.name, exc_info=True)
            continue
        added = 0
        for p, why in found:
            if all(path_key(p) != path_key(x) for x in c.save_paths):
                c.save_paths.append(p)
                c.save_evidence.append(why)
                added += 1
        if added:
            hits += 1
    if hits:
        log.info("注册表为 %d 个候选补充了存档路径", hits)
    return hits

