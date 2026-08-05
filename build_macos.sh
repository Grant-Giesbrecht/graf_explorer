#!/usr/bin/env bash
# ============================================================
#  Build "GrAF Explorer.app" for macOS with PyInstaller.
#
#  Run from the folder containing src/graf_explorer/ and
#  graf_explorer.spec.
# ============================================================
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

# Use `python3 -m pip`/`python3 -m PyInstaller` everywhere instead of bare
# `pip`/`pyinstaller`: those can resolve to a *different* Python install than
# `python3` (shell aliases don't expand inside a script, and it's common to
# have more than one Python on PATH), which would silently build against the
# wrong site-packages.
PYTHON=python3

# Install/refresh graf_explorer's own runtime dependencies (numpy, matplotlib,
# PyQt5, graf-format, stardust-tools -- see pyproject.toml) plus PyInstaller
# itself. Skipping this is how you get a build that succeeds but crashes on
# launch: PyInstaller only bundles what's already importable in this
# environment, so a machine missing e.g. PyQt5 gets an app that silently
# ships without it and dies at runtime with
# "Failed to import any of the following Qt binding modules".
"$PYTHON" -m pip install -e .
"$PYTHON" -m pip install pyinstaller

# Clean previous builds
rm -rf build dist

# Build using the spec (handles data files + Info.plist document type)
"$PYTHON" -m PyInstaller graf_explorer.spec

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
