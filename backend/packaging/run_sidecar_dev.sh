#!/bin/sh
# Development sidecar launcher: runs the Python backend directly from the venv.
# Used by the Tauri shell via the KERUI_SIDECAR_BIN environment variable, which
# avoids requiring a PyInstaller-packaged binary for `tauri dev`.
exec "/Users/chuzu/Desktop/kerui-recruit/.venv/bin/python" -m kerui_recruit.sidecar "$@"
