# macOS Apple Silicon Release Gate

The `desktop-macos-arm64` CI job runs natively on GitHub's `macos-15` arm64 image. It executes backend, React, Rust and browser tests, builds the PyInstaller sidecar without a Windows suffix, validates the binary architecture with `lipo`, and builds `.app` and `.dmg` artifacts using the macOS-specific Tauri configuration.

Local build on an Apple Silicon Mac:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e 'backend[dev]'
cd desktop && npm ci && cd ..
bash backend/packaging/build_sidecar_macos.sh
cd desktop
npm run validate:platforms
npm run tauri:build:macos
```

Current release limitations:

- CI artifacts are unsigned and are suitable only for internal functional acceptance.
- Production distribution requires an Apple Developer ID Application certificate, hardened runtime signing, and notarization credentials.
- Install, launch, upgrade, and uninstall-data-retention must still be verified on a physical Apple Silicon Mac before release.
