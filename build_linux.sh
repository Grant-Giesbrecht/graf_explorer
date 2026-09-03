#!/usr/bin/env bash
# ============================================================
#  Build "GrAF Explorer" for Linux with PyInstaller, and
#  (optionally) register it as the handler for .graf files
#  for the current user via the freedesktop.org menu/MIME
#  mechanisms (~/.local/share/{applications,mime,icons}).
#
#  Run from the project root (same folder as build_macos.sh):
#
#      ./build_linux.sh                  # build + register .graf
#      ./build_linux.sh --no-register    # just build, don't touch menu/MIME db
#      ./build_linux.sh --install-dir "$HOME/.local/opt/graf-explorer"
#
#  The build itself is driven by graf_explorer_linux.spec (as the macOS and
#  Windows builds are by their own specs), so dependency collection, the
#  bundled icons/ tree and the editable-install path fix in build_support.py
#  all live in one place. That is also why there is no --onefile flag: the
#  spec produces the onedir bundle dist/GrAF Explorer/.
#
#  Prereqs: just `python3` on PATH. This script installs graf_explorer
#  itself (via `pip install -e .`, which pulls in numpy/matplotlib/PyQt5/
#  graf-format/stardust-tools per pyproject.toml) plus PyInstaller, so a
#  dependency missing from this environment fails loudly here instead of
#  producing a binary that builds fine but crashes on launch with
#  "Failed to import any of the following Qt binding modules".
# ============================================================
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

APP_NAME="GrAF Explorer"
APP_ID="com.grantgiesbrecht.grafexplorer"
SPEC="graf_explorer_linux.spec"
APP_PNG="src/graf_explorer/icons/app/graf_explorer.png"
DOC_PNG="src/graf_explorer/icons/file/graf_file.png"

REGISTER=1
INSTALL_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-register) REGISTER=0 ;;
    --install-dir) INSTALL_DIR="$2"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
  shift
done

# Use `python3 -m pip`/`python3 -m PyInstaller` throughout instead of bare
# `pip`/`pyinstaller`: those can resolve to a *different* Python install than
# `python3` (shell aliases don't expand inside a script, and it's common to
# have more than one Python on PATH), which would silently build against the
# wrong site-packages.
PYTHON=python3

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "python3 not found on PATH. Install Python 3 first." >&2
  exit 1
fi
if [ ! -f "$SPEC" ]; then
  echo "Cannot find $SPEC. Run this from the project root." >&2
  exit 1
fi

# --- 1. install/refresh dependencies -----------------------------------------
# Installing graf_explorer itself (-e .) pulls in its full dependency set from
# pyproject.toml. Skipping this is how you get a build that succeeds but
# crashes on launch: PyInstaller only bundles what's already importable in
# this environment.
echo "==> Installing dependencies"
"$PYTHON" -m pip install -e .
"$PYTHON" -m pip install pyinstaller

# --- 2. clean previous builds -------------------------------------------------
echo "==> Cleaning old build/dist"
rm -rf build dist

# --- 3. build ------------------------------------------------------------------
# The spec handles data files (icons/), graf's bundled assets, the PyQt6/
# graf.widgets exclusions, and the editable-install paths from build_support.py.
echo "==> Running PyInstaller"
"$PYTHON" -m PyInstaller --noconfirm --clean "$SPEC"

APP_ROOT="$(pwd)/dist/$APP_NAME"
BIN_PATH="$APP_ROOT/$APP_NAME"

# --- 4. optional: install to a stable location ----------------------------------
if [ -n "$INSTALL_DIR" ]; then
  echo "==> Installing to $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  rsync -a --delete "$APP_ROOT/" "$INSTALL_DIR/" 2>/dev/null || cp -a "$APP_ROOT/." "$INSTALL_DIR/"
  APP_ROOT="$INSTALL_DIR"
  BIN_PATH="$APP_ROOT/$APP_NAME"
fi

echo
echo "Built: $BIN_PATH"

# --- 5. register the .graf MIME type + app launcher (current user only) --------
if [ "$REGISTER" -eq 1 ]; then
  echo "==> Registering .graf for the current user"

  APPS_DIR="$HOME/.local/share/applications"
  MIME_DIR="$HOME/.local/share/mime/packages"
  ICON_APPS_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
  ICON_MIME_DIR="$HOME/.local/share/icons/hicolor/256x256/mimetypes"
  mkdir -p "$APPS_DIR" "$MIME_DIR" "$ICON_APPS_DIR" "$ICON_MIME_DIR"

  cp -f "$APP_PNG" "$ICON_APPS_DIR/grafexplorer.png"
  cp -f "$DOC_PNG" "$ICON_MIME_DIR/application-x-graf.png"

  cat > "$MIME_DIR/grafexplorer-graf.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-graf">
    <comment>GrAF File</comment>
    <glob pattern="*.graf"/>
  </mime-type>
</mime-info>
EOF

  cat > "$APPS_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=View and edit .graf data files
Exec="$BIN_PATH" %f
Icon=grafexplorer
Terminal=false
Categories=Science;Graphics;
MimeType=application/x-graf;
EOF
  chmod +x "$APPS_DIR/$APP_ID.desktop"

  update-mime-database "$HOME/.local/share/mime" >/dev/null 2>&1 || true
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
  command -v xdg-mime >/dev/null 2>&1 && xdg-mime default "$APP_ID.desktop" application/x-graf || true

  echo "    .graf -> application/x-graf -> $APP_ID.desktop"
  echo "    open  -> \"$BIN_PATH\" %f"
  echo "    (log out/in, or restart your file manager, if the icon doesn't show up right away)"
fi

echo
echo "============================================================"
echo "  Done. Your build is at:  $BIN_PATH"
echo "  Run it:  \"$BIN_PATH\""
echo "============================================================"
