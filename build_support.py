"""Shared PyInstaller dependency resolution for the macOS / Windows / Linux builds.

Why this file exists
--------------------
graf and stardust are installed in editable mode. setuptools implements editable
installs one of two ways, and which one you get is an implementation detail:

  * a plain path ``.pth``            -- graf_format
  * an ``__editable___*_finder.py``  -- stardust_tools
    (a MetaPathFinder shim registered at interpreter start)

PyInstaller's analysis walks ``sys.path`` statically; it cannot *run* a
MetaPathFinder. So a package installed the second way is invisible to it: the
modules never enter the PYZ and -- the part that actually bites -- *their* imports
are never followed. ``collect_all()`` hides the failure, because it copies the
package's ``.py`` files in as **data**: the package is physically present in the
bundle and looks fine, but data files are not analysed.

That is how ``cryptography`` (imported only by ``stardust.io``) went missing and
made the frozen macOS app die on launch with a bare ModuleNotFoundError.

Feeding the packages' parent directories to ``pathex`` / ``--paths`` puts them
back on the search path the analysis actually uses, so their imports get followed
like any other package. Resolved at build time so the builds stay
machine-independent.

Run directly to print the paths one per line -- that is how build_win.ps1
consumes them:

    python build_support.py
"""

import importlib.util
import os

# Editable-install packages whose sources PyInstaller may fail to locate.
EDITABLE_PACKAGES = ("graf", "stardust")

# Third-party packages reached only *through* graf/stardust, so they appear
# nowhere in this app's own import graph. package_paths() below should now expose
# them to the analysis; naming them explicitly as hidden imports means a future
# editable-install quirk degrades into a bigger bundle rather than a crash.
EXTRA_HIDDEN_IMPORTS = ("cryptography", "h5py")


def package_paths(packages=EDITABLE_PACKAGES):
    """Return the directories that must be on PyInstaller's search path.

    For each package, that is the directory *containing* the package directory
    (e.g. ``.../graf/src`` for ``.../graf/src/graf/__init__.py``), which is what
    ``sys.path`` would hold for a normal install. Packages that cannot be
    imported are skipped: the build then fails with PyInstaller's own missing
    -module error, which is clearer than one raised from here.
    """
    paths = []
    for pkg in packages:
        spec = importlib.util.find_spec(pkg)
        if spec and spec.origin:
            parent = os.path.dirname(os.path.dirname(spec.origin))
            if parent not in paths:
                paths.append(parent)
    return paths


if __name__ == "__main__":
    print("\n".join(package_paths()))
