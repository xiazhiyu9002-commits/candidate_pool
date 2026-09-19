# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the KeRui Recruit local sidecar.

Build with:
    pyinstaller backend/packaging/kerui_recruit.spec

The result is a self-contained ``kerui-recruit-sidecar`` directory (onedir mode)
whose inner executable binds only to 127.0.0.1 with a per-launch session token.
Onedir avoids per-launch extraction and starts much faster than onefile.
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="kerui-recruit-sidecar",
)
