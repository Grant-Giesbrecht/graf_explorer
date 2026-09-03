# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for building "GrAF Explorer.app" on macOS.
#
#   pyinstaller graf_explorer.spec
#
# Run it from inside the Python environment that can already launch the app
# (i.e. where `python -m graf_explorer` works and graf/stardust import cleanly).
#
# Icons
#   * App icon      : icons/app/graf_app.icns   -> set on BUNDLE (the .app icon
#                     comes from BUNDLE, NOT from EXE, on macOS).
#   * Document icon : icons/file/document.icns  -> must end up in the bundle's
#                     Contents/Resources/ AND be named by CFBundleTypeIconFile.
#                     PyInstaller has no parameter for that, so we copy it in at
#                     the end of this spec (runs after BUNDLE writes the .app).

import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

# build_support lives next to this spec; specs do not run with SPECPATH on sys.path.
sys.path.insert(0, SPECPATH)
from build_support import EXTRA_HIDDEN_IMPORTS, package_paths

APP_ICON = 'src/graf_explorer/icons/app/graf_app.icns'
DOC_ICON = 'src/graf_explorer/icons/file/document.icns'
DOC_ICON_NAME = os.path.basename(DOC_ICON)        # 'document.icns'

binaries, hiddenimports = [], []
# Ship the icon assets so the app's resource_path() finds them at runtime (via
# sys._MEIPASS/icons) for the window/taskbar icon and custom tab sprites. This
# is separate from APP_ICON/DOC_ICON below, which are the .app and document icons.
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
    argv_emulation=False,      # forwards "open with" file args on macOS
    target_arch=None,         # build for the arch you're running on
    icon=[APP_ICON],          # ignored for the .app, but fine to keep
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name='GrAF Explorer',
)

app = BUNDLE(
    coll,
    name='GrAF Explorer.app',
    icon=APP_ICON,            # <-- THIS is what gives the .app its icon
    bundle_identifier='com.grantgiesbrecht.grafexplorer',
    info_plist={
        'CFBundleName': 'GrAF Explorer',
        'CFBundleDisplayName': 'GrAF Explorer',
        'NSHighResolutionCapable': True,

        # Tell macOS this app opens .graf documents (double-click / Open With),
        # and which icon to draw for those documents.
        'CFBundleDocumentTypes': [{
            'CFBundleTypeName': 'GrAF File',
            'CFBundleTypeRole': 'Editor',
            'LSItemContentTypes': ['com.grantgiesbrecht.graf'],
            'LSHandlerRank': 'Owner',
            'CFBundleTypeIconFile': DOC_ICON_NAME,   # looked up in Contents/Resources/
        }],
        # Declare the custom .graf type so the system knows the extension,
        # and give the type itself the same icon.
        'UTExportedTypeDeclarations': [{
            'UTTypeIdentifier': 'com.grantgiesbrecht.graf',
            'UTTypeDescription': 'GrAF File',
            'UTTypeConformsTo': ['public.data'],
            'UTTypeIconFile': DOC_ICON_NAME,
            'UTTypeTagSpecification': {
                'public.filename-extension': ['graf'],
            },
        }],
    },
)

# -- Post-build: place the document icon where Launch Services looks ------------
# BUNDLE() has already written dist/GrAF Explorer.app at this point, so we can
# drop document.icns into Contents/Resources/. CFBundleTypeIconFile above refers
# to it by name.
_src = os.path.join(SPECPATH, DOC_ICON)
_resources = os.path.join(DISTPATH, 'GrAF Explorer.app', 'Contents', 'Resources')
if os.path.isfile(_src):
    os.makedirs(_resources, exist_ok=True)
    shutil.copy(_src, os.path.join(_resources, DOC_ICON_NAME))
    print("[spec] copied {} -> {}".format(DOC_ICON_NAME, _resources))
else:
    print("[spec] WARNING: {} not found; document icon will be missing".format(_src))
