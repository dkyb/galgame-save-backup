# =============================================================================
#  Galgame 存档守护 —— PyInstaller 打包脚本
#
#  刻意使用 --onedir，绝不使用 --onefile：
#      --onefile 每次启动都会把内容解压到 %TEMP%\_MEIxxxxxx\，
#      违反本项目“零污染 / 不写系统临时目录”的承诺，而且启动更慢。
#      --onedir 只解压到程序自己所在目录。
#
#  用法（在项目根目录执行）：
#      powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1
#      powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1 -PythonExe .\.venv\Scripts\python.exe
#      powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1 -NoTray
#      powershell -ExecutionPolicy Bypass -File .\build_onedir.ps1 -NoWatchdog
#
#  体积说明：脚本里的一组 --exclude-module 会裁掉未使用的 Pillow 插件与
#  OpenSSL（约 11 MB），产物约 26 MB。若你确实需要联网或字体渲染，
#  把对应的 exclude 行注释掉即可。
# =============================================================================

[CmdletBinding()]
param(
    [string]$Name = "GalgameSaveGuard",
    [string]$PythonExe = "",   # 留空 = 优先用项目内 .venv，其次用 PATH 里的 python
    [switch]$NoTray,           # 传 -NoTray 则不打包 pystray/Pillow（产物无托盘，其余功能不变）
    [switch]$NoWatchdog        # 传 -NoWatchdog 则不打包 watchdog（产物退化为纯轮询监控）
)

# 注意：这里刻意不设 $ErrorActionPreference = "Stop"。
# 因为 Python 往 stderr 写一行提示就会在 Windows PowerShell 5.1 下
# 被当成终止错误，导致脚本误判失败。所有失败都通过显式检查 $LASTEXITCODE 处理。
$ErrorActionPreference = "Continue"
# 让 PyInstaller 及其子进程也不写 __pycache__（子进程不继承 -B 参数，只能用环境变量）
$env:PYTHONDONTWRITEBYTECODE = "1"

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

function Write-Step([string]$text) { Write-Host "`n$text" -ForegroundColor Yellow }
function Fail([string]$text) { Write-Host "`n[错误] $text" -ForegroundColor Red; exit 1 }

Write-Host "=== Galgame 存档守护 —— onedir 打包 ===" -ForegroundColor Cyan
Write-Host "项目目录: $root"

# --- 0. 解析要用的 Python ----------------------------------------------------
if (-not $PythonExe) {
    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $PythonExe = $venvPy } else { $PythonExe = "python" }
}
if ($PythonExe -ne "python" -and -not (Test-Path $PythonExe)) {
    Fail "找不到指定的 Python：$PythonExe"
}

$ver = & $PythonExe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $ver) {
    Fail "无法运行 Python：$PythonExe`n请安装 Python 3.10+，或加 -PythonExe 指定解释器路径。"
}
Write-Host "Python 解释器: $PythonExe"
Write-Host "Python 版本  : $ver"
if ([version]$ver -lt [version]"3.10.0") { Fail "需要 Python 3.10 或更高版本，当前为 $ver。" }

# tkinter 是 GUI 的依赖，打包前必须确认可用
& $PythonExe -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "当前 Python 缺少 tkinter，无法打包。`n请改用 python.org 官方安装包（安装时勾选 tcl/tk），或指定另一个解释器。"
}
Write-Host "tkinter     : 可用"

# --- 1. 安装打包工具 ---------------------------------------------------------
Write-Step "[1/4] 安装 / 检查 PyInstaller ..."
& $PythonExe -m pip install --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 安装失败（如果是网络问题，可先设置镜像：$env:PIP_INDEX_URL = 'https://pypi.tuna.tsinghua.edu.cn/simple'）。" }

# --- 2. 可选依赖 -------------------------------------------------------------
# PyInstaller 只会打包“已安装且被导入”的模块，所以要先保证可选依赖在位。
Write-Step "[2/4] 检查可选依赖 ..."

# 包名 -> 实际 import 的模块名（Pillow 的模块名是 PIL，两者不一样！）
$wanted = @()
if (-not $NoWatchdog) { $wanted += @{ Pkg = "watchdog"; Mod = "watchdog" } }
if (-not $NoTray)     { $wanted += @{ Pkg = "pystray";  Mod = "pystray"  }
                        $wanted += @{ Pkg = "Pillow";   Mod = "PIL"      } }

