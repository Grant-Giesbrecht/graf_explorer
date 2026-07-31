# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('src/graf_explorer/icons', 'icons')]
binaries = []
hiddenimports = ['PyQt5.sip']
hiddenimports += collect_submodules('pylogfile')
hiddenimports += collect_submodules('ganymede')
hiddenimports += collect_submodules('colorama')
tmp_ret = collect_all('graf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('stardust')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# This app uses PyQt5, but graf.widgets imports PyQt6 and collect_all('graf')
# sweeps in every graf submodule -- PyInstaller refuses to bundle two Qt bindings.
# The app never imports graf.widgets, so drop it and the other Qt bindings.
hiddenimports = [h for h in hiddenimports if not h.startswith('graf.widgets')]
EXCLUDES = ['PyQt6', 'PySide6', 'PySide2', 'graf.widgets']


a = Analysis(
    ['src\\graf_explorer\\__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GrAF Explorer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icons\\app\\graf_app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GrAF Explorer',
)
