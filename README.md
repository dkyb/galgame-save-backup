# Galgame 存档守护（Galgame Save Guard）

> 一个**纯本地**的 Galgame / 视觉小说存档扫描与快照备份工具。
> 只备份你自己的存档，不绕过 DRM、不修改游戏文件、**不联网、不上传任何数据**。
>
> **只有你主动双击启动它，它才会工作。选择「彻底退出」后，进程完全消失。**

---

## 目录

1. [它做什么 / 不做什么](#1-它做什么--不做什么)
2. [项目结构](#2-项目结构)
3. [安装与运行](#3-安装与运行)
4. [首次启动流程](#4-首次启动流程)
5. [日常使用](#5-日常使用)
6. [关闭主窗口 vs 彻底退出](#6-关闭主窗口-vs-彻底退出)
7. [彻底退出后为什么没有任何后台残留](#7-彻底退出后为什么没有任何后台残留)
8. [零污染：写在哪里、不写在哪里](#8-零污染写在哪里不写在哪里)
9. [如何验证它没写到别的目录（Process Monitor 实操）](#9-如何验证它没写到别的目录process-monitor-实操)
10. [备份格式说明](#10-备份格式说明)
11. [自定义检测规则](#11-自定义检测规则)
12. [自动检测的局限（必读）](#12-自动检测的局限必读)
13. [常见问题 FAQ](#13-常见问题-faq)
14. [打包成 exe（PyInstaller --onedir）](#14-打包成-exepyinstaller---onedir)
15. [卸载](#15-卸载)
16. [验收自检](#16-验收自检)

---

## 1. 它做什么 / 不做什么

### 做

* 扫描你**明确指定**的游戏库根目录，识别可能的 Galgame / 视觉小说。
* Steam 库识别（**只读**注册表 + 解析 `libraryfolders.vdf` / `appmanifest_*.acf`）。
* 基于引擎特征（Ren'Py / KiriKiri / NScripter / BGI / Siglus / RealLive / Unity / CatSystem2 / YU-RIS / AliceSoft / Artemis / Nitroplus / RPG Maker…）推断存档位置。
* 首次确认游戏时建立**存档基线**（文件清单 + 大小 + 修改时间 + 可选 SHA256）。
* 程序运行期间常驻托盘，**静默监控已确认游戏的存档目录**；存档新增/修改/删除时自动生成独立快照。
* 每个快照都带 `manifest.json` 与 `原始存档路径.txt`，明确记录这份备份来自哪个存档路径。
* 手动兜底：自动检测失败时，可手动添加游戏、手动指定任意多个存档路径。

### 不做（而且是刻意不做）

| 不做的事 | 说明 |
| --- | --- |
| ❌ 监控游戏进程 | 不使用 `psutil` 盯游戏 exe，不 hook、不注入、不读游戏内存。**只基于文件系统。** |
| ❌ 开机自启 | 不写启动文件夹、不写 `Run` 注册表键。 |
| ❌ 计划任务 / 系统服务 | 完全不涉及 `schtasks` / `sc` / SCM。 |
| ❌ 后台守护进程 | 进程结束＝工具结束，没有常驻进程。 |
| ❌ 写注册表 | 注册表只读（`KEY_READ`），仅用于读取 Steam 安装路径。 |
| ❌ 联网 | 代码里没有任何网络调用。 |
| ❌ 修改游戏文件 | 对游戏目录 / 存档目录只读；只往备份目录复制。 |

---

## 2. 项目结构

```
galgame_save_guard/
├── run.py                     # 入口（在任何 import 之前关闭字节码写入）
├── selftest.py                # 无界面端到端自检（97 项断言）
├── requirements.txt
├── build_onedir.ps1           # PyInstaller --onedir 打包脚本
├── make_zip.ps1               # 把 dist 打成可分发的独立 zip
├── packaging/使用说明.txt       # 随 zip 一起分发的简短说明
├── README.md
├── .venv/                     # （可选，自己创建）打包用虚拟环境，不影响运行
├── dist/GalgameSaveGuard/     # （可选）打包产物，可整个文件夹拷走直接用
└── galguard/
    ├── __init__.py            # 版本常量
    ├── __main__.py            # python -m galguard 入口
    ├── app.py                 # 装配与生命周期（启动顺序 / 彻底退出顺序）
    ├── bootstrap.py           # 字节码、DPI、temp 重定向、单实例锁、崩溃钩子
    ├── paths.py               # ★ PathGuard 写入白名单（零污染核心）
    ├── logsetup.py            # 滚动日志
    ├── config.py              # config.json 读写 + 各选项数据类
    ├── models.py              # FileStamp / BaselineDiff / GameEntry / Candidate
    ├── db.py                  # SQLite：baseline_files / backups / events
    ├── rules.py               # ★ 可扩展 JSON 检测规则（v2，含版本自动升级）
    ├── pathgate.py            # ★ 存档路径准入闸门（硬规则 + 体量熔断 + 存档证据）
    ├── detect.py              # Steam 发现、引擎启发式、存档推断、存档嗅探
    ├── systemscan.py          # ★ 系统痕迹发现（注册表 / 快捷方式 / 最近使用，全只读）
    ├── manifest.py            # ★ Ludusavi 存档数据库离线导入（自带轻量 YAML 解析）
    ├── scanner.py             # 库扫描 + 系统发现 + 基线建立 + 当前状态快照
    ├── fsutil.py              # SHA256（带内存缓存）、安全遍历、失败重试
    ├── backup.py              # ★ 快照引擎（manifest + 原始存档路径.txt + 保留策略）
    ├── monitor.py             # ★ 文件监控（watchdog / 纯轮询自动降级）+ 防抖
    ├── tasks.py               # 守护线程任务池（刻意不用 ThreadPoolExecutor）
    ├── service.py             # 服务层：后台线程与界面之间的事件桥
    ├── ui_main.py             # 主窗口
    ├── ui_dialogs.py          # 首启向导 / 候选确认 / 手动添加 / 设置
    └── tray.py                # 系统托盘（pystray，可选）
```

运行期在**你指定的数据根目录**里生成：

```
<总数据根目录>/
├── config.json               # 全部配置（含游戏列表）
├── logs/
│   ├── app.log               # 滚动日志（2MB × 5）
│   └── crash.log             # 未捕获异常
├── cache/galguard.lock       # 单实例锁（进程退出即释放）
├── temp/                     # 被重定向的 Python 临时目录
├── data/history.db           # 基线 + 备份历史 + 事件（含 -wal/-shm）
├── rules/detect_rules.json   # 可自行编辑的检测规则
└── exports/                  # 预留导出目录

<备份根目录>/
└── <游戏名>/
    └── 2026-05-04_21-33-07/
        ├── manifest.json
        ├── 原始存档路径.txt
        └── files/…
```

---

## 3. 安装与运行

### 环境

* Windows 11（Windows 10 亦可）
* Python **3.10+**（开发与验证环境为 3.11.9，官方安装包自带 tkinter）
* 无需管理员权限

### 安装依赖

```powershell
# 必装（GUI 用标准库 tkinter，无需安装）
pip install -r requirements.txt
```

`requirements.txt`：

```
watchdog>=3.0.0      # 文件系统监控（Windows 下基于 ReadDirectoryChangesW，纯只读）
pystray>=0.19.5      # 系统托盘图标（仅程序运行期间存在，退出即销毁）
Pillow>=10.0.0       # pystray 需要的内存内图标绘制（不落盘）
```

> **这三个包都是可选的**：
> * 没有 `watchdog` → 自动降级为**纯标准库轮询**监控，功能不变。
> * 没有 `pystray` / `Pillow` → 自动降级为**无托盘**模式，主界面的「彻底退出」照常可用。

### 运行

```powershell
# 推荐：-B 关闭字节码写入，源码目录不会出现 __pycache__
python -B run.py

# 指定数据位置（跳过首启向导，适合做快捷方式）
python -B run.py --data-root "D:\GalgameGuard" --backup-root "E:\GalgameBackups"

# 同时把日志打到控制台（调试用）
python -B run.py --console

# 不创建托盘图标
python -B run.py --no-tray

# 忘记数据位置时：删除“记住路径”的指针文件
python -B run.py --forget-pointer

# 查看版本
python -B run.py --version

# 也可以这样运行
python -B -m galguard
```

> `run.py` 的**第一行代码**就执行 `sys.dont_write_bytecode = True`，
> 所以即使你不加 `-B`，也不会生成 `__pycache__`。加 `-B` 是双保险。

### 程序怎么记住你的数据根目录？

`config.json` 本身就在数据根目录里，因此需要一个“指针”。程序按以下**优先级**寻找数据根目录：

1. 命令行 `--data-root`
2. 环境变量 `GALGUARD_DATA_ROOT` / `GALGUARD_BACKUP_ROOT`
3. 启动器同目录下的指针文件 `galguard.root.json`
4. 都没有 → 弹出**首次启动向导**让你选择

**关于指针文件（唯一可能在数据根目录之外写入的东西）：**

* 首启向导里有一个复选框「记住这个位置」，**只有你勾选**，程序才会在 `run.py` / exe 所在目录写一个 `galguard.root.json`。
* 这个文件只包含两行路径，不含任何其它信息。
* **不勾选** → 程序每次启动都会问一次数据位置，且**绝不在数据根目录之外创建任何文件**。
* 想追求绝对纯净：不要勾选，改用 `--data-root` 参数或 `GALGUARD_DATA_ROOT` 环境变量。
* 删除该文件即可让程序重新询问：`python -B run.py --forget-pointer`

---

## 4. 首次启动流程

1. 双击 / 命令行启动程序。
2. 弹出**首次启动向导**（此时程序还没写过任何文件）：
   * ① 选择**总数据根目录**（必填）—— 配置、日志、缓存、临时文件、数据库、规则、崩溃日志全在这里。
   * ② 选择**备份根目录**（留空＝`总数据根目录\backups`）。
   * ③ 添加一个或多个**游戏库根目录**（例如 `D:\Galgame`），只扫描这些目录，不做全盘扫描。
   * ④ 扫描选项：是否计算 SHA256、是否比较修改时间。
3. 点「开始使用」→ 程序建立标准目录结构、重定向临时目录、写 `config.json`。
4. 界面出现后自动执行一次**全量扫描**，列出**候选游戏**（名称 / 引擎 / 安装目录 / 候选存档路径 / 评分 / 来源）。
5. 在候选窗口里勾选要纳入监控的游戏 → 「加入游戏列表」。
   * 也可以点「手动添加游戏…」补充自动检测漏掉的作品。
6. 程序在后台为每个新游戏**建立存档基线**，之后「是否有更新」才有意义。
7. 若设置里开着「启动后自动开始静默监控」，此时监控已经开始工作。

> 如果向导里取消了，程序会打印一行提示并退出，**磁盘上什么都不会多出来**。

---

## 5. 日常使用

### 主界面

游戏列表列：游戏名称 / 引擎 / 存档路径 / 安装目录 / 上次备份时间 / 备份数 / 是否有更新 / 监控状态。
有更新的行会高亮成淡黄色。点表头可以排序，右键有上下文菜单，双击打开备份目录。

按钮：

| 按钮 | 作用 |
| --- | --- |
| 添加游戏 | 手动添加（自动检测的兜底） |
| 编辑 / 删除 | 修改或移除游戏条目（删除时可选择是否连备份一起删） |
| 扫描游戏库 | 重新扫描库目录，找出候选游戏 |
| 扫描存档更新 | 立即遍历所有游戏，对比基线，刷新「是否有更新」 |
| 备份有更新的游戏 | 对所有标记为有更新的游戏立即生成快照 |
| 备份选中的游戏 | 对**勾选/选中**的游戏生成快照（支持多选） |
| 备份所有游戏 | 对列表里**全部**游戏生成快照，不比较更新（点击前会确认） |
| 全选 / 全不选 / 反选 | 批量操作 ☑ 勾选列 |
| 打开存档目录 / 打开备份目录 | 资源管理器打开（一次一个） |
| 复制存档路径 | 把勾选游戏的存档路径复制到剪贴板（多个游戏会带名字注释） |
| 开始/暂停静默监控 | 切换监控状态（暂停只停自动备份，手动备份仍可用） |
| 设置 | 打开设置页 |
| 彻底退出 | **真正结束程序** |

### 托盘（程序运行期间存在）

* 显示/隐藏主窗口
* 备份所有有更新的游戏
* 暂停监控 / 恢复监控（菜单文字随状态变化）
* 打开数据根目录
* **彻底退出（结束进程）**

### 键盘

* `F5` = 扫描存档更新

---

## 6. 关闭主窗口 vs 彻底退出

这是两个**完全不同**的行为，程序里也用了不同的措辞：

| | 关闭主窗口（点右上角 X） | 彻底退出 |
| --- | --- | --- |
| 触发 | X 按钮 | 主界面「彻底退出」按钮 / 托盘「彻底退出」/ 菜单「文件→彻底退出」 |
| 默认行为 | **最小化到系统托盘**（可在设置里改成“直接彻底退出”） | 一定真正结束程序 |
| 进程 | 继续运行 | 结束，任务管理器里消失 |
| 托盘图标 | 保留 | 移除，立即消失 |
| 文件监控 | 继续 | 全部停止 |
| 能否再打开 | 能从托盘重新打开主窗口 | 不能，无法再从托盘唤醒 |
| 后台任务 | 继续 | 停止，排队中的任务被丢弃 |

设置里可以改：

* **设置 → 常规 → 关闭主窗口时**
  * `最小化到系统托盘（程序继续监控，默认）`
  * `直接彻底退出（停止监控、移除托盘、进程完全结束）`

> 如果环境里没有 `pystray`（无法创建托盘），点 X 时程序会明确问你「要不要直接彻底退出」，
> 而不是偷偷隐藏到一个你找不到的地方。

**彻底退出前会弹一次确认框**，写清楚会做什么，避免误触。

---

## 7. 彻底退出后为什么没有任何后台残留

看 `galguard/app.py` 的 `GalguardApp.quit()`，退出顺序是显式且完整的：

1. **停止文件监控** —— `SaveMonitor.stop()`
   * 取消所有防抖 `threading.Timer`
   * `Observer.stop()` + `join(3s)` 关闭 watchdog 内核通知
   * 置位轮询停止事件并 `join(3s)` 结束轮询线程
2. **停止后台任务池** —— `DaemonPool.stop(drain=True, timeout=4)`
   * 给正在跑的任务最多 4 秒收尾，然后清空队列、丢弃未开始的任务
   * 工作线程**全部是 `daemon=True`**，进程结束时必然消失
3. **关闭数据库** —— `PRAGMA wal_checkpoint(TRUNCATE)` + `close()`，不留半截 WAL
4. **移除托盘图标** —— `pystray.Icon.stop()` → `Shell_NotifyIcon(NIM_DELETE)`，图标立即从任务栏消失
5. **销毁窗口** —— `Tk.destroy()`
6. **释放文件锁** —— `msvcrt.locking(LK_UNLCK)` + 关闭句柄
7. **清理 temp 目录**
8. **刷盘并关闭日志** —— 显式调用，因为下一步会跳过 `atexit`
9. **`os._exit(0)`** —— **立即结束进程**

### 为什么最后要用 `os._exit(0)`？

因为最常见的“退出后仍有残留进程”的元凶是：
`ThreadPoolExecutor` / 非守护线程 / `atexit` 钩子 / 卡在慢速磁盘 IO 上的线程
—— 它们会让解释器在退出时**一直等**，进程就挂在那里成为僵尸。

本程序的对策：

* **刻意不用 `ThreadPoolExecutor`**（它的线程是非守护的，还注册了 `threading._register_atexit`，会阻塞退出）。
  改用自己写的 `DaemonPool`（`galguard/tasks.py`），工作线程全是守护线程。
* 托盘线程、轮询线程、防抖定时器线程全部 `daemon=True`。
* 所有资源在 `os._exit` 之前都已显式释放，所以跳过解释器兜底逻辑是安全的。

### 不留下任何系统痕迹

* 没有服务（`sc query` 里找不到）
* 没有计划任务（`schtasks /query` 里找不到）
* 没有开机自启项
* 没有注册表写入（`galguard` 相关键一个都不会有）

可以用这些命令自查：

```powershell
# 计划任务
schtasks /query /fo LIST | Select-String -Pattern "galguard" -CaseSensitive:$false
# 服务
Get-Service | Where-Object { $_.Name -like "*galguard*" }
# 启动项
Get-CimInstance Win32_StartupCommand | Where-Object { $_.Command -like "*galguard*" }
# 残留进程（彻底退出后应该为空）
Get-Process | Where-Object { $_.Path -like "*galgame*" }
```

---

## 8. 零污染：写在哪里、不写在哪里

### 允许写

* `<总数据根目录>` 及其全部子目录
* `<备份根目录>` 及其全部子目录
* （**仅在你勾选时**）启动器目录下的 `galguard.root.json` 指针文件

### 严禁写

`%APPDATA%`、`%LOCALAPPDATA%`、`%TEMP%`、`%TMP%`、`文档`、`已保存的游戏`、
程序安装目录、游戏安装目录、游戏存档目录、注册表、启动文件夹、计划任务、任何未授权目录。

### 技术保障

| 风险 | 对策 | 代码位置 |
| --- | --- | --- |
| 选择数据根目录之前就写文件 | `PathGuard` 在解锁前拒绝一切写入，抛 `SandboxViolation` | `paths.py` |
| 某处代码忘了校验路径 | 所有写操作统一走 `GUARD.check_write()` / `ensure_dir()` / `write_json()` | `paths.py` |
| 第三方库偷偷写 `%TEMP%` | `tempfile.tempdir` + `TEMP/TMP/TMPDIR` 环境变量全部重定向到 `<数据根目录>\temp` | `bootstrap.py` |
| GUI 库写 AppData / 注册表 | 用标准库 **tkinter**（不写任何配置、不碰注册表），所以不需要重定向 | `ui_*.py` |
| 源码运行时散落 `__pycache__` | `run.py` 首行 `sys.dont_write_bytecode = True` + 推荐 `python -B` | `run.py` |
| 托盘库写文件 | `pystray` 用 Shell_NotifyIcon，图标是内存里的 PIL 图像，不落盘 | `tray.py` |
| 打包后解压到 `%TEMP%` | **只用 `--onedir`，绝不用 `--onefile`**（onefile 会自解压到 `%TEMP%`） | `build_onedir.ps1` |
| 崩溃日志写到 Windows 事件日志 | 自定义 `sys.excepthook` / `threading.excepthook`，写 `<数据根目录>\logs\crash.log` | `bootstrap.py` |
| 删除备份时误删别的东西 | 删除前校验目标确实位于备份根目录内，且不等于备份根目录本身 | `backup.py` |
| 单实例锁残留 | 用文件锁（`msvcrt.locking`），进程结束由系统自动释放 | `bootstrap.py` |

`PathGuard` 的单元验证在 `selftest.py` 的 `[1] 路径沙盒` 一节：
它会对 `%TEMP%`、`%APPDATA%`、程序目录各尝试一次写入，并断言**全部被拦截**。

---

## 9. 如何验证它没写到别的目录（Process Monitor 实操）

这是最硬核的验证方式，推荐自己跑一遍：

1. 下载 Sysinternals **Process Monitor**（微软官方，绿色版）：
   <https://learn.microsoft.com/sysinternals/downloads/procmon>
2. 启动 `Procmon64.exe`，先按 `Ctrl+E` 停止捕获，按 `Ctrl+X` 清空。
3. 设置过滤（Filter → Filter…），添加三条：

   | Column | Relation | Value | Action |
   | --- | --- | --- | --- |
   | Process Name | is | python.exe（打包后是 `GalgameSaveGuard.exe`） | Include |
   | Operation | is | WriteFile | Include |
   | Operation | is | CreateFile | Include |

   再加一条 **Path → begins with → `D:\GalgameGuard`（你的数据根目录）** 的 **Exclude** 规则，
   这样列表里剩下的就全是“数据根目录之外”的写入尝试。
4. 按 `Ctrl+E` 开始捕获，然后运行本程序：走一遍首启向导 → 扫描 → 备份 → 彻底退出。
5. 按 `Ctrl+E` 停止，查看结果。

**预期结果**：

* 列表里只会看到 `CreateFile` 这类**只读打开**（`Desired Access: Read Attributes` / `Generic Read`），
  它们来自对游戏库、文档、AppData 的**只读扫描**，不会有任何 `WriteFile` 落在这三个位置。
* 唯一的例外是你在向导里勾选了「记住这个位置」时，启动器目录下那一次
  `galguard.root.json` 的 `WriteFile`。

### 更轻量的两种验证

```powershell
# A. 用“文件夹的最后修改时间”做粗筛：启动->使用->彻底退出 前后对比
Get-ChildItem $env:APPDATA, $env:LOCALAPPDATA -Directory |
    Sort-Object LastWriteTime -Descending | Select-Object -First 5 FullName, LastWriteTime

# B. 只监控 TEMP 目录有没有新文件
$before = (Get-ChildItem $env:TEMP -Recurse -File -ErrorAction SilentlyContinue).Count
# ……运行程序，做一遍操作并彻底退出……
$after  = (Get-ChildItem $env:TEMP -Recurse -File -ErrorAction SilentlyContinue).Count
"TEMP 文件数变化：$($after - $before)"
```

> 建议把 A/B 作为日常抽查，把 ProcMon 作为一次性严格验收。

---

## 10. 备份格式说明

### 目录

```
<备份根目录>\<游戏名>\<YYYY-MM-DD_HH-MM-SS>\
```

同一个游戏多次备份 = 多个并列的时间戳目录，**互不覆盖**。
目录名冲突时自动加 `_02`、`_03` 后缀。

### `manifest.json`

```jsonc
{
  "schema_version": 1,
  "tool": { "name": "Galgame 存档守护", "version": "1.0.0" },
  "snapshot": {
    "name": "2026-05-04_21-33-07",
    "created_at": "2026-05-04 21:33:07",
    "trigger": "auto",
    "trigger_text": "静默监控检测到存档变化（自动）"
  },
  "game": {
    "id": "…", "name": "…", "engine": "Ren'Py",
    "exe": "…", "install_dir": "…", "notes": "", "source": "auto"
  },
  "原始存档路径": ["D:\\Game\\save"],          // 中文键，方便直接看
  "origin_save_paths": ["D:\\Game\\save"],     // 同样的内容，英文键，方便程序读
  "available_save_paths": ["D:\\Game\\save"],
  "unavailable_save_paths": [],
  "files": [
    {
      "save_path": "D:\\Game\\save",
      "rel_path": "save1.sav",
      "archive_path": "files/save1.sav",
      "size": 12345,
      "mtime": 1778000000.0,
      "mtime_text": "2026-05-04 21:30:00",
      "sha256": "…"
    }
  ],
  "summary": {
    "file_count": 3, "total_bytes": 45678, "total_text": "44.6 KB",
    "skipped_files": 0, "save_path_count": 1, "sha256_computed": true
  }
}
```

### `原始存档路径.txt`

UTF-8 **带 BOM**、CRLF 换行 —— 用记事本草稿双击就能正常显示中文。
内容形如：

```
游戏名称　　：FakeGalgame
游戏引擎　　：Ren'Py
游戏主程序　：D:\Galgame\FakeGalgame\FakeGalgame.exe
安装目录　　：D:\Galgame\FakeGalgame
备份时间　　：2026-05-04 21:33:07
触发方式　　：静默监控检测到存档变化（自动）

本备份来自以下原始存档路径：
[1] D:\Galgame\FakeGalgame\save
      快照内对应位置：files\
```

### `files/` 布局

* 只有 **1 个**存档路径 → 直接放在 `files\` 下（保持原有子目录结构）。
* **多个**存档路径 → `files\00_<目录名>\`、`files\01_<目录名>\` … 避免同名冲突，
  且与 `原始存档路径.txt` 里的编号一一对应。

### 安全性设计

* **先写 `<快照名>.partial`，全部成功后再 `os.replace` 改名为正式目录**。
  断电/崩溃不会留下一个看起来像正常备份的半成品；下次备份会顺手清理掉残留的 `.partial`。
* 复制失败自动重试 3 次（递增间隔）；仍失败则记日志并计入 `skipped_files`。
* 自动跳过临时文件（`*.tmp`、`*.part`、`~$*`、`Thumbs.db` …，可自定义）。
* 默认**不删除任何旧备份**；只有你在设置里选了「最近 N 份」或「按天保留」才会清理。

### 恢复方法

把快照 `files\` 下的内容原样复制回 `原始存档路径.txt` 里对应的路径即可。
（程序不提供“一键恢复”，避免误覆盖你当前的存档。）

---

## 11. 自定义检测规则

规则文件：**`<总数据根目录>\rules\detect_rules.json`**（首次启动自动生成）。
在「设置 → 检测规则」里可以一键打开目录、重新加载、恢复默认。

主要字段：

| 字段 | 说明 |
| --- | --- |
| `engine_signatures` | 引擎特征数组。每项：`engine`（引擎名）、`weight`（权重，越高越独特）、`dirs`（目录名）、`globs`（文件 glob，含 `/` 时匹配相对安装目录 2 层内的路径）、`save_subdirs`（安装目录下常见的存档子目录）、`appdata_subdirs` / `localappdata_subdirs` / `documents_subdirs`（用户级存档位置） |
| `save_dir_names` | 存档目录名候选（会被所有引擎共用） |
| `generic_dir_names` | **通用/无意义的目录名**（`bin`、`gamedata`、`x64`、`汉化版`…）。安装目录名落在这里时，游戏名会自动往上找一层，避免出现叫 `bin` 的候选 |
| `save_file_globs` | **判定“这个目录像存档目录”的文件 glob**。存档嗅探全靠它 —— 自下资源识别不出来时，优先往这里加 |
| `install_exe_exclude` | 安装目录里“非游戏”的 exe 名（用于优先挑主程序；如果全部命中，会兜底取最大的 exe，不会整款漏掉） |
| `ignore_dir_names` | 扫描时跳过的目录名 |
| `name_strip_patterns` | 游戏名清洗正则（去掉 `[汉化]`、版本号、括号备注等） |
| `ignore_file_globs` | 备份时忽略的临时文件 |
| `user_save_rel_roots` | 用户级存档搜索根（相对 `%USERPROFILE%`），只读扫描 |
| `steam_userdata_subdirs` | Steam 云存档子目录名（默认 `remote`、`local`） |
| `min_game_dir_score` | 候选游戏的最低评分阈值 |

### 例子 1：给某个同人引擎加上特征

```json
{
  "engine": "MyDoujinEngine",
  "weight": 4,
  "dirs": ["myengine_data"],
  "globs": ["*.myd", "startup.myi"],
  "save_subdirs": ["savedata", "userdata"]
}
```

把它追加进 `engine_signatures` 数组，然后在设置里点「重新加载规则」。

### 例子 2：某个游戏的存档目录名很特殊

把它加进 `save_dir_names`：

```json
"save_dir_names": ["save", "saves", "savedata", "存档", "我的存档文件夹"]
```

### 例子 3：屏蔽某个总是误报的目录

```json
"ignore_dir_names": ["_commonredist", "redist", "我的工具目录"]
```

### 例子 4：某游戏名带一堆后缀导致匹配不到存档

在 `name_strip_patterns` 里加一条正则，例如去掉 `- Steam Edition`：

```json
"name_strip_patterns": ["\\s*[-_]\\s*Steam\\s*Edition\\s*$"]
```

> 规则改动只影响**扫描与推断**；已经确认并保存的游戏条目不会被自动改写。
> 如果某个游戏已经在列表里，改完规则后请用「编辑」手动补上存档路径。

---

## 12. 自动检测的局限（必读）

自动检测是**启发式**的，**一定**会有漏检和误检。请了解以下情况：

### 程序一共有 4 条找游戏的途径

1. **游戏库根目录** —— 你指定的目录（例如 `D:\GAL`）及其子目录。**这条最重要**，
   自己下载的游戏主要靠它。首启向导和「设置 → 扫描」里都能随时增删，
   也支持「添加整个磁盘…」。
2. **Steam 库** —— 只读注册表 + `libraryfolders.vdf` + `appmanifest_*.acf`。
3. **系统痕迹**（「设置 → 自动发现」）——
   * **注册表安装记录**：安装版游戏一般在 `...\CurrentVersion\Uninstall` 里留了安装位置；
   * **开始菜单 / 桌面快捷方式**：绿色版、免安装版通常有快捷方式；
   * **最近使用（Recent）**：记录的是你**真正运行过**的程序，最贴近需求里的“玩过的游戏”。
4. **Ludusavi 离线数据库**（可选）—— 见下面「借别人的数据库」。

### 借别人的数据库：Ludusavi manifest

[Ludusavi](https://github.com/mtkennerly/ludusavi) 是目前最主流的开源存档备份工具，
它的做法是把 [PCGamingWiki](https://www.pcgamingwiki.com) 上人工维护的
“存档位置”数据定期编译成一份 `manifest.yaml`
（[mtkennerly/ludusavi-manifest](https://github.com/mtkennerly/ludusavi-manifest)），
再按**游戏标题**匹配，把 `<base>` / `<winAppData>` / `<home>` 这类占位符展开成真实路径
（详见 [missing-saves](https://docs.rs/crate/ludusavi/latest/source/docs/help/missing-saves.md)
与 [roots](https://docs.rs/crate/ludusavi/latest/source/docs/help/roots.md) 文档）。

本程序**不联网、不下载**，但支持离线导入：

1. 自己把 `manifest.yaml` 下载到本地（约 10~20 MB）；
2. 「设置 → 自动发现 → Ludusavi 存档数据库」里选中它；
3. 之后每次扫描都会按游戏名查表，命中的存档路径直接填进候选。

> 对商业游戏很有效；对**冷门同人 / 汉化作品覆盖率有限**，那类还是得靠嗅探和手动指定。
> 实现上刻意没有引入 PyYAML，而是为这个文件的固定结构写了逐行解析器
> （`galguard/manifest.py`），所以不会多一个依赖、加载也快。

### 存档路径识别的精度

早期版本是“匹配到就全塞进去”，所以会出现**补丁目录被当成存档**、
**一个游戏给出十几个路径**、**嵌套目录重复识别**。现在改成
「收集候选 → 逐个打分 → 排除补丁 → 父子折叠 → 取前 N 个」：

| 机制 | 说明 |
| --- | --- |
| **强/弱两档特征** | 强特征（`save*.sav`、`*.rpgsave`、`savedata*`…）出现 1 个就算存档目录；弱特征（`config*.dat`、`system*.dat`…）要出现 2 个以上才算 —— 所以游戏自己的 `data\config.dat` 不会被误判 |
| **补丁/备份目录排除** | 目录名像 `patch`、`汉化补丁`、`backup`、`备份`、`crack`、`cache`… 的**直接排除**（中英日文都覆盖） |
| **父子折叠** | `save\` 和 `save\2024存档\` 同时命中时只保留 `save\`；反过来，如果安装目录本身和它下面的具体存档目录同时命中，会丢掉安装目录、只备份存档子目录 |
| **打分排序** | 常见存档目录名、Steam 云存档、**游戏自己配置文件里写明的路径**、最近改动过、离安装目录近 —— 分数更高；两年多没动过的会扣分 |
| **数量上限** | 默认每个游戏最多自动选中 **5** 个（设置 → 扫描 可改）；被舍弃的候选会写进候选窗口的「详情」里备查，需要时用「指定存档目录…」手动补 |
| **读游戏自己的配置文件** | 在安装目录（2 层内、≤512 KB）的 `.ini/.cfg/.txt/.json/.xml/.dat` 里找明文路径 —— **这是精度最高的一类线索，因为是游戏自己说的** |
| **名字匹配按“词”来** | 以前是无脑子串匹配：安装目录叫 `PC` 时，`%LOCALAPPDATA%` 下的 `TpcdMetadata`、`XiaomiPCManager`、`Clipchamp`、`LockApp` 全都因为含 "pc" 被当成存档目录。现在：完全同名才算短名匹配；纯 ASCII 名必须 ≥4 字符且按词匹配；含中日文才允许子串匹配 |
| **中文标点归一化** | `樱花，萌放` 与 `樱花萌放` 视为同一个名字（全角逗号/顿号/书名号等都会剥掉） |
| **补丁目录也排除在游戏发现之外** | 不只是存档路径 —— `免DVD补丁备份`、`官方修正补丁备份` 这类目录里即使有 exe 也不会被当成“游戏”。但 `樱花，萌放 v1.08(已打补丁).7z\` 这种**正经游戏目录不会被误排除**（先剥掉括号说明/版本号/压缩包后缀再判断） |

#### 准入闸门（pathgate）—— 防止“猜错一条路径 = 写出十几个 GB”

检测阶段本质上是**猜**。只要有一条猜错，代价可能就是
把 `%LOCALAPPDATA%\Temp\PCL` 或 `%USERPROFILE%\Downloads` 整个复制进备份。
所以新增了一层**结构化硬规则**闸门（`galguard/pathgate.py`），
所有候选路径在**入选**与**真正备份**之前都要过一遍：

| 规则 | 拦什么 |
| --- | --- |
| 等于环境根 | `%USERPROFILE%` / `%LOCALAPPDATA%` / `%APPDATA%` / `%TEMP%` / `%ProgramFiles%` … 本身 |
| 用户目录下的一级位置 | `Downloads` / `Desktop` / `OneDrive` / `Videos` / `Pictures` … |
| `%APPDATA%` / `%LOCALAPPDATA%` 下的系统目录 | `Temp` / `Packages` / `Microsoft` / `Programs` / `CrashDumps` / `pip` / `npm-cache` … |
| 任意一级目录名含缓存特征 | `cache` / `gpucache` / `ebwebview` / `code cache` / `logs` / `downloads` … |
| **数据根 / 备份根本身及其祖先、后代** | 递归自备份（两个方向都拦） |
| **体量熔断** | 文件数 > 3000 或 > 500 MB → 「需人工确认」，**默认不备份** |
| **存档证据** | 用户数据目录下**真实存在却没有一个存档特征文件**的目录 → 拒绝 |

> `deny_rel_roots` **故意不含 `Documents` 与 `Saved Games`** ——
> 它们本身就是大量 galgame 的合法存档根（`Saved Games\<游戏>`、
> `Documents\My Games\<游戏>`）；整目录被误用的情况由体量熔断兜住。
>
> 闸门不含任何具体用户名、盘符、第三方软件名，全部用环境变量与结构表达，
> 换台机器判定不变。

另外还有**独占文件熔断**：同一条存档路径上**连续**出现 20 次 `PermissionError`
（多半是被进程独占的浏览器/应用缓存），就判定它不是存档目录，
中止该路径并**回滚**已经复制进去的内容。
被闸门拦下的路径会在备份结束后弹窗列出明细，并写进运行日志。

检测阶段的候选收集用的是闸门的 `quick=True` 模式（只做路径形状判断，
不数文件），所以扫描速度不受影响。

`selftest.py` 的 `[15] [16] [17] [18]` 四节会用一组构造好的目录逐个验证这些规则，
其中 `[18]` 完整复刻了交接任务书里的 15 条必拒路径、10 条必放路径、5 条跨机器用例，
以及 6 条“伪造存档路径”。

**已经加进列表的游戏，存档路径识别错了怎么办？**

选中它 → 「编辑」→ 点 **「重新识别（替换）」**，会用当前逻辑重推一遍并替换存档路径。

> 注意：旧的错误路径是老逻辑存进 `config.json` 的，**「扫描存档更新」不会自动改已存在的条目**，
> 所以要手动点一下这个按钮（或者「删除」后重新从候选里加）。

### 游戏列表的多选与批量操作

* 最左边新增 **☑ 列**：**直接点方框就能勾选**，可以勾任意多个，不需要按住 Ctrl。
* 也可以按传统方式 `Ctrl / Shift + 单击` 多选；按**空格**切换当前行勾选。
* 点**表头那个 ☑** = 全选 / 全不选；工具栏还有「全选 / 全不选 / 反选」按钮。
* 底部状态栏实时显示「已勾选 N / 总数」。
* 需要多选的操作都会作用于**勾选的**游戏；一个都没勾时自动回退到**高亮选中**的，两种习惯都能用：
  * 「备份选中的游戏」「删除」「复制存档路径」支持多个（复制会把每个游戏的名字作为注释行一起复制）；
  * 「编辑」「打开存档目录 / 备份目录」一次只作用于一个，多选时会明确提示。
* 工具栏和「操作」菜单都新增了 **「备份所有游戏」**：不比较更新，直接给列表里每个游戏各生成一份独立快照。
  点击前会弹确认框，写明会备份多少个游戏、多少个存档路径，以及有几个游戏因为没记录存档路径会被跳过。

### 针对“自己下的资源”做过的强化

自下资源（汉化版 / 绿色版 / 合集 / 免安装版）的目录结构千奇百怪，是漏检的重灾区。
以下几类结构现在都能正确识别（`selftest.py` 的 `[12]` 一节逐个验证）：

| 结构 | 例子 | 处理方式 |
| --- | --- | --- |
| 合集 / 系列目录 | `D:\GAL\汉化gal合集\游戏A\游戏A.exe`、`…\游戏B\游戏B.exe` | 父目录含 exe **不再阻止**继续下沉，会继续找出各自含 exe 的子目录 |
| exe 藏在通用名目录里 | `…\游戏B\bin\游戏B.exe`、`…\游戏D\GameData\游戏D.exe` | 游戏名自动往上找一层，不再出现叫 `bin`、`GameData` 的候选 |
| 只有启动器 exe | `…\游戏C\launcher.exe`、`启动游戏.exe` | 即使命中排除规则也**兜底**取最大的 exe，不再整款漏掉 |
| 库根目录直接指向单个游戏 | 把“游戏库根目录”设成 `D:\GAL\CLANNAD` | 根目录自身含 exe 时同样会被产出 |
| 存档就在安装目录里 | `…\游戏E\save01.dat`、`nscript.dat` | 安装目录本身被当作存档目录 |

再加两条找存档的捷径：

* **存档嗅探** —— 不依赖引擎识别，直接在安装目录及其子目录、安装目录旁边的目录、
  文档 / AppData / LocalLow 里搜“长得像存档的文件”（`save*.dat`、`*.sav`、`savedata*`…）。
* **注册表存档路径** —— 不少日式引擎把存档目录写在 `HKCU\Software\<厂商>\<游戏>` 里，
  程序会找同名键并读出其中的路径（只读）。

> **最实用的一招**：先在游戏里**存一次档**，再点「自动猜存档目录」。
> 刚被写过的目录时间最新，命中率会高很多。
> 也可以在「设置 → 扫描 → 额外搜最近 N 天内改过的存档目录」里把它设成 7~30 天。

### 扫描完看不到东西？看「扫描诊断」

扫描结束后，日志里（以及“一个候选都没有”时的弹窗里）会给出诊断，例如：

```
遍历 1234 个目录，其中 37 个含 exe；共 37 个候选，耗时 4.2 秒

· D:\GAL
    存在=True　遍历目录=1200　含 exe 的目录=37　产出候选=37
· E:\Galgame
    存在=False
    （路径不存在，请检查是否写错，或对应硬盘/移动盘没有挂载。）
```

最常见的坑：**把「游戏库根目录」指到了某一个游戏里面**，而不是
“装着一堆游戏文件夹”的那一层。诊断会直接提示这一点。
首启向导里如果没添加任何扫描范围，也会先提醒一次再让你继续。

### 系统发现的候选为什么默认不勾选？

注册表 / 快捷方式里混着大量非游戏软件，所以做了这些约束：

* 名称命中常见软件关键词（SDK / 工具 / 加速器 / 网盘 / 模拟器 / 音乐…）的直接过滤；
* 必须“像游戏目录”（有引擎特征 / 有存档目录名 / 有 ≥20 MB 的资源文件）才进候选；
* **已经找到存档路径的默认勾选，没找到的默认不勾选**；
* 候选窗口有 **「只看已找到存档的」** 勾选框和 **来源列**
  （Steam / 游戏库 / 注册表 / 快捷方式 / 最近使用）；
  「全选」只作用于**当前筛选出来的行**，不会误选看不见的东西。

### 仍然可能漏检的情况

1. **随机/哈希命名的目录认不出来。**
   有些游戏把存档放在 `%LOCALAPPDATA%\a3f9c1…\` 这类目录，无法与游戏名建立关联。
2. **存档在注册表或加密容器里。**
   少数老引擎和商业引擎把进度写在注册表或专有容器中。本工具**只读注册表**、不解析容器，
   因此这类游戏只能手动备份（可以手动把存档目录指向导出后的位置）。
3. **汉化 / 破解 / 绿色版目录结构千奇百怪。**
   它们常会删掉引擎标志文件、重命名 exe，导致引擎识别失败。
4. **Steam 库依赖 `appmanifest_*.acf`。**
   移动过库、手动删过 acf、或用第三方工具装的游戏可能扫不到。
5. **同名游戏容易混淆。**
   当 `文档\` 或 `AppData\` 下存在同名目录时，可能命中到无关目录（例如同名的截图文件夹）。
   → 请在候选确认窗口里看清楚“判定依据”，必要时手动改。
6. **多存档路径的游戏可能只识别出一部分。**
   有些游戏同时在安装目录和 `AppData` 下写存档，或不同章节用不同目录。
7. **不扫全盘。**
   这是刻意的：只扫你指定的游戏库根目录 + Steam 库 + 常见用户存档位置，避免卡顿和越权。
8. **不会去猜“哪个才是最新存档”。**
   它只做快照，不做同步、不做冲突合并。

### 兜底方案

* **候选窗口里可以直接改**：双击游戏名那一列就能重命名；选中一行点「指定存档目录…」
  可以直接补一个存档路径；顶部有筛选框和「只看已找到存档的」，候选多了也能快速找到目标。
* 「添加游戏」手动指定：游戏名、exe、安装目录、**任意多个存档路径**、独立备份根目录、备注。
  **引擎不用你填** —— 程序会从安装目录的特征文件里自己识别（识别不出来也不影响使用，
  关键是「存档路径」填对）。这些信息存进 `<数据根目录>\config.json`，重启后仍在。
* **「自动猜存档目录」**：填好安装目录后点一下，程序会把嗅探到的候选目录全部填进存档路径框。
* 自动检测失败时，可以先在游戏里存一次档，看游戏目录/文档下哪个文件夹的修改时间变了，
  把这个文件夹填进「存档路径」。
* 如果某类结构总是识别不到，把它的特征写进
  [`detect_rules.json`](#11-自定义检测规则)（例如往 `generic_dir_names` 加一条、
  往 `save_file_globs` 加一条），就能一次性覆盖所有同结构的游戏。

* 也可以在「设置 → 扫描 → 额外存档目录」里挂上全局的额外扫描目录。

---

## 13. 常见问题 FAQ

**Q1. 会不会拖慢游戏？**
不会。监控只挂在**存档目录**上。有 `watchdog` 时用的是 Windows 内核的目录变更通知（CPU 占用接近 0）；
没有 `watchdog` 时是每 5 秒扫一次存档目录（只 stat 文件名/大小/时间，不读内容）。
扫描和备份都在后台线程，界面不会卡。

**Q2. 会不会修改我的存档？**
不会。源存档只以只读方式打开（哈希时 `open(..., "rb")`，复制时 `shutil.copy2` 从源读到目标）。
代码里不存在任何对游戏目录/存档目录的写、删操作。

**Q3. 大存档会不会很慢？**
首次建立基线时如果开了 SHA256，会读一遍所有存档文件。可以在
「设置 → 扫描」里关掉 SHA256，或把「超过该大小的文件不计算哈希」调小（默认 64 MB）。

**Q4. 为什么“是否有更新”一直是 `—`？**
`—` 表示还没做过对比。点一次「扫描存档更新」，或者重启程序（启动后会自动对比一次）。
如果刚加入游戏，需要等基线建立完成。

**Q5. 备份占空间太大怎么办？**
「设置 → 保留策略」可选「每个游戏只保留最近 N 份」或「只保留最近 N 天」。
默认是**不自动删除**，更安全。

**Q6. 能备份到 NAS / 网盘同步目录吗？**
可以，只要它是盘符路径（如 `Z:\Backups`）。但请注意同步盘的文件锁可能让复制失败 ——
本程序会自动重试 3 次，失败会记进日志和 `manifest.json` 的 `skipped_files`。

**Q7. 杀毒软件报警？**
因为要用 `Shell_NotifyIcon` 建托盘、要遍历目录，某些启发式引擎会误报。
程序不联网、不写系统目录、不注册服务，可自行查看源码或用 ProcMon 验证（见第 9 节）。
打包后如果仍报警，请把 exe 加入白名单，或直接源码运行。

**Q8. 我只想临时用一次，不想留任何东西？**
不要勾选「记住这个位置」，把数据根目录设为临时文件夹，用完直接删掉该文件夹即可。

**Q9. 能不能同时开两个实例？**
不能。第二个实例会检测到 `<数据根目录>\cache\galguard.lock` 被占用并提示你
「已经有一个实例在运行」。这是为了避免两份程序同时往同一个备份目录写。

**Q10. 换电脑怎么迁移？**
整个复制「总数据根目录」，然后用 `--data-root` 指过去。
注意 `config.json` 里的游戏路径是绝对路径，换机器后可能需要用「编辑」重新指向。

**Q11. 关闭后托盘图标还在？**
如果你点的是 X 且设置是「最小化到托盘」，图标本来就该在（程序还在运行）。
要让它消失，用托盘菜单或主界面的「彻底退出」。
如果点了彻底退出图标仍在：Windows 托盘偶尔有刷新延迟，把鼠标移到图标上或打开一次任务栏设置即可刷新；
同时用 `Get-Process` 确认进程确实已经退出。

**Q12. 日志在哪？**
`<总数据根目录>\logs\app.log`（滚动，2MB × 5），崩溃信息在 `crash.log`。
主界面底部也有最近 500 条日志。

**Q13. 托盘图标没出现 / 点 X 之后窗口不见了？**
程序会**主动校验**托盘图标是否真的显示成功（`pystray` 有一个坑：不在 `setup`
回调里设 `visible = True`，图标永远不会出现；另外受限会话下
`ChangeWindowMessageFilterEx` 会抛 `WinError 5` 让托盘线程直接崩溃，程序已内置兼容处理）。

如果校验失败，程序会记一条日志并按**无托盘模式**运行：
此时点右上角 X 不会静默隐藏窗口，而是**明确问你要不要彻底退出**，
不会让你把窗口藏到一个点不到的地方。

排查方法：
1. 看 `<数据根目录>\logs\app.log` 里有没有“系统托盘图标已创建并显示”。
2. 看 `crash.log` 里有没有 `galguard-tray` 线程的异常。
3. 确认 `pip install pystray Pillow` 成功；或干脆用 `--no-tray` 参数明确走无托盘模式。

**Q14. 打包后的 exe 需要 Python 环境吗？**
不需要。`--onedir` 产物里已经带上了 Python 解释器和全部依赖，
拷到别的 Windows 11 机器上直接双击即可运行（整包约 24 MB）。
但必须**整个 `dist\GalgameSaveGuard` 文件夹一起拷**，不能只拷 exe。

**Q15. 双击 exe 没反应，怎么办？**
因为打包用的是 `--windowed`（不带黑框控制台），一旦启动阶段出错，
错误信息默认是**看不见**的。请按顺序排查：

1. **确认是整包拷贝。** 只拷 `GalgameSaveGuard.exe` 是不行的，
   必须连同同目录的 `_internal\` 文件夹一起。
   只拷 exe 的现象通常是一个“Failed to load Python DLL”的错误框。
2. **看有没有弹错误框。** 程序在启动失败时会用原生 MessageBox 显示完整 traceback，
   并提示写到 `<总数据根目录>\logs\crash.log`。如果连框都没有，继续往下看。
3. **检查杀毒软件。** 360 / 火绒 / 电脑管家对“新编译、无签名”的 PyInstaller exe
   非常敏感，经常**静默拦截**，表现就是双击完全没反应。
   请查看杀软的拦截记录，或把整个 `dist\GalgameSaveGuard` 目录加白名单。
4. **确认进程是否真的没起来。** 打开任务管理器搜 `GalgameSaveGuard`；
   或在 PowerShell 里执行：
   ```powershell
   Get-Process GalgameSaveGuard | Select-Object Id, MainWindowTitle
   ```
   如果有进程但没有窗口标题，说明它可能已经在托盘里（看右下角 ^ 里有没有图标）。
5. **看日志。** 如果之前成功运行过，看 `<总数据根目录>\logs\app.log` 与 `crash.log`。
6. **用源码运行对比。** 源码能跑、exe 不能跑 → 大概率是杀软或打包环境问题：
   ```powershell
   python -B run.py --data-root "D:\GalgameGuard" --console
   ```
7. **干脆跳过托盘**试试（极少数受限环境里创建托盘会失败）：
   ```powershell
   .\GalgameSaveGuard.exe --no-tray
   ```

> 排查提示：本项目已内置防呆——任何**启动期致命错误**都会用原生 MessageBox 弹出来，
> 不会静默退出。如果你看到的是真正的“完全无反应”，那基本可以确定
> 进程在进入 Python 之前就被系统/杀软拦掉了。

---

## 14. 打包成 exe（PyInstaller --onedir）

### 为什么必须用 `--onedir` 而不是 `--onefile`

`--onefile` 打包出来的 exe 每次启动都会把内容**解压到 `%TEMP%\_MEIxxxxxx\`**，
这直接违反本项目的零污染承诺（会写 `%TEMP%`），而且启动更慢、更容易被杀软误判。
`--onedir` 是解压到**程序自己的目录**，不碰 `%TEMP%`。

### 一键脚本

```powershell
# 在项目根目录执行（脚本会自动优先使用项目内的 .venv）
powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1

# 指定解释器
powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1 -PythonExe .\.venv\Scripts\python.exe

# 想要更小的产物（不要托盘 / 不要 watchdog，功能会自动降级）
powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1 -NoTray -NoWatchdog
```

### 推荐做法：先建一个项目内的虚拟环境

这样不会污染全局 Python，脚本也会自动识别：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
# 国内网络如果卡住，加镜像：
#   -i https://pypi.tuna.tsinghua.edu.cn/simple
powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1
```

实测结果（Python 3.11.9 / PyInstaller 6.22.3 / Windows 11）：

```
产物目录  : dist\GalgameSaveGuard
可执行文件: dist\GalgameSaveGuard\GalgameSaveGuard.exe
整包大小  : 24.0 MB
```

打包后实测：托盘图标正常显示、日志正常写入 `<数据根目录>\logs\app.log`、
`%TEMP%` 下**没有** `_MEI*` 解压目录、exe 所在目录**没有**被写入任何文件
（未勾选「记住路径」时），进程被结束后无残留。

### 手工命令（等价）

```powershell
pip install pyinstaller

pyinstaller --noconfirm --clean --onedir --windowed `
  --name GalgameSaveGuard `
  --distpath dist --workpath build --specpath build `
  --exclude-module PyQt5 --exclude-module PyQt6 `
  --exclude-module PySide2 --exclude-module PySide6 `
  --exclude-module numpy --exclude-module pandas `
  --exclude-module matplotlib --exclude-module scipy `
  --exclude-module pytest --exclude-module IPython `
  --exclude-module setuptools --exclude-module pip `
  run.py
```

产物：`dist\GalgameSaveGuard\GalgameSaveGuard.exe`（连同同目录的 DLL 一起分发）。
**首次运行前请确保 `watchdog` / `pystray` / `Pillow` 已安装**，否则打包产物会自动降级
（无 watchdog → 轮询；无 pystray → 无托盘），功能仍可用。

### 打成可分发的独立 zip

```powershell
# 先打包，再压成 zip
powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1
powershell -ExecutionPolicy Bypass -File .\make_zip.ps1

# 一步到位：压好并直接复制到下载目录
powershell -ExecutionPolicy Bypass -File .\make_zip.ps1 -CopyTo "$env:USERPROFILE\Downloads"
```

产物：`GalgameSaveGuard-v1.0.0-win64.zip`（约 11.3 MB），解压后结构：

```
GalgameSaveGuard\
├── GalgameSaveGuard.exe
├── _internal\                  （Python 运行时与全部依赖）
└── 使用说明.txt
```

> `make_zip.ps1` 刻意没有用 `Compress-Archive` / `ZipFile.CreateFromDirectory`：
> 它们在 Windows 下会把条目名写成反斜杠（`GalgameSaveGuard\a.exe`），
> 不符合 ZIP 规范，部分解压工具会解出名字里带反斜杠的怪文件。
> 脚本改为手工写入正斜杠条目名，已实测解压后目录结构正确。

### 产物体积是怎么压下来的

未做处理时整包是 **40.3 MB**，其中 **99% 不是我写的代码**（我的源码只有 430 KB）。
`build_onedir.ps1` 里的一组 `--exclude-module` 把它压到 **24 MB / zip 11.3 MB**：

| 排除项 | 省下 | 依据 |
| --- | --- | --- |
| `PIL._avif` | 7.52 MB | 只用 `ImageDraw` 画一个 64×64 托盘图标，永远用不到 AVIF 解码 |
| `PIL._imagingft` | 2.07 MB | 图标只画矩形，不渲染任何文字（FreeType 用不上） |
| `PIL._webp` / `PIL._imagingcms` | 0.66 MB | 同上 |
| `libcrypto-3.dll` + `libssl-3.dll` + `_ssl.pyd` | 5.87 MB | 本程序**从不联网**（Ludusavi 是离线文件，Steam 只读本地 vdf） |
| `_hashlib` | — | 哈希退回到 CPython 内建的 `_sha256`/`_md5`，结果完全一致 |
| 一批未用标准库 | ~1 MB | `unittest` / `asyncio` / `multiprocessing` / `smtplib` … |

> `_hashlib` 被排除后，`hashlib.sha256()` 会走 CPython 自带的 `_sha256` 内建模块
> （编译进 `python311.dll`，不受打包影响）。本项目**实测验证过**：
> 在施加了相同排除项的冻结环境里，`sha256('hello')` 与标准值一致，
> md5/sha1/sha512/hmac/sqlite3/tkinter/PIL 画图全部正常。

**如果你要改回去**：把 `build_onedir.ps1` 里对应的 `--exclude-module` 行注释掉重新打包即可。
需要联网功能的场景（例如自己加一个"检查更新"）必须恢复 `ssl` / `_ssl` / `_hashlib`。

### 打包注意事项

* `--windowed` 表示不带控制台窗口；日志仍然写在数据根目录里。
* 打包后请**不要**把 exe 放在游戏目录里 —— 指针文件（如果你勾选了记住路径）会写在 exe 旁边。
* 打包产物目录里不会生成 `__pycache__`（字节码在 `--onedir` 的内部归档中）。
* 如果确实需要 `--onefile`，请务必在文档里明确告知使用者：它会向 `%TEMP%` 解压临时文件，
  与零污染目标冲突，**本项目不推荐**。

---

## 15. 卸载

1. 用程序的「彻底退出」结束进程（确认托盘图标消失、任务管理器里没有残留）。
2. 删除「总数据根目录」和「备份根目录」。
3. 如果当初勾选了「记住这个位置」，删除启动器旁的 `galguard.root.json`。
4. （仅打包版）删除 `dist\GalgameSaveGuard\` 目录。

除此之外系统里**没有任何残留**：无服务、无计划任务、无开机自启、无注册表键、无 AppData 目录。

---

## 16. 验收自检

项目自带一个无界面自检脚本，会在一分钟内跑完全链路并自动清理测试目录：

```powershell
python -B selftest.py
```

它验证 97 项断言，覆盖：

| 分组 | 内容 |
| --- | --- |
| [1] 路径沙盒 | `%TEMP%`/`%APPDATA%`/程序目录写入被拦截；未解锁时拒绝一切写入 |
| [2] 临时目录 | `tempfile.gettempdir()` 与 `TEMP/TMP` 均指向 `<数据根目录>\temp` |
| [3] 配置 | `config.json` 落盘并可回读 |
| [4] 扫描 | 识别出 Ren'Py 候选游戏与 `<安装目录>\save` |
| [5] 备份 | 基线 / 新增 / 修改 / 删除对比；快照结构；`manifest.json`；`原始存档路径.txt`；文件完整复制；备份后基线刷新 |
| [6] 保留策略 | 「最近 1 份」确实只留 1 份 |
| [7] 监控 | 轮询后端能捕获存档变化并触发回调，停止后线程全灭 |
| [8] 任务池 | 守护线程任务可执行、可回调，停止后无存活线程 |
| [9] 界面 | 主窗口 + 服务层可正常装配 |
| [10] 托盘 | 托盘图标**真正显示**（`visible=True`）并可销毁 |
| [11] 对话框 | 首启向导 / 手动添加 / 设置 / 候选确认**都能成功构造**（防止 `TclError` 这类只在运行时才暴露、窗口模式下又看不见的 bug） |
| [12] 自下资源 | 合集目录、`bin\` 里的 exe、只有 `launcher.exe`、`GameData\` 里的 exe、存档就在安装目录 —— 5 种结构逐个验证能识别且游戏名正确 |
| [13] 系统发现 | 「像不像游戏目录」判定；Ludusavi manifest 解析、`<base>` 占位符展开、`when` 条件块不被误认为路径、中文游戏名；注册表只读可读；坏快捷方式不崩 |

实测输出（Python 3.11.9 / Windows 11）：

```
通过 97 项，失败 0 项
```

---

## 附：验收标准对照

| 验收项 | 实现位置 | 自检覆盖 |
| --- | --- | --- |
| 第一次启动能扫描并列出候选游戏，确认后建立基线 | `scanner.py` / `ui_dialogs.CandidateDialog` / `service.add_candidates` | [4] [5] |
| 程序运行期间，存档更新后自动生成独立快照备份 | `monitor.py` → `service._on_fs_change` → `backup.create_snapshot` | [7] [5] |
| 每个备份都有 `manifest.json` 与 `原始存档路径.txt` | `backup.py` | [5] |
| 主动「扫描存档更新」刷新更新标记 | `service.refresh_updates` | [5] |
| 主动「备份有更新的游戏」批量备份 | `service.backup_updated` | [5] |
| 关闭主窗口 ≠ 彻底退出 | `ui_main.on_close_button` / `app.quit` | — |
| 彻底退出后无残留进程 / 无托盘 / 无服务 / 无计划任务 | `app.GalguardApp.quit` + `tasks.DaemonPool`（守护线程） | [8] |
| 不在授权目录之外写文件 | `paths.PathGuard` + `bootstrap.redirect_temp_dir` | [1] [2] |
| [17] 禁区 | 存档路径落在备份目录内时不复制任何文件；存档目录里套着备份目录时只备份真存档；把备份目录当游戏库会被拒绝；备份根落在游戏库内会警告 |