foreach ($item in $wanted) {
    $found = & $PythonExe -c "import importlib.util as u; print('1' if u.find_spec('$($item.Mod)') else '0')" 2>$null
    if ($found -eq "1") {
        Write-Host "  已安装 $($item.Pkg)"
    } else {
        Write-Host "  未安装 $($item.Pkg)，正在安装 ..."
        & $PythonExe -m pip install $item.Pkg
        if ($LASTEXITCODE -ne 0) { Fail "安装 $($item.Pkg) 失败。" }
    }
}
if ($NoWatchdog) { Write-Host "  [跳过] watchdog —— 产物将使用纯轮询监控" }
if ($NoTray)     { Write-Host "  [跳过] pystray/Pillow —— 产物将没有系统托盘" }

# --- 3. 清理旧产物 -----------------------------------------------------------
Write-Step "[3/4] 清理旧产物 ..."
foreach ($d in @("build", "dist")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    Write-Host "  已清理 $d"
}

# --- 4. 打包 -----------------------------------------------------------------
Write-Step "[4/4] 开始打包（--onedir）..."

$pyArgs = @(
    "--noconfirm",
    "--clean",
    "--onedir",                 # ← 关键：绝不用 --onefile
    "--windowed",               # 不带控制台窗口
    "--name", $Name,
    "--distpath", "dist",
    "--workpath", "build",
    "--specpath", "build",
    # 排除明显用不到的大包，减小产物体积
    "--exclude-module", "PyQt5",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2",
    "--exclude-module", "PySide6",
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "matplotlib",
    "--exclude-module", "scipy",
    "--exclude-module", "pytest",
    "--exclude-module", "IPython",
    "--exclude-module", "setuptools",
    "--exclude-module", "pip",

    # ---- 体积裁剪（40 MB -> 约 26 MB，功能零影响）--------------------------
    # ① 未使用的 Pillow 插件：我们只用 ImageDraw 画一个 64x64 的托盘图标，
    #    但 PyInstaller 会把 PIL 包里所有 .pyd 插件都收进来。
    #    _avif 一个文件就 7.5 MB；_imagingft 是 FreeType 字体渲染，我们不画字。
    "--exclude-module", "PIL._avif",
    "--exclude-module", "PIL._webp",
    "--exclude-module", "PIL._imagingcms",
    "--exclude-module", "PIL._imagingft",
    "--exclude-module", "PIL.ImageQt",
    "--exclude-module", "PIL.ImageShow",
    # ② OpenSSL：本程序从不联网（Ludusavi 是离线文件，Steam 只读本地 vdf），
    #    哈希改用 CPython 内建的 _sha256/_md5（已实测结果一致）。
    #    这一项去掉 libcrypto-3.dll(4.95 MB) + libssl-3.dll(0.75 MB)。
    "--exclude-module", "ssl",
    "--exclude-module", "_ssl",
    "--exclude-module", "_hashlib",
    # ③ 用不到的标准库。
    #    注意：不能排除 urllib —— pathlib 在模块级 import 了 urllib.parse。
    "--exclude-module", "unittest",
    "--exclude-module", "doctest",
    "--exclude-module", "pydoc",
    "--exclude-module", "pdb",
    "--exclude-module", "lib2to3",
    "--exclude-module", "asyncio",
    "--exclude-module", "multiprocessing",
    "--exclude-module", "concurrent",
    "--exclude-module", "ftplib",
    "--exclude-module", "imaplib",
    "--exclude-module", "smtplib",
    "--exclude-module", "poplib",
    "--exclude-module", "telnetlib",
    "--exclude-module", "xmlrpc",
    "--exclude-module", "tkinter.test",
    "--exclude-module", "test",
    "run.py"
)

& $PythonExe -B -m PyInstaller @pyArgs   # -B：打包过程也不在源码树里生成 __pycache__
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 打包失败，请查看上面的输出。" }

$outDir = Join-Path $root "dist\$Name"
$exe = Join-Path $outDir "$Name.exe"
if (-not (Test-Path $exe)) { Fail "没有找到产物：$exe" }

$size = [math]::Round((Get-ChildItem $outDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)

Write-Host "`n=== 打包完成 ===" -ForegroundColor Green
Write-Host "产物目录  : $outDir"
Write-Host "可执行文件: $exe"
Write-Host "整包大小  : $size MB"
Write-Host ""
Write-Host "提醒：" -ForegroundColor Cyan
Write-Host "  · 分发时要整个 dist\$Name 文件夹一起拷贝，不能只拷 exe。"
Write-Host "  · 首次运行会让你选择「总数据根目录」与「备份根目录」。"
Write-Host "  · 如果勾选「记住这个位置」，指针文件 galguard.root.json 会写在 exe 旁边。"
Write-Host "  · 本产物不会开机自启、不装服务、不写计划任务、不写注册表。"
Write-Host "  · 想验证“不写其它目录”，按 README 第 9 节用 Process Monitor 过滤本 exe。"
exit 0
