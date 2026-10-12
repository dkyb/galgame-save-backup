# =============================================================================
#  Galgame 存档守护 —— 生成可分发的独立 zip 包
#
#  前提：先执行过 build_onedir.ps1，存在 dist\GalgameSaveGuard\GalgameSaveGuard.exe
#
#  用法：
#      powershell -ExecutionPolicy Bypass -File .\make_zip.ps1
#      powershell -ExecutionPolicy Bypass -File .\make_zip.ps1 -Version 1.0.1
#      powershell -ExecutionPolicy Bypass -File .\make_zip.ps1 -CopyTo "$env:USERPROFILE\Downloads"
#
#  说明：刻意不用 Compress-Archive / ZipFile.CreateFromDirectory，
#        因为它们在 Windows 上会把条目名写成反斜杠（GalgameSaveGuard\a.exe），
#        不符合 ZIP 规范，某些解压工具会解出带反斜杠的怪文件名。
#        这里手工写入正斜杠条目名。
# =============================================================================

[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [string]$Name = "GalgameSaveGuard",
    [string]$CopyTo = ""        # 例如 "$env:USERPROFILE\Downloads"，留空则只放在项目目录
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

function Fail([string]$text) { Write-Host "`n[错误] $text" -ForegroundColor Red; exit 1 }

$src = Join-Path $root "dist\$Name"
if (-not (Test-Path (Join-Path $src "$Name.exe"))) {
    Fail "找不到 dist\$Name\$Name.exe，请先运行 build_onedir.ps1。"
}
$note = Join-Path $root "packaging\使用说明.txt"
if (-not (Test-Path $note)) {
    Fail "找不到 packaging\使用说明.txt。"
}

$out = Join-Path $root "$Name-v$Version-win64.zip"
if (Test-Path $out) { Remove-Item -Force $out }

Write-Host "=== 生成独立 zip ===" -ForegroundColor Cyan
Write-Host "来源: $src"
Write-Host "输出: $out"

Add-Type -AssemblyName System.IO.Compression | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

$srcFull = (Resolve-Path $src).Path
$files = Get-ChildItem $src -Recurse -File

$fs = [System.IO.File]::Open($out, [System.IO.FileMode]::CreateNew)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)

$count = 0
try {
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($srcFull.Length + 1).Replace('\', '/')
        $entryName = "$Name/$rel"                      # ← 正斜杠，符合 ZIP 规范
        $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
        $es = $entry.Open()
        try {
            $ins = [System.IO.File]::OpenRead($f.FullName)
            try { $ins.CopyTo($es) } finally { $ins.Dispose() }
        } finally { $es.Dispose() }
        $count++
    }

    # 追加使用说明.txt
    $entryName = "$Name/使用说明.txt"
    $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $es = $entry.Open()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($note)
        $es.Write($bytes, 0, $bytes.Length)
    } finally { $es.Dispose() }
    $count++
} finally {
    $zip.Dispose()
    $fs.Dispose()
}

$sizeMB = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Green
Write-Host "压缩包  : $out"
Write-Host "条目数  : $count"
Write-Host "大小    : $sizeMB MB"
Write-Host "包内结构: $Name\  （含 $Name.exe、_internal\、使用说明.txt）"

if ($CopyTo) {
    if (-not (Test-Path $CopyTo)) { Fail "目标目录不存在：$CopyTo" }
    $dest = Join-Path $CopyTo (Split-Path $out -Leaf)
    Copy-Item -Force -Path $out -Destination $dest
    if (Test-Path $dest) {
        Write-Host "已复制到: $dest" -ForegroundColor Green
    } else {
        Fail "复制到 $CopyTo 失败（可能被沙盒或权限拦截）。"
    }
}
exit 0
