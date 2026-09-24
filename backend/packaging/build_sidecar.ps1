# 打包 KeRui Recruit 本地 sidecar，并放进 Tauri 壳的资源目录。
#
# 产物形态是 **onedir**（`kerui_recruit.spec` 用 COLLECT）：`dist\kerui-recruit-sidecar\` 里
# 是「同名可执行文件 + `_internal\`」。Tauri 壳的解析器（`desktop/src-tauri/src/lib.rs`
# 的 `sidecar_candidates`）专门有一条 onedir 分支：
#
#     resource_dir\kerui-recruit-sidecar.exe\kerui-recruit-sidecar.exe
#
# 所以这里必须把**整个目录**拷成 `binaries\kerui-recruit-sidecar.exe\`（同名目录），
# 而不是拷单个文件——单文件与 onedir 混用会得到一个「名字像可执行文件、实际是目录」的
# 资源项，壳层启动 sidecar 时直接失败，而且安装包看着是正常的。
#
# 用法（在仓库根目录执行）：
#   powershell -ExecutionPolicy Bypass -File backend/packaging/build_sidecar.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Spec = "backend/packaging/kerui_recruit.spec"

Push-Location $Root
try {
    # 优先用仓库虚拟环境；没有就退回系统解释器（本机与 CI 两种环境都能跑）。
    if (Test-Path $VenvPython) {
        & $VenvPython -m PyInstaller $Spec --noconfirm
    } else {
        & py -3.12 -m PyInstaller $Spec --noconfirm
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出码 $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$Bundle = Join-Path $Root "dist\kerui-recruit-sidecar"
$Exe = Join-Path $Bundle "kerui-recruit-sidecar.exe"
if (-not (Test-Path $Exe -PathType Leaf)) {
    throw "打包产物缺失：$Exe"
}

$BundleDirectory = Join-Path $Root "desktop\src-tauri\binaries"
New-Item -ItemType Directory -Path $BundleDirectory -Force | Out-Null
$Target = Join-Path $BundleDirectory "kerui-recruit-sidecar.exe"
# 目标必须是「同名目录」；先清掉上一轮残留，否则 Copy-Item 会把整个目录塞进旧目录里。
if (Test-Path $Target) {
    Remove-Item -LiteralPath $Target -Recurse -Force
}
Copy-Item -LiteralPath $Bundle -Destination $Target -Recurse -Force
if (-not (Test-Path (Join-Path $Target "kerui-recruit-sidecar.exe") -PathType Leaf)) {
    throw "拷贝后未找到可执行文件：$Target\kerui-recruit-sidecar.exe"
}

Write-Host "sidecar 打包完成：$Target"
