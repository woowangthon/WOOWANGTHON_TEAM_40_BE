# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 빌드 설정. 실행: companion/.venv/Scripts/pyinstaller companion.spec
# 결과: companion/dist/companion.exe

block_cipher = None

a = Analysis(
    ["companion.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["webview.platforms.edgechromium", "webview.platforms.winforms", "clr_loader"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="companion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
