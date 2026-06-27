#!/usr/bin/env bash
# ============================================================
#  Build "GrAF Explorer.app" for macOS with PyInstaller.
#
#  Run from the folder containing graf_explorer.py and
#  graf_explorer.spec, inside the Python environment where
#  `python graf_explorer.py` already works.
# ============================================================
set -e

# Make sure PyInstaller is available
pip install pyinstaller

# Clean previous builds
rm -rf build dist

# Build using the spec (handles data files + Info.plist document type)
pyinstaller graf_explorer.spec

echo
echo "============================================================"
echo "  Done. Your app bundle is at:  dist/GrAF Explorer.app"
echo
echo "  Try it:        open \"dist/GrAF Explorer.app\""
echo "  Install it:    drag it into /Applications"
echo
echo "  To make .graf double-click to this app, after copying to"
echo "  /Applications, right-click a .graf file > Get Info >"
echo "  'Open with' > GrAF Explorer > 'Change All...'"
echo "  (or run the lsregister line below to refresh Launch Services)"
echo "============================================================"

# Optional: force Launch Services to notice the new document type
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "dist/GrAF Explorer.app" 2>/dev/null || true
