# -*- coding: utf-8 -*-
"""存档路径准入闸门 —— 任何候选路径在**入选**和**真正备份**之前都必须过这里。

为什么需要单独一层？

    检测阶段的“启发式”本质上是“猜”。只要有一条路径猜错，代价可能就是
    把 `%LOCALAPPDATA%\\Temp\\PCL`、`%USERPROFILE%\\Downloads` 这种目录
    整个复制进备份，一次就能写出十几个 GB，还会把程序自己的 `backups\\` 卷进去。

    所以闸门用**结构化的硬规则**兜底，而不是靠文件名猜测：
    路径层级（是不是环境变量的根、是不是位于 Temp/Packages/Downloads 之下）、
    任一层目录名是否命中缓存类特征、是否与数据根/备份根互为祖先或后代。

设计要点（与检测逻辑解耦）：

* 只认**通用结构**与环境变量，不含任何具体用户名、盘符、第三方应用名；
* ``REJECT`` 是硬拒绝；``CONFIRM`` 表示“规模超限，需要人确认”，默认**不备份**；
* 体量统计**边数边超限即返回**，不会为了统计把几十万文件全走一遍。
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .logsetup import get_logger

__all__ = ["Decision", "SavePathGate", "norm_path"]

log = get_logger("pathgate")


class Decision:
    """闸门裁决结果。"""

    OK = "ok"            # 可以备份
    REJECT = "reject"    # 明确拒绝，绝不备份
    CONFIRM = "confirm"  # 规模超限，需要用户确认（默认不备份）


def norm_path(p: Any) -> str:
    """规范化路径以便比较：展开环境变量、绝对化、去尾分隔符、Windows 下折叠大小写。"""
    try:
        s = os.path.abspath(os.path.expandvars(str(p)))
    except Exception:  # noqa: BLE001
        s = str(p)
    s = s.rstrip("\\/")
    return os.path.normcase(s) if os.name == "nt" else s


def _env_values(names: Iterable[str]) -> List[str]:
    out: List[str] = []
    for n in names:
        v = os.environ.get(n)
        if v:
            out.append(v)
    return out


class SavePathGate:
    """按 ``detect_rules.json`` 的 ``save_path_gate`` 节校验候选存档路径。"""

    def __init__(
        self,
        rules: Any,
        backup_root: Any = None,
        data_root: Any = None,
        logger: Any = None,
    ) -> None:
        data: Dict[str, Any] = {}
        if rules is None:
            data = {}
        elif isinstance(rules, dict):
            data = rules
        else:
            # 传进来的是 RuleSet 对象
            data = getattr(rules, "data", {}) or {}
        cfg: Dict[str, Any] = data.get("save_path_gate", {}) or {}

        self.enabled = bool(cfg.get("enabled", True))
        self.max_files = int(cfg.get("max_files", 3000) or 0)
        self.max_bytes = int(cfg.get("max_bytes", 512 * 1024 * 1024) or 0)
        self.max_depth = int(cfg.get("max_depth", 8) or 8)
        self.abort_after_denied_errors = int(cfg.get("abort_after_denied_errors", 20) or 20)
        self.on_exceed = str(cfg.get("on_exceed", "require_confirm") or "require_confirm")
        self.deny_if_ancestor_of_backup_root = bool(
            cfg.get("deny_if_ancestor_of_backup_root", True)
        )
        self.log = logger or log

        self.backup_root = norm_path(backup_root) if backup_root else None
        self.data_root = norm_path(data_root) if data_root else None

        #: 等于这些环境变量根目录的路径一律拒绝
        self.deny_abs = {norm_path(p) for p in _env_values(cfg.get("deny_equals_env", []) or [])}
        #: 用户主目录下不允许整目录备份的一级位置
        self.deny_rel_user = {
            str(x).replace("/", "\\").strip("\\").lower()
            for x in (cfg.get("deny_rel_roots", []) or [])
        }
        #: %LOCALAPPDATA% / %APPDATA% 下不允许的位置
        self.deny_rel_appdata = {
            str(x).replace("/", "\\").strip("\\").lower()
            for x in (cfg.get("deny_rel_appdata", []) or [])
        }
        #: 任意一层目录名命中就拒绝（缓存/临时类特征）
        self.deny_substr = [str(s).lower() for s in (cfg.get("deny_name_substrings", []) or []) if s]

        #: 用户数据目录（%USERPROFILE% 下）里的候选，必须拿出“存档证据”才算数。
        #  光看路径形状拦不住 `%LOCALAPPDATA%\MI\XiaomiPCManager`、
        #  `%LOCALAPPDATA%\DingTalk_133\TpcdMetadata` 这类“在 AppData 里凭空出现的目录”。
        self.require_evidence = bool(cfg.get("require_save_evidence_in_appdata", True))
        ev: List[str] = []
        for _key in ("save_file_globs", "weak_save_file_globs"):
            ev += [str(x).lower() for x in (data.get(_key) or [])]
        self.evidence_globs = ev
        self._user_root = norm_path(os.environ.get("USERPROFILE", "")) if os.environ.get("USERPROFILE") else ""

    # ------------------------------------------------------------------

    def check(self, path: Any, quick: bool = False) -> Tuple[str, str]:
        """裁决一条候选路径。

        ``quick=True`` 时跳过体量统计（检测阶段用，避免为每个候选都去数文件）。
        """
        if not self.enabled:
            return Decision.OK, ""

        raw = str(path or "")
        if not raw.strip():
            return Decision.REJECT, "路径为空"

        n = norm_path(raw)
        if not n:
            return Decision.REJECT, "路径为空"

        # 0) 程序自己的数据根 / 备份根 —— 绝不能当存档（否则备份套备份）
        for label, root in (("备份根目录", self.backup_root), ("数据根目录", self.data_root)):
            if not root:
                continue
            if n == root:
                return Decision.REJECT, f"该路径就是{label}本身"
            if n.startswith(root + os.sep):
                return Decision.REJECT, f"该路径位于{label}内部，会造成递归自备份"
            if self.deny_if_ancestor_of_backup_root and root.startswith(n + os.sep):
                return Decision.REJECT, f"该路径是{label}的祖先，会造成递归自备份"

        # 1) 等于系统/用户环境根目录
        if n in self.deny_abs:
            return Decision.REJECT, f"路径等于系统或用户环境根目录（{raw}）"

        # 2) 用户主目录下的一级位置
        up = os.environ.get("USERPROFILE")
        if up:
            upn = norm_path(up)
            if n == upn:
                return Decision.REJECT, "路径等于用户主目录"
            if n.startswith(upn + os.sep):
                rel = n[len(upn) + 1:].replace("/", "\\").lower()
                for d in self.deny_rel_user:
                    if rel == d or rel.startswith(d + "\\"):
                        return Decision.REJECT, f"路径位于用户目录「{d}」之下，不是游戏存档位置"

        # 3) %LOCALAPPDATA% / %APPDATA% 下的系统与缓存目录
        for env_name in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env_name)
            if not base:
                continue
            bn = norm_path(base)
            if n == bn:
                return Decision.REJECT, f"路径等于 %{env_name}%"
            if n.startswith(bn + os.sep):
                rel = n[len(bn) + 1:].replace("/", "\\").lower()
                for d in self.deny_rel_appdata:
                    if rel == d or rel.startswith(d + "\\"):
                        return Decision.REJECT, (
                            f"路径位于 %{env_name}%\\{d} 之下，判定为系统/缓存目录"
                        )

        # 4) 任意一层目录名命中缓存/临时特征
        try:
            parts = [x.lower() for x in Path(n).parts]
        except (OSError, ValueError):
            parts = [x.lower() for x in str(n).replace("\\", "/").split("/")]
        for sub in self.deny_substr:
            for part in parts:
                if sub and sub in part:
                    return Decision.REJECT, f"路径含缓存/临时特征名「{sub}」"

        if quick:
            return Decision.OK, ""

        # 5) 走一遍目录：顺便统计规模、以及“有没有存档特征文件”
        stats = self._stats(n)
        if stats is not None:
            files, total, evidence, exceeded = stats
            if exceeded:
                return (
                    Decision.CONFIRM,
                    "规模超限（已统计 %d 个文件 / %.1f MB），需人工确认"
                    % (files, total / 1048576.0),
                )
            # 用户数据目录里的候选必须自证“这是存档”：
            #   %LOCALAPPDATA%\MI\XiaomiPCManager 这种目录里一个存档特征文件都没有，
            #   却因为名字碰巧沾边就被当成存档 —— 这条规则就是专门拦它的。
            #   注意：目录**不存在**时不做这个判断（用户可能只是还没存过档，
            #   或者别的机器上的配置指过来），只拦“真实存在却没有存档证据”的目录。
            if (self.require_evidence and os.path.isdir(n)
                    and self._under_user_root(n) and evidence == 0):
                return (
                    Decision.REJECT,
                    "位于用户数据目录下，但里面没有任何存档特征文件（%d 个文件都不是存档）"
                    % files,
                )
        return Decision.OK, ""

    def _under_user_root(self, n: str) -> bool:
        return bool(self._user_root) and (
            n == self._user_root or n.startswith(self._user_root + os.sep)
        )

    # ------------------------------------------------------------------

    def _stats(self, path: str) -> Optional[Tuple[int, int, int, bool]]:
        """一次遍历同时得到：文件数、总字节、存档证据文件数、是否超限。

        超限时**立刻返回**，不会为了统计把几十万文件全走一遍。
        """
        files = 0
        total = 0
        evidence = 0
        try:
            for root, dirs, names in os.walk(path, onerror=lambda _e: None):
                try:
                    depth = root[len(path):].count(os.sep)
                except (ValueError, TypeError):
                    depth = 0
                if depth >= self.max_depth:
                    dirs[:] = []
                for f in names:
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
                    if evidence == 0 and depth <= 2 and self.evidence_globs:
                        low = f.lower()
                        for g in self.evidence_globs:
                            if fnmatch.fnmatch(low, g):
                                evidence += 1
                                break
                    if (self.max_files > 0 and files > self.max_files) or \
                       (self.max_bytes > 0 and total > self.max_bytes):
                        return files, total, evidence, True
        except OSError:
            return None
        if self.require_evidence and evidence == 0:
            return files, total, 0, False
        return files, total, evidence, False

    def measure(self, path: str) -> Optional[Tuple[str, str]]:
        """兼容接口：只做体量熔断判断。"""
        stats = self._stats(path)
        if stats is None:
            return None
        files, total, _ev, exceeded = stats
        if exceeded:
            return (
                Decision.CONFIRM,
                "规模超限（已统计 %d 个文件 / %.1f MB），需人工确认"
                % (files, total / 1048576.0),
            )
        return None

    def filter_paths(
        self, paths: Sequence[str], quick: bool = False
    ) -> Tuple[List[str], List[Tuple[str, str]]]:
        """批量过滤，返回 ``(放行的, [(被拒的, 原因), ...])``。"""
        accepted: List[str] = []
        rejected: List[Tuple[str, str]] = []
        for p in paths:
            decision, reason = self.check(p, quick=quick)
            if decision == Decision.OK:
                accepted.append(p)
            else:
                rejected.append((p, f"{decision}: {reason}"))
        return accepted, rejected
