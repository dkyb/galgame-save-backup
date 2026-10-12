# -*- coding: utf-8 -*-
"""可扩展检测规则。

规则以 JSON 保存在 ``<总数据根目录>\\rules\\detect_rules.json``，
首次运行时由内置默认规则生成；用户可自行编辑、增删引擎签名与存档目录名，
重启程序后生效（界面“规则”页也提供“重新加载”与“恢复默认”按钮）。

规则驱动的是“**启发式**”检测，必然存在误判与漏判：
    * 同人 / 汉化 / 破解目录结构千奇百怪；
    * 部分引擎把存档写在注册表或加密容器里；
    * 少数游戏使用随机目录名。
因此程序始终提供“手动添加游戏”作为兜底。
"""

from __future__ import annotations

import fnmatch
import json
import os
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import EngineHit
from .paths import PathGuard, normalize

__all__ = ["DEFAULT_RULES", "RuleSet", "RULES_FILENAME", "RULES_SUBDIR"]

RULES_SUBDIR = "rules"
RULES_FILENAME = "detect_rules.json"


def _has_cjk(s: str) -> bool:
    """是否含中日文字符。"""
    return any(
        ("\u4e00" <= ch <= "\u9fff") or ("\u3040" <= ch <= "\u30ff")
        for ch in (s or "")
    )


# ---------------------------------------------------------------------------
# 内置默认规则
# ---------------------------------------------------------------------------

