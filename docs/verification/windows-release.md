# Windows Release Gate

## Automated inspection

Run from the repository root:

```powershell
pwsh -File desktop/scripts/verify-windows-release.ps1 `
  -InstallerPath desktop/src-tauri/target/release/bundle/nsis/kerui-recruit-desktop_0.1.0_x64-setup.exe `
  -EvidencePath docs/verification/windows-installer-evidence.json
```

This records the installer byte size, SHA-256 digest and Authenticode status. It does not install or launch the application.

## Isolated install/upgrade/uninstall cycle

On a disposable Windows 10/11 test account or VM, add `-RunInstallCycle`. To verify a real upgrade, also pass `-PreviousInstallerPath` pointing at the previous released installer. The script installs into a unique temporary directory, launches the application with isolated `LOCALAPPDATA` and `APPDATA`, verifies database initialization, silently uninstalls it, and verifies the isolated user-data directory remains.

The script intentionally retains its isolated test directory as evidence. Remove that exact directory manually after reviewing the report.

## Current status

- Functional inspection: passed for the 2026-08-30 internal acceptance artifact.
- Isolated clean installation, launch/database initialization, uninstall and retained-data cycle: passed on Windows 11.
- Upgrade cycle: requires a previous released installer.
- Authenticode signing: `NotSigned`; the artifact is not production-ready.
