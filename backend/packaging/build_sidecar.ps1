# 打包 KeRui Recruit 本地 sidecar 为独立可执行文件。
# 产物输出到仓库根 dist/kerui-recruit-sidecar.exe，供 Tauri 桌面壳在运行时定位。
#
# 用法（在仓库根的 worktree 目录下执行）：
#   powershell -ExecutionPolicy Bypass -File backend/packaging/build_sidecar.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "未找到虚拟环境解释器：$Python"
}

Push-Location $Root
try {
    & $Python -m PyInstaller backend/packaging/kerui_recruit.spec --noconfirm
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出码 $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Exe = Join-Path $Root "dist\kerui-recruit-sidecar.exe"
if (-not (Test-Path $Exe)) {
    throw "打包产物缺失：$Exe"
}

$BundleDirectory = Join-Path $Root "desktop\src-tauri\binaries"
New-Item -ItemType Directory -Path $BundleDirectory -Force | Out-Null
Copy-Item -LiteralPath $Exe -Destination (Join-Path $BundleDirectory "kerui-recruit-sidecar.exe") -Force

Write-Host "sidecar 打包完成：$Exe"