DEFAULT_RULES: Dict[str, Any] = {
    "version": 2,
    "comment": (
        "Galgame 存档守护 检测规则 v2。相对 v1 的关键修正：删除 Unity/RPGMaker 的 localappdata_subdirs:[\".\"]（它会把整个 %LOCALAPPDATA% 当存档根）；补齐 BGI/KiriKiri/CatSystem2/WillPlus 等引擎的真实存档文件名与目录名；新增 save_path_gate 路径准入闸门。可自由增删条目，保存后重启程序生效。"
        "glob 使用 fnmatch 语法，'data/*.arc' 这类含 '/' 的写法会匹配相对安装目录最多 2 层的路径。"
    ),

    # --- 引擎特征 ---------------------------------------------------------
    # weight 越高说明该特征越独特（越不容易误判）。
    "engine_signatures": [
        {
            "engine": "Ren'Py",
            "weight": 4,
            "dirs": ["renpy", "game/python-packages"],
            "globs": ["*.rpa", "*.rpy", "*.rpyc", "*.rpymc", "*.rpyb"],
            "save_subdirs": ["game/saves", "saves", "save", "game/save"],
            "appdata_subdirs": ["RenPy"],
            "notes": ("权威存档位置是 %APPDATA%\\RenPy\\<游戏名>；"
                      "安装目录下的 game/saves 常常只有 sync 桩文件，不是权威来源"),
        },
        {
            "engine": "KiriKiri",
            "weight": 3,
            "dirs": [],
            "globs": ["*.xp3", "*.ks", "*.tjs", "*.asd"],
            "save_subdirs": ["savedata", "save", "saveData", "savedatas", "savedata_cn"],
            "notes": ("存档在安装目录 savedata/（汉化版常叫 savedata_cn）；"
                      "特征文件 *.ksd / *.cfu / data_anchor.ksd"),
        },
        {
            "engine": "NScripter/ONScripter",
            "weight": 3,
            "dirs": [],
            "globs": ["nscript.dat", "arc.nsa", "*.nsa", "*.nbz", "*._bp", "nscr_sec.dat", "onscript"],
            "save_subdirs": [".", "savedata", "save"],
            "notes": ("经典 NScripter 把 save*.dat 直接写在安装目录下。"
                      "注意 \".\" 只表示安装目录本身，不允许向外扩"),
        },
        {
            "engine": "BGI/Ethornell",
            "weight": 3,
            "dirs": [],
            "globs": ["*.arc", "BGI.exe", "BGI.chs.exe", "*.bgi", "ethornell*.dll"],
            "save_subdirs": ["save", "savedata", "userdata", "UserData", "Savcn"],
            "notes": "BGI 存档是 *.sud + BGI.gdb，放在 userdata/ 或 Savcn/（大小写都要照顾）",
        },
        {
            "engine": "Siglus/RealLive",
            "weight": 4,
            "dirs": [],
            "globs": [
                "SiglusEngine.exe", "Scene.pck", "Gameexe.dat", "Gameexe.ini",
                "RealLive.exe", "seen.txt", "*.g00", "SiglusEngine*.exe",
            ],
            "save_subdirs": ["save", "savedata"],
            "documents_subdirs": ["."],
        },
        {
            "engine": "Unity",
            "weight": 2,
            "dirs": ["Managed", "StreamingAssets"],
            "globs": ["UnityPlayer.dll", "globalgamemanagers", "*.assets", "*.unity3d", "app.info"],
            "save_subdirs": ["save", "saves", "SaveData"],
            "locallow_subdirs": ["${app_company}/${app_product}"],
            "notes": ("存档在 %USERPROFILE%\\AppData\\LocalLow\\<公司>\\<产品>，"
                      "公司/产品从游戏目录的 app.info 确定性读出。"
                      "v1 用 localappdata_subdirs:[\".\"] 等于把整个 %LOCALAPPDATA% 当存档根，已删除"),
        },
        {
            "engine": "CatSystem2",
            "weight": 3,
            "dirs": [],
            "globs": ["*.cst", "*.int", "cs2config.dat", "*.noa"],
            "save_subdirs": ["save", "savedata", "data/save"],
            "appdata_subdirs": ["${app_company}"],
            "notes": "常写到 %APPDATA%\\<厂商>\\<游戏>\\save（例如 %APPDATA%\\Sphere\\<游戏>\\save）",
        },
        {
            "engine": "YU-RIS",
            "weight": 3,
            "dirs": [],
            "globs": ["*.ypf", "yscfg.dat", "*.ybn"],
            "save_subdirs": ["save", "savedata"],
        },
        {
            "engine": "AliceSoft",
            "weight": 4,
            "dirs": [],
            "globs": ["*.afa", "AliceStart.ini", "*.ald", "*.ain", "*.dat.afa"],
            "save_subdirs": ["SaveData", "save", "savedata", "System40.ini"],
        },
        {
            "engine": "Artemis",
            "weight": 3,
            "dirs": [],
            "globs": ["*.pfs", "*.iet", "*.alf"],
            "save_subdirs": ["save", "savedata"],
            "appdata_subdirs": ["${app_company}"],
        },
        {
            "engine": "Nitroplus",
            "weight": 3,
            "dirs": [],
            "globs": ["*.npk", "*.npa", "*.nsa"],
            "save_subdirs": ["save", "savedata"],
        },
        {
            "engine": "Majiro",
            "weight": 2,
            "dirs": [],
            "globs": ["*.arc", "data/*.arc", "*.mjo"],
            "save_subdirs": ["save", "savedata"],
        },
        {
            "engine": "RPG Maker (MV/MZ)",
            "weight": 3,
            # 注意：这里**刻意不再用** ["www","js","data"] 这种目录名做特征 ——
            # 太多 Electron/工具类软件都有 js、data 目录，会大面积误报。
            # 改成只认 RPG Maker 专有的文件名。
            "dirs": [],
            "globs": [
                "nw.dll", "*.rpgmvp", "*.rpgmvo", "*.rpgmvm", "*.rpgproject",
                "www/data/System.json", "www/js/rpg_core.js", "js/rpg_core.js",
                "www/data/Map001.json", "Game.rpgproject",
            ],
            "save_subdirs": ["save", "www/save"],
            "localappdata_exact": True,
            "notes": ("存档常在 %LOCALAPPDATA%\\<游戏名> 或安装目录 save/。"
                      "v1 的 localappdata_subdirs:[\".\"] 过宽已删除，"
                      "改为按游戏名/安装目录名做精确同名匹配"),
        },
    ],

    # --- 存档目录名候选 ---------------------------------------------------
    "save_dir_names": [
        "save", "saves", "savedata", "save_data", "savedatas", "savegame", "savegames",
        "savefiles", "userdata", "user_data", "systemdata",
        "savcn", "savedata_cn",
        "存档", "存档数据", "游戏存档", "全cg存档", "cg存档",
        "セーブ", "セーブデータ", "データ", "ゆうごう",
    ],

    # --- 太通用的目录名 -----------------------------------------------------
    # profile / storage 这类名字浏览器和聊天软件也在用，所以**只在安装目录的
    # 直接子目录**里才认，不做全局名字匹配。
    "generic_risky_dir_names": ["profile", "storage", "book"],

    # --- 安装目录里常见的“非游戏”可执行文件（用于打负分） -------------------
    "install_exe_exclude": [
        "unins*", "uninstall*", "setup*", "install*", "installer*", "vcredist*",
        "dxsetup*", "dxwebsetup*", "directx*", "config*", "setting*", "launcher*",
        "update*", "updater*", "crash*", "report*", "redist*", "helper*", "7z*",
        "winrar*", "python*", "pythonw*", "notepad*", "start*", "run*", "注册*",
        # 崩溃处理器 / 引擎运行时 —— 以前只写了 crash*，匹配不到 UnityCrashHandler64.exe
        "unitycrashhandler*", "unityplayer*", "crashhandler*", "dxrepair*",
        "k-lite*", "codec*", "vcredist_*", "dotnet*", "ffmpeg*", "obs*",
    ],

    # --- 扫描时忽略的目录名 -----------------------------------------------
    "ignore_dir_names": [
        "_commonredist", "commonredist", "redist", "directx", "vcredist", "dotnet",
        "support", "manual", "manuals", "patch", "update", "backup", "backups",
        "$recycle.bin", "system volume information", "node_modules", "__pycache__",
        ".git", ".svn", ".hg", ".vs", ".idea", "temp", "tmp", "cache",
        # 下面这些是“扫描整个磁盘”时用来跳过系统目录的（只影响扫描范围，不影响读写权限）
        "windows", "winsxs", "programdata", "$windows.~bt", "$windows.~ws",
        "recovery", "perflogs", "msocache", "drivers", "assembly",
        # 浏览器内核 / UWP / Electron 的缓存目录（误检重灾区）
        "cache_data", "gpucache", "dawnwebgpucache", "dawngraphitecache",
        "code cache", "service worker", "shadercache", "webcache", "grshadercache",
        "crashpad", "blob_storage", "indexeddb", "local storage", "session storage",
        "ebwebview", "msedgewebview2", "cef", "chromium", "crashdumps",
        "download", "installer", "installers",
        "pip", "npm-cache", "pnpm-cache", "pnpm-store", "yarn-cache", "nuget",
        "thumbnails", "iconcache",
    ],

    # --- 游戏名清洗（按顺序应用正则） --------------------------------------
    "name_strip_patterns": [
        "\\[[^\\]]*\\]",
        "【[^】]*】",
        "\\([^)]*\\)",
        "（[^）]*）",
        "[\\s_\\-]+v?\\d+(\\.\\d+)+[a-z]?\\s*$",
        "\\s+(CHS|CHT|CN|JP|EN|汉化|汉化版|中文版|中文|生肉|熟肉|完全版|体验版|体験版|Demo|Patch|修正版)\\s*$",
        "[\\s_\\-]+$",
    ],

    # --- 通用/无意义的目录名 -------------------------------------------------
    # 自下资源常见结构：<游戏名>\bin\game.exe、<游戏名>\GameData\xxx.exe。
    # 如果安装目录名落在下面这些里面，就把“游戏名”往上找一层，
    # 否则会得到一堆叫 "bin"、"GameData"、"x64" 的候选，很难认。
    "generic_dir_names": [
        "bin", "bin32", "bin64", "game", "games", "gamedata", "gamefiles",
        "gameroot", "data", "dat", "x64", "x86",
        "win", "win32", "win64", "windows", "pc", "release", "app", "application",
        "contents", "content", "files", "root", "src", "program", "build",
        "dist", "output", "out", "main", "core", "system", "engine", "resource",
        "resources", "assets", "runtime", "startup", "setup", "install",
        "installed", "portable", "crack", "cracked", "nodvd", "disk", "disc",
        "chs", "cht", "jp", "cn", "tw", "en", "japanese", "english", "chinese",
        "游戏", "本体", "正式版", "汉化版", "安装包", "免安装", "硬盘版",
        "绿色版", "解压即玩", "客户端",
        "galgame", "galgames", "gal", "eroge", "visual novel", "vn",
        "游戏库", "资源", "下载", "收藏", "合集", "系列",
    ],

    # --- 判定“这个目录像存档目录”的文件 glob --------------------------------
    # 分成强/弱两档：一个目录**至少要有 1 个强特征，或有 2 个弱特征**才算存档目录。
    # 这是为了排除游戏自己的数据目录 —— 它们常有 config.dat / system.dat，
    # 但那是引擎资源，不是存档。
    "save_file_globs": [
        "save*.dat", "save*.sav", "save*.bin", "save*.json",
        "*.sav", "*.savdata", "*.save", "*.savedata", "*.rpgsave", "*.rmmzsave",
        "savedata*", "global.sav", "global.rpgsave", "*save*.dat", "*save*.sav",
        "savefile*", "clearsave*", "*.ksd", "*.asd",
        # 各引擎的权威存档文件（任务书第 6 节对照表）
        "*.sud", "*.cfu", "*.gdb",
        "data_anchor.ksd", "datasc.ksd", "datasu.ksd", "datavc.ksd",
        "Save[0-9][0-9][0-9]", "Save[0-9][0-9][0-9]Favorite", "Save[0-9][0-9][0-9]GS",
    ],
    "weak_save_file_globs": [
        "config*.dat", "system*.dat", "syssave*", "persistent*",
        "config*.ini", "config*.cfg", "*.sav.bak", "*.ksd.bak", "*.sud.bak",
        "savecheck", "krkr.console.log",
    ],

    # --- 「绝对不算存档目录」的目录名 ---------------------------------------
    # 补丁 / 备份 / 破解 / 缓存目录里也常有 .dat/.sav 之类的文件，
    # 不排除掉就会把补丁目录当成存档，甚至和真正的存档目录重复识别。
    "save_path_exclude_names": [
        "patch", "patches", "update", "updates", "updater", "hotfix", "upgrade",
        "crack", "cracked", "nocd", "nodvd", "keygen", "loader",
        "backup", "backups", "bak", "old", "oldver", "old_version", "_old",
        "save_backup", "savebak", "save_old", "temp", "tmp", "cache", "logs", "log",
        "recycle", "trash", "bin_backup",
        # 用户目录里这些“一看就不是游戏存档”的系统/软件目录
        "downloads", "packages", "microsoft", "programs", "programdata",
        "connecteddevicesplatform", "d3dscache", "crashdumps", "publisher",
        "virtualstore", "assembly", "comms", "iconcache", "history",
        "application data", "temporary internet files",
        "补丁", "汉化补丁", "免dvd补丁", "免cd补丁", "升级补丁", "修正补丁",
        "破解补丁", "补丁包", "补丁文件", "备份", "存档备份", "历史存档",
        "还原", "恢复", "说明", "汉化说明", "readme", "manual", "使用说明",
    ],

    # --- 备份时忽略的临时文件 ---------------------------------------------
    "ignore_file_globs": [
        "*.tmp", "*.temp", "*.part", "*.partial", "*.crdownload", "*.download",
        "~$*", "*.lock", "*.pid", "Thumbs.db", "desktop.ini", ".DS_Store",
    ],

    # --- 用户级存档搜索根（只读扫描） --------------------------------------
    "user_save_rel_roots": [
        "Documents",
        "Saved Games",
        "AppData/Roaming",
        "AppData/Local",
        "AppData/LocalLow",
    ],

    # --- Steam 相关 --------------------------------------------------------
    "steam_userdata_subdirs": ["remote", "local"],
    "steam_max_appid_scan": 400,

    # --- 目录评分阈值 ------------------------------------------------------
    # =====================================================================
    # 存档路径准入闸门：任何候选路径在“入选”和“真正备份”前都必须过这里
    # =====================================================================
    # 检测阶段是启发式“猜”，闸门是结构化硬规则兜底。缺了它，一条猜错的
    # 路径就可能把 %LOCALAPPDATA%\Temp\PCL 或 %USERPROFILE%\Downloads
    # 整个复制进备份，一次写出十几个 GB。
    #
    # 说明：deny_rel_roots 刻意**不含** Documents 与 Saved Games ——
    # 它们本身就是大量 galgame 的合法存档根（Saved Games\<游戏>、
    # Documents\My Games\<游戏>），整目录误用的情况交给体量熔断兜住。
    "save_path_gate": {
        "enabled": True,
        "max_files": 3000,
        "max_bytes": 524288000,
        "max_depth": 8,
        "deny_equals_env": [
            "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PUBLIC",
            "TEMP", "TMP", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
        ],
        "deny_rel_roots": [
            "Downloads", "Desktop", "OneDrive", "3D Objects", "Links", "Favorites",
            "Searches", "Contacts", "Videos", "Music", "Pictures", "AppData/Local/Temp",
        ],
        "deny_rel_appdata": [
            "Temp", "Packages", "Microsoft", "Programs", "CrashDumps",
            "pip", "npm-cache", "pnpm-cache", "pnpm-store", "Yarn", "NuGet",
            "Steam", "Google", "D3DSCache", "NVIDIA", "NVIDIA Corporation", "AMD",
        ],
        "deny_name_substrings": [
            "cache", "gpucache", "dawnwebgpu", "dawngraphite", "code cache",
            "service worker", "shadercache", "webcache", "crashpad", "crashdump",
            "blob_storage", "indexeddb", "local storage", "session storage",
            "ebwebview", "msedgewebview2", "cef", "node_modules", "__pycache__",
            "temp", "tmp", "logs", "log", "download", "downloads",
            "installer", "installers", "redist", "driver", "thumbnails",
            "pip", "npm-", "pnpm-",
        ],
        "require_save_evidence_in_appdata": True,
        "deny_if_ancestor_of_backup_root": True,
        "abort_after_denied_errors": 20,
        "on_exceed": "require_confirm",
    },

    # --- “明显不是游戏”的软件名关键词 ---------------------------------------
    # 只影响「注册表安装记录 / 快捷方式 / 最近使用」这三个来源的候选筛选，
    # 不会影响你自己加进列表的游戏。
    # 这里故意做成**数据**而不是写死在代码里：品牌名应该由使用者自己维护，
    # 换台机器、换个语种都不用改代码。想加自己的屏蔽词直接往数组里追加即可。
    "non_game_name_keywords": [
        "update for", "security update", "hotfix", "kb2", "kb3", "kb4", "kb5",
        "redistributable", "runtime", "driver", "sdk", "toolkit", "service",
        "launcher", "updater", "uninstaller", "installer", "utility",
        "运行库", "支持库", "组件", "驱动", "工具箱", "加速器", "模拟器",
        "管家", "助手", "修复", "装卸载", "卸载程序",
        "browser", "chrome", "firefox", "edge", "opera", "brave",
        "office", "photoshop", "illustrator", "vlc", "potplayer", "obs",
        "blender", "steam", "epic games", "discord", "telegram",
        "defender", "antivirus", "ccleaner", "aida64", "cpu-z", "everything",
        "网盘", "音乐", "视频", "输入法", "壁纸", "看图", "压缩", "下载器",
    ],

    # --- “明显不是游戏主程序”的 exe 名（前缀匹配） -------------------------
    "non_game_exe_prefixes": [
        "unins", "uninstall", "setup", "install", "vcredist", "dxsetup",
        "dxwebsetup", "python", "pythonw", "javaw", "node", "npm", "git",
        "code", "devenv", "maintenancetool", "updater", "update", "crashpad",
        "elevate", "unitycrashhandler", "unityplayer",
    ],

    "min_game_dir_score": 2,
}


