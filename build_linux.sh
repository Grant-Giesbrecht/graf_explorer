#!/usr/bin/env bash
# ============================================================
#  Build the "GrAF Explorer" bundle for Linux with PyInstaller.
#
#  Run from the folder containing src/graf_explorer/ and
#  graf_explorer_linux.spec, inside the Python environment where
#  `python -m graf_explorer` already works.
# ============================================================
set -e

# Make sure PyInstaller is available
pip install pyinstaller

# Clean previous builds
rm -rf build dist

# Build using the spec (handles data files + editable-install paths)
pyinstaller graf_explorer_linux.spec

echo
echo "============================================================"
echo "  Done. Your bundle is at:  dist/GrAF Explorer"
echo
echo "  Try it:  \"dist/GrAF Explorer/GrAF Explorer\""
echo
echo "  This build does not register a desktop entry or the .graf"
echo "  file association. To do that, install a .desktop file into"
echo "  ~/.local/share/applications/ and a shared-mime-info XML"
echo "  into ~/.local/share/mime/packages/, then run"
echo "  update-desktop-database and update-mime-database."
echo "============================================================"
