# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for building the "GrAF Explorer" Linux bundle.
#
#   pyinstaller graf_explorer_linux.spec
#
# Run it from inside the Python environment that can already launch the app
# (i.e. where `python -m graf_explorer` works and graf/stardust import cleanly).
#
# Output is a directory, dist/GrAF Explorer/, containing the launcher of the same
# name. There is no Linux equivalent of the macOS BUNDLE step: the app icon and
# the .graf file association would come from a .desktop entry plus a shared-mime
# -info XML installed into the user's ~/.local/share, which this spec does not do.
# The in-app window icon still works, since the icons/ tree ships as data below.

import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

# build_support lives next to this spec; specs do not run with SPECPATH on sys.path.
sys.path.insert(0, SPECPATH)
from build_support import EXTRA_HIDDEN_IMPORTS, package_paths

binaries, hiddenimports = [], []
# Ship the icon assets so the app's resource_path() finds them at runtime (via
# sys._MEIPASS/icons) for the window icon and custom tab sprites.
datas = [('src/graf_explorer/icons', 'icons')]

# collect_all grabs submodules AND data files. graf ships its fonts and
# portable_fonts.json under graf/assets/, which graf.base loads at import time --
# without these the bundled app fails on launch. stardust is collected the same
# way to be safe (serializer / sandbox / io submodules).
for pkg in ("graf", "stardust"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# graf and stardust are editable installs that PyInstaller's static analysis can
# fail to locate, which silently drops *their* dependencies from the bundle. See
# build_support.py for the full explanation.
PATHEX = package_paths()

# Pure-python deps that PyInstaller's static analysis can miss.
for pkg in ("pylogfile", "colorama"):
    hiddenimports += collect_submodules(pkg)

hiddenimports += list(EXTRA_HIDDEN_IMPORTS) + ["PyQt5.sip"]

# This app uses PyQt5. However graf.widgets imports PyQt6, and collect_all("graf")
# above sweeps in *every* graf submodule -- so PyQt6 gets pulled into the freeze
# alongside PyQt5. PyInstaller refuses to bundle two Qt bindings, hence the
# "attempt to collect multiple Qt bindings packages" abort. The app never imports
# graf.widgets (graf.base pulls no graf submodules), so drop it and the other Qt
# bindings entirely.
hiddenimports = [h for h in hiddenimports if not h.startswith("graf.widgets")]
EXCLUDES = ["PyQt6", "PySide6", "PySide2", "graf.widgets"]


a = Analysis(
    ['src/graf_explorer/__main__.py'],
    pathex=PATHEX,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GrAF Explorer',
    console=False,            # windowed app, no terminal
    disable_windowed_traceback=False,
    target_arch=None,         # build for the arch you're running on
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name='GrAF Explorer',
)
