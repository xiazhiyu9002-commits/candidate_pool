# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the KeRui Recruit local sidecar.

Build with:
    pyinstaller backend/packaging/kerui_recruit.spec

产物形态**按平台分叉**（2026-09-22 用户口径）：

- **Windows：onedir**。`desktop/src-tauri/tauri.windows.conf.json` 把 sidecar 放进
  `bundle.resources`，壳层的 `sidecar_candidates` 有专门的 onedir 分支
  （`resource_dir\\kerui-recruit-sidecar.exe\\kerui-recruit-sidecar.exe`）。
  onedir 免去每次启动解压整个 bundle，启动明显更快。
- **macOS：onefile**。`tauri.macos.conf.json` 用的是 `bundle.externalBin`，它要求单个
  可执行文件；`backend/packaging/build_sidecar_macos.sh` 也按单文件校验与 `lipo` 验证。

两种形态的差异只在这一处（`COLLECT` 与否），Analysis 与 datas/hiddenimports 完全共用，
所以不值得拆成两个 spec 文件。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hiddenimports = collect_submodules("uvicorn")
hiddenimports += collect_submodules("jieba")
if os.name == "nt":
    hiddenimports += collect_submodules("win32com")
    hiddenimports += ["pythoncom", "pywintypes"]

datas = []
datas += collect_data_files("uvicorn")
datas += collect_data_files("pydantic")
datas += collect_data_files("jieba")
datas += copy_metadata("uuid6")
datas += [(os.path.join(SPECPATH, "..", "src", "kerui_recruit", "providers", "ai", "provider_catalog.builtin.json"), "kerui_recruit/providers/ai")]

a = Analysis(
    [os.path.join(SPECPATH, "run_sidecar.py")],
    pathex=[os.path.join(SPECPATH, "..", "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "pandas", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

_exe_options = dict(
    name="kerui-recruit-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if os.name == "nt":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_options)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="kerui-recruit-sidecar",
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_exe_options)

