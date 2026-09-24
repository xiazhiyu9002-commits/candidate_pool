[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [string]$PreviousInstallerPath,
    [string]$EvidencePath,
    [switch]$RunInstallCycle
)

$ErrorActionPreference = 'Stop'

function Resolve-Installer([string]$Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if ([IO.Path]::GetExtension($resolved) -ne '.exe') {
        throw "Installer must be an .exe file"
    }
    $item = Get-Item -LiteralPath $resolved
    if ($item.Length -lt 50MB) {
        throw "Installer is unexpectedly small: $($item.Length) bytes"
    }
    return $item
}

function Get-InstallerEvidence([IO.FileInfo]$Installer) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Installer.FullName
    $hash = Get-FileHash -LiteralPath $Installer.FullName -Algorithm SHA256
    return [ordered]@{
        filename = $Installer.Name
        size_bytes = $Installer.Length
        sha256 = $hash.Hash.ToLowerInvariant()
        signature_status = [string]$signature.Status
        signer_subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { $null }
        inspected_at_utc = [DateTime]::UtcNow.ToString('o')
    }
}

function Invoke-SilentInstaller([IO.FileInfo]$Installer, [string]$InstallRoot) {
    $process = Start-Process -FilePath $Installer.FullName `
        -ArgumentList @('/S', "/D=$InstallRoot") `
        -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Installer exited with code $($process.ExitCode)"
    }
}

function Start-IsolatedApplication([string]$Executable, [string]$LocalData, [string]$RoamingData) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $start.Environment['LOCALAPPDATA'] = $LocalData
    $start.Environment['APPDATA'] = $RoamingData
    return [Diagnostics.Process]::Start($start)
}

function Invoke-IsolatedInstallCycle([IO.FileInfo]$Current, [IO.FileInfo]$Previous) {
    $testRoot = Join-Path ([IO.Path]::GetTempPath()) ("kerui-release-" + [guid]::NewGuid().ToString('N'))
    $installRoot = Join-Path $testRoot 'app'
    $localData = Join-Path $testRoot 'local'
    $roamingData = Join-Path $testRoot 'roaming'
    New-Item -ItemType Directory -Path $installRoot, $localData, $roamingData -Force | Out-Null

    if ($Previous) {
        Invoke-SilentInstaller $Previous $installRoot
    }
    Invoke-SilentInstaller $Current $installRoot

    # 应用主程序名跟着 `productName` 走（当前是 `recruit`，安装包名 `recruit_<版本>_x64-setup.exe`），
    # 而这里原来写死了旧名 `kerui-recruit-desktop.exe` —— 改名后冒烟会误报「找不到可执行文件」。
    # 先按候选名找，找不到就在安装根目录里挑体积最大的非卸载器 exe（壳层只有一个主程序）。
    $candidates = @('kerui-recruit-desktop.exe', 'recruit.exe')
    $application = $null
    foreach ($name in $candidates) {
        $candidatePath = Join-Path $installRoot $name
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
            $application = $candidatePath
            break
        }
    }
    if (-not $application) {
        $application = Get-ChildItem -LiteralPath $installRoot -Filter *.exe -File |
            Where-Object { $_.Name -ne 'uninstall.exe' } |
            Sort-Object Length -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $application) {
        throw "Installed application executable was not found"
    }

    $process = Start-IsolatedApplication $application $localData $roamingData
    # **Windows 是便携式布局**：数据跟着安装目录走，落在 `<exe 目录>\data`
    # （见 `src-tauri/src/lib.rs:default_data_root`），不是 `%LOCALAPPDATA%\KeRuiRecruit`
    # ——后者只是旧版的迁移来源（`legacy_data_root`），只会造成「找不到数据库」的误报。
    $dataRoot = Join-Path $installRoot 'data'
    try {
        $database = Join-Path $dataRoot 'db\recruit.sqlite3'
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-Path -LiteralPath $database) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Path -LiteralPath $database)) {
            throw "Application did not initialize its portable database at $database"
        }
    }
    finally {
        if ($process -and -not $process.HasExited) {
            # `Process.Kill($true)`（连子进程树一起杀）只在 .NET Core / PowerShell 7+ 存在，
            # Windows PowerShell 5.1 上会直接报 "Cannot find an overload for Kill and the
            # argument count: 1"。而 sidecar 是壳层的子进程，只杀壳层会留下孤儿服务——
            # 所以改用两种宿主都能用的 `taskkill /T`（与 Rust 侧 terminate_sidecar 同一口径）。
            & taskkill /T /F /PID $process.Id 2>$null | Out-Null
            $process.WaitForExit()
        }
    }

    $uninstaller = Join-Path $installRoot 'uninstall.exe'
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "Uninstaller was not found"
    }
    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList '/S' -PassThru -Wait -WindowStyle Hidden
    if ($uninstall.ExitCode -ne 0) {
        throw "Uninstaller exited with code $($uninstall.ExitCode)"
    }
    # 卸载不得删用户数据。便携式布局下数据就在安装目录内，所以要**实测**它是否还在——
    # 这正是「NSIS 卸载会不会连 `data` 一起删掉」这个问题的答案，不能靠假设。
    $survived = (Test-Path -LiteralPath (Join-Path $dataRoot 'db\recruit.sqlite3') -PathType Leaf) -or
                (Test-Path -LiteralPath (Join-Path $localData 'KeRuiRecruit') -PathType Container)
    if (-not $survived) {
        throw "Uninstall removed the user data directory (checked $dataRoot and the legacy LOCALAPPDATA root)"
    }

    return [ordered]@{
        ok = $true
        upgraded_from_previous = [bool]$Previous
        launch_initialized_database = $true
        uninstall_retained_data = $true
        data_root = $dataRoot
        isolated_test_root = $testRoot
    }
}

$current = Resolve-Installer $InstallerPath
$previous = if ($PreviousInstallerPath) { Resolve-Installer $PreviousInstallerPath } else { $null }
$evidence = [ordered]@{
    installer = Get-InstallerEvidence $current
    install_cycle = $null
}

if ($RunInstallCycle) {
    $evidence.install_cycle = Invoke-IsolatedInstallCycle $current $previous
}

$json = $evidence | ConvertTo-Json -Depth 6
if ($EvidencePath) {
    $parent = Split-Path -Parent $EvidencePath
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Set-Content -LiteralPath $EvidencePath -Value $json -Encoding utf8
}
$json