# ---------------------------------------------------------------------------
# RuleSet
# ---------------------------------------------------------------------------

@dataclass
class _DirIndex:
    """安装目录的浅层索引，避免对每个候选目录反复遍历。"""

    dir_names: Set[str] = field(default_factory=set)
    file_names: Set[str] = field(default_factory=set)
    rel_paths: Set[str] = field(default_factory=set)
    truncated: bool = False


class RuleSet:
    """规则集对象：负责加载、保存、匹配。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = json.loads(json.dumps(DEFAULT_RULES))
        if data:
            self._merge(data)

    # -- 加载 / 保存 --------------------------------------------------------

    def _merge(self, data: Dict[str, Any]) -> None:
        """浅合并：用户规则里出现的键覆盖默认值，其余保留默认。"""
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                self.data[k].update(v)
            else:
                self.data[k] = v

    @classmethod
    def load(cls, guard: PathGuard) -> "RuleSet":
        """从数据根目录加载规则；不存在时写出默认规则文件。"""
        path = Path(guard.data_root) / RULES_SUBDIR / RULES_FILENAME
        if not path.is_file():
            try:
                guard.write_json(path, DEFAULT_RULES)
            except Exception:
                pass
            return cls()
        raw = guard.read_json(path, default=None)
        if not isinstance(raw, dict):
            return cls()

        # --- 规则版本升级 -------------------------------------------------
        # v1 有个致命缺陷：Unity / RPG Maker 的 localappdata_subdirs:["."]，
        # 等于把整个 %LOCALAPPDATA% 当存档根，会把 Temp/Packages/缓存目录
        # 全部当成存档复制走。老用户的规则文件必须换掉。
        try:
            ver = int(raw.get("version", 1) or 1)
        except (TypeError, ValueError):
            ver = 1
        if ver < int(DEFAULT_RULES.get("version", 1)):
            # 先把老规则另存一份，方便用户找回自己的自定义
            try:
                guard.write_json(path.with_name("detect_rules.v%d.bak.json" % ver), raw)
            except Exception:  # noqa: BLE001
                pass
            upgraded = json.loads(json.dumps(DEFAULT_RULES))
            for k, v in raw.items():
                if k not in DEFAULT_RULES:
                    upgraded[k] = v          # 用户自己加的键保留
            try:
                guard.write_json(path, upgraded)
            except Exception:  # noqa: BLE001
                pass
            return cls(upgraded)
        return cls(raw)

    def save(self, guard: PathGuard) -> Path:
        path = Path(guard.data_root) / RULES_SUBDIR / RULES_FILENAME
        return guard.write_json(path, self.data)

    def reset_to_default(self, guard: PathGuard) -> Path:
        self.data = json.loads(json.dumps(DEFAULT_RULES))
        return self.save(guard)

    # -- 规则访问 -----------------------------------------------------------

    @property
    def save_dir_names(self) -> List[str]:
        return [str(x).lower() for x in self.data.get("save_dir_names", [])]

    @property
    def ignore_dir_names(self) -> Set[str]:
        return {str(x).lower() for x in self.data.get("ignore_dir_names", [])}

    @property
    def ignore_file_globs(self) -> List[str]:
        return [str(x).lower() for x in self.data.get("ignore_file_globs", [])]

    @property
    def exe_exclude(self) -> List[str]:
        return [str(x).lower() for x in self.data.get("install_exe_exclude", [])]

    @property
    def min_game_dir_score(self) -> int:
        return int(self.data.get("min_game_dir_score", 2) or 2)

    @property
    def steam_userdata_subdirs(self) -> List[str]:
        return [str(x) for x in self.data.get("steam_userdata_subdirs", ["remote"])]

    @property
    def steam_max_appid_scan(self) -> int:
        return int(self.data.get("steam_max_appid_scan", 400) or 400)

    def is_ignored_dir(self, name: str) -> bool:
        low = name.lower()
        return low in self.ignore_dir_names

    def is_temp_file(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatch.fnmatch(low, pat) for pat in self.ignore_file_globs)

    def is_excluded_exe(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatch.fnmatch(low, pat) for pat in self.exe_exclude)

    # -- 通用目录名 / 存档特征文件 ------------------------------------------

    @property
    def generic_dir_names(self) -> Set[str]:
        return {str(x).strip().lower() for x in self.data.get("generic_dir_names", [])}

    def is_generic_dir(self, name: str) -> bool:
        """目录名是否是 bin / game / x64 这种“没有信息量”的通用名。"""
        low = (name or "").strip().lower()
        if len(low) <= 1:
            return True
        if low in self.generic_dir_names:
            return True
        # "bin-x64"、"data_win" 这类组合也视为通用
        for sep in ("-", "_", " "):
            if sep in low:
                head = low.split(sep, 1)[0]
                if head in self.generic_dir_names:
                    return True
        return False

    @property
    def non_game_name_keywords(self) -> List[str]:
        """“明显不是游戏”的软件名关键词（小写匹配）。"""
        return [str(x).lower() for x in self.data.get("non_game_name_keywords", []) if x]

    @property
    def non_game_exe_prefixes(self) -> List[str]:
        """“明显不是游戏主程序”的 exe 名前缀。"""
        return [str(x).lower() for x in self.data.get("non_game_exe_prefixes", []) if x]

    def is_non_game_name(self, name: str) -> bool:
        low = (name or "").lower()
        if not low:
            return True
        return any(tok in low for tok in self.non_game_name_keywords)

    def is_non_game_exe(self, exe_name: str) -> bool:
        low = (exe_name or "").lower()
        return any(low.startswith(tok) for tok in self.non_game_exe_prefixes)

    @property
    def save_file_globs(self) -> List[str]:
        """强存档特征：出现任意一个就足以判定这是存档目录。"""
        globs = [str(x).lower() for x in self.data.get("save_file_globs", [])]
        return globs or [
            "save*.dat", "save*.sav", "*.sav", "*.savdata", "savedata*", "global.sav",
            "*save*.dat", "*.rpgsave", "save*.bin", "savefile*",
        ]

    @property
    def weak_save_file_globs(self) -> List[str]:
        """弱存档特征：需要同时出现 2 个以上才认为“这是存档目录”。"""
        return [str(x).lower() for x in self.data.get("weak_save_file_globs", [])]

    def looks_like_save_file(self, name: str) -> bool:
        """是否是**强**存档特征文件。"""
        low = (name or "").lower()
        return any(fnmatch.fnmatch(low, pat) for pat in self.save_file_globs)

    def looks_like_weak_save_file(self, name: str) -> bool:
        low = (name or "").lower()
        return any(fnmatch.fnmatch(low, pat) for pat in self.weak_save_file_globs)

    # -- 排除“补丁 / 备份”这类目录 ------------------------------------------

    @property
    def save_path_exclude_names(self) -> Set[str]:
        return {str(x).strip().lower() for x in self.data.get("save_path_exclude_names", [])}

    def is_excluded_save_path_name(self, name: str) -> bool:
        """目录名是不是“补丁 / 备份 / 破解 / 系统垃圾”这类应当忽略的目录。

        既用来排除存档路径候选，也用来在**游戏发现**阶段跳过这些目录
        （否则「免DVD补丁备份」这种目录里只要有个 exe，就会被当成一个游戏）。

        ★ 精度要求：不能一见到「补丁」二字就排除 ——
        很多正经游戏就放在 ``樱花，萌放 v1.08(已打补丁).7z\\`` 这样的目录里。
        所以先把**括号内容、扩展名、版本号**剥掉，只对“核心名”做判断：

        ============================================  ==========
        目录名                                          判定
        ============================================  ==========
        ``免DVD补丁备份`` / ``官方修正补丁备份``           排除
        ``汉化补丁`` / ``补丁`` / ``backup`` / ``备份``    排除
        ``patch_v1.2`` / ``patch``                      排除
        ``Downloads`` / ``Packages`` / ``temp``         排除
        ``樱花，萌放 v1.08(已打补丁).7z``                 保留（是游戏）
        ``Patchwork``                                   保留（不是补丁目录）
        ============================================  ==========
        """
        raw = (name or "").strip().lower()
        if not raw:
            return False
        tokens = self.save_path_exclude_names
        if raw in tokens:
            return True
        squeezed = raw.replace(" ", "_").replace("-", "_")
        if squeezed in tokens:
            return True

        # 1) 剥掉括号里的说明（"(已打补丁)" / "【汉化】"）、压缩包后缀、版本号
        core = re.sub(r"[\(\（\[\【][^\)\）\]\】]*[\)\）\]\】]", "", raw)
        core = re.sub(r"\.(7z|zip|rar|tar|gz|exe|iso|mdf|bin)$", "", core)
        core = re.sub(r"[\s_\-]*v?\d+(\.\d+)*[a-z]?$", "", core)
        core = core.strip(" ._-·，,")
        if not core:
            return False
        if core in tokens:
            return True

        # 2) 中文关键词：核心名里出现就排除（中文关键词本身有足够信息量）
        for tok in tokens:
            if not tok or not _has_cjk(tok) or len(tok) < 2:
                continue
            if tok in core:
                return True

        # 3) 英文关键词：核心名**整体**等于关键词才算（避免误伤 Patchwork 这类真名）
        core_key = re.sub(r"[^a-z0-9]+", "", core)
        for tok in tokens:
            if not tok or _has_cjk(tok):
                continue
            if core_key and core_key == re.sub(r"[^a-z0-9]+", "", tok):
                return True
        return False

    #: 语义更清楚的别名（游戏发现阶段也用它）
    def is_excluded_dir_name(self, name: str) -> bool:
        return self.is_excluded_save_path_name(name)

    def user_save_roots(self) -> List[Path]:
        """返回用户级存档搜索根（只读），不存在的会被过滤掉。"""
        out: List[Path] = []
        for rel in self.data.get("user_save_rel_roots", []):
            p = normalize(Path(os.environ.get("USERPROFILE", "")) / str(rel))
            if p.is_dir() and p not in out:
                out.append(p)
        return out

    def strip_game_name(self, name: str) -> str:
        """按规则清洗游戏名（去掉 [汉化]、版本号、括号备注等）。"""
        import re

        out = name
        for pat in self.data.get("name_strip_patterns", []):
            try:
                out = re.sub(pat, "", out, flags=re.IGNORECASE)
            except re.error:
                continue
        out = out.strip(" -_·.")
        return out or name.strip()

    # -- 引擎识别 -----------------------------------------------------------

    def build_index(self, install_dir: Path, max_entries: int = 6000) -> _DirIndex:
        """只遍历到第 2 层，构造浅层索引。"""
        idx = _DirIndex()
        root = str(install_dir)
        count = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                rel = os.path.relpath(dirpath, root)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth >= 2:
                    dirnames[:] = []
                keep = []
                for d in dirnames:
                    if self.is_ignored_dir(d):
                        continue
                    keep.append(d)
                    idx.dir_names.add(d.lower())
                    if rel != ".":
                        idx.rel_paths.add(os.path.join(rel, d).replace("\\", "/").lower())
                    else:
                        idx.rel_paths.add(d.lower())
                dirnames[:] = keep
                for f in filenames:
                    idx.file_names.add(f.lower())
                    if rel != ".":
                        idx.rel_paths.add(os.path.join(rel, f).replace("\\", "/").lower())
                    else:
                        idx.rel_paths.add(f.lower())
                count += len(filenames) + len(dirnames)
                if count > max_entries:
                    idx.truncated = True
                    break
        except OSError:
            pass
        return idx

    def detect_engines(self, install_dir: Path, index: Optional[_DirIndex] = None) -> List[EngineHit]:
        """返回按权重降序排列的引擎命中列表。"""
        idx = index if index is not None else self.build_index(install_dir)
        hits: List[EngineHit] = []
        for sig in self.data.get("engine_signatures", []):
            engine = str(sig.get("engine", "")).strip()
            if not engine:
                continue
            weight = int(sig.get("weight", 1) or 1)
            reasons: List[str] = []
            score = 0

            for d in sig.get("dirs", []) or []:
                d_low = str(d).lower()
                if d_low in idx.dir_names or d_low in idx.rel_paths:
                    score += weight
                    reasons.append(f"目录 {d}")
                    break
            for g in sig.get("globs", []) or []:
                g_low = str(g).lower()
                hit = None
                if "/" in g_low:
                    for rp in idx.rel_paths:
                        if fnmatch.fnmatch(rp, g_low):
                            hit = rp
                            break
                else:
                    for fn in idx.file_names:
                        if fnmatch.fnmatch(fn, g_low):
                            hit = fn
                            break
                if hit:
                    score += weight
                    reasons.append(f"文件 {hit}")
                    break

            if score > 0:
                hits.append(EngineHit(engine=engine, weight=score, reasons=reasons))

        hits.sort(key=lambda h: -h.weight)
        return hits

    def confidence_label(self, hit: Optional[EngineHit]) -> str:
        if hit is None:
            return "未知"
        if hit.weight >= 6:
            return "高"
        if hit.weight >= 3:
            return "中"
        return "低"

    def signature_for(self, engine: str) -> Optional[Dict[str, Any]]:
        for sig in self.data.get("engine_signatures", []):
            if str(sig.get("engine", "")) == engine:
                return sig
        return None
