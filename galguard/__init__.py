# -*- coding: utf-8 -*-
"""Galgame 存档守护 —— 纯本地、零污染的视觉小说存档扫描与快照备份工具。

本包不实现、也永远不实现以下任何一项：
    * 开机自启
    * 启动文件夹写入
    * 计划任务
    * Windows 服务
    * 常驻守护进程

程序只在用户主动双击启动后运行，用户选择“彻底退出”后进程完全消失。
"""

from __future__ import annotations

APP_NAME = "Galgame 存档守护"
APP_ID = "GalgameSaveGuard"
APP_VERSION = "1.0.0"
SCHEMA_VERSION = 1

# 托盘图标与窗口标题统一使用
WINDOW_TITLE = f"{APP_NAME} v{APP_VERSION}"

__all__ = ["APP_NAME", "APP_ID", "APP_VERSION", "SCHEMA_VERSION", "WINDOW_TITLE"]
