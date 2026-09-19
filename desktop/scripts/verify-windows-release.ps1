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

    $application = Join-Path $installRoot 'kerui-recruit-desktop.exe'
    if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
        throw "Installed application executable was not found"
    }

    $process = Start-IsolatedApplication $application $localData $roamingData
    try {
        $database = Join-Path $localData 'KeRuiRecruit\db\recruit.sqlite3'
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-Path -LiteralPath $database) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Path -LiteralPath $database)) {
            throw "Application did not initialize its isolated database"
        }
    }
    finally {
        if ($process -and -not $process.HasExited) {
            $process.Kill($true)
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
    if (-not (Test-Path -LiteralPath (Join-Path $localData 'KeRuiRecruit') -PathType Container)) {
        throw "Uninstall removed the isolated user data directory"
    }

    return [ordered]@{
        ok = $true
        upgraded_from_previous = [bool]$Previous
        launch_initialized_database = $true
        uninstall_retained_data = $true
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
