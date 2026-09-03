#!/usr/bin/env python3
"""
GrAF Explorer
=============
A desktop viewer/editor for GrAF (TOME) files. Each file opens in its own tab with
an embedded matplotlib plot and a sidebar:

    • top    — full internal structure of the file (Group / Dataset / attr tree)
    • bottom — X / Y / (Z) data for a trace or surface, picked from a dropdown

Editing
    A lock button (locked by default) governs editing in BOTH the structure tree
    and the trace-data table. When unlocked:
      - any value in FILE STRUCTURE is editable in place
      - trace cells are editable, and rows can be added/removed
    Every change immediately re-renders the plot. A change that produces a file
    that cannot be rendered is highlighted in red. Modified files show a '*' in
    the tab and are never written to disk until you choose File > Save As…

Open files via the File menu (Cmd+O) or by dragging them onto the window.
Theme and fonts are adjustable from the View menu.

Requirements:
    pip install PyQt5 matplotlib numpy
    plus your GrAF stack: graf, stardust, pylogfile, colorama
"""

import os
import sys
import csv
import json
import copy
import pickle
import tempfile
import traceback
from pathlib import Path
from dataclasses import dataclass, replace

import numpy as np

import matplotlib
matplotlib.use("Qt5Agg")           # Lock the backend BEFORE graf.base pulls in pyplot.
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
    NavigationToolbar2QT as NavigationToolbar,
)

from PyQt5.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QEvent, QObject, QTimer, QSettings,
    pyqtSignal,
)
from PyQt5.QtGui import QKeySequence, QBrush, QColor, QIcon, QDoubleValidator
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QSplitter, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTableView, QTreeWidget,
    QTreeWidgetItem, QFileDialog, QMessageBox, QStyleFactory, QHeaderView,
    QPushButton, QAbstractItemView, QActionGroup, QCheckBox, QSizePolicy,
    QStyledItemDelegate,
    QLineEdit, QSpinBox, QGridLayout, QFormLayout,
    QMenu, QInputDialog, QDialog,
)

# Optional analysis dependencies — the app still runs without them, the relevant
# UI just reports that they're missing.
try:
    import mplcursors
except Exception:
    mplcursors = None

try:
    from scipy.optimize import curve_fit as _scipy_curve_fit
    from scipy.signal import windows as _scipy_windows
except Exception:
    _scipy_curve_fit = None
    _scipy_windows = None

# Trace formatting dialog (line / marker / error-bar styling + colour picker).
try:
    from graf_explorer.style_dialog import (TraceStyleDialog, apply_themed_icon,
                                            set_icon_tint)
except ImportError:                 # running app.py directly out of src/
    from style_dialog import TraceStyleDialog, apply_themed_icon, set_icon_tint

# ── GrAF stack ────────────────────────────────────────────────────────────────
from graf.base import Graf
from stardust.tome import dict_to_tome   # same import graf.base itself uses


# ── Theme system ───────────────────────────────────────────────────────────────
FLOAT_FMT = "%.8g"
INVALID_RED = "#ff5b5b"
ARRAY_EXPAND_LIMIT = 100   # 1-D arrays at or below this are expanded to editable rows


def _resource_bases():
    """Candidate roots that may contain bundled resources, most specific first:
    the PyInstaller unpack dir, the script's own dir, its parent (so a dev run
    from src/ finds ../icons), and the working directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        bases.append(mp)
    bases.extend([here, os.path.dirname(here), os.getcwd()])
    seen, out = set(), []
    for b in bases:
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def resource_path(*parts) -> str:
    """Resolve a resource path that works when run as a script (from src/ or the
    project root) and when frozen by PyInstaller. Returns the first existing
    candidate; if none exist, returns the most-likely intended location so
    callers can still test/branch on it."""
    bases = _resource_bases()
    for base in bases:
        p = os.path.join(base, *parts)
        if os.path.exists(p):
            return p
    return os.path.join(bases[0], *parts)


def find_app_icon() -> str:
    """Locate the application icon for the window/taskbar (NOT the same thing as
    the .exe's embedded icon or the .graf document icon). Prefers .ico on
    Windows for crisp multi-resolution rendering."""
    for rel in (("icons", "app", "graf_app.ico"), ("icons", "app", "graf_app.png")):
        p = resource_path(*rel)
        if os.path.isfile(p):
            return p
    return ""


def application_icon() -> QIcon:
    p = find_app_icon()
    return QIcon(p) if p else QIcon()


def custom_icon_qss() -> str:
    """Inject QSS for any custom widget sprites the user drops in ./icons.
    Drop e.g. icons/tab_close.png (and optionally tab_close_hover.png) next to
    the script (or bundle it via PyInstaller datas) and it is used automatically.
    Add more sub-control rules here for other widget bits you want to reskin."""
    frag = ""
    close_png = resource_path("icons", "tab_close.png")
    if os.path.exists(close_png):
        normal = close_png.replace("\\", "/")
        hover_png = resource_path("icons", "tab_close_hover.png")
        hover = (hover_png if os.path.exists(hover_png) else close_png).replace("\\", "/")
        frag += f"""
QTabBar::close-button {{
    image: url("{normal}");
    width: 14px; height: 14px;
    subcontrol-position: right;
    margin: 2px;
}}
QTabBar::close-button:hover {{ image: url("{hover}"); }}
"""
    return frag


FONT_FAMILIES = {
    "Sans":  '"Helvetica Neue", "Segoe UI", "Arial", sans-serif',
    "Mono":  '"Menlo", "Consolas", "DejaVu Sans Mono", monospace',
    "Serif": '"Georgia", "Times New Roman", serif',
}
DATA_FAMILY = '"Menlo", "Consolas", "DejaVu Sans Mono", monospace'


@dataclass
class Theme:
    name: str
    bg: str
    surface: str
    surface_alt: str
    header_bg: str
    accent: str
    accent_hover: str
    text: str
    subtext: str
    border: str
    sel_bg: str
    sel_text: str
    provenance: str = "#c8963e"     # colour for protected provenance tree rows
    ui_family: str = FONT_FAMILIES["Sans"]
    base_pt: int = 13


THEMES = {
    "Graphite": Theme(
        name="Graphite",
        bg="#21252b", surface="#282c34", surface_alt="#2e333d",
        header_bg="#2f3540", accent="#4b89dc", accent_hover="#5d97e6",
        text="#e8eaed", subtext="#9aa0a6", border="#3a3f4b",
        sel_bg="#3d5a80", sel_text="#ffffff", provenance="#d1a054",
    ),
    "Daylight": Theme(
        name="Daylight",
        bg="#f4f6f8", surface="#ffffff", surface_alt="#eef1f5",
        header_bg="#e6eaf0", accent="#2f6fde", accent_hover="#1f5fce",
        text="#1b1f24", subtext="#5a626b", border="#d3d8df",
        sel_bg="#cfe2ff", sel_text="#0b2545", provenance="#9a6b12",
    ),
    "Midnight": Theme(
        name="Midnight",
        bg="#1a1a2e", surface="#16213e", surface_alt="#1b2747",
        header_bg="#0f3460", accent="#e94560", accent_hover="#ff5b78",
        text="#eaeaea", subtext="#8888aa", border="#2a2a4a",
        sel_bg="#e94560", sel_text="#ffffff", provenance="#e0b354",
    ),
}
DEFAULT_THEME = "Graphite"


def build_stylesheet(t: Theme) -> str:
    pt = t.base_pt
    return f"""
QMainWindow, QWidget {{
    background-color: {t.bg};
    color: {t.text};
    font-family: {t.ui_family};
    font-size: {pt}px;
}}

QTabWidget::pane {{
    border: 1px solid {t.border};
    border-top: 2px solid {t.accent};
    background-color: {t.surface};
}}
QTabBar::tab {{
    background-color: {t.header_bg};
    color: {t.subtext};
    padding: 7px 16px;
    margin-right: 2px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}}
QTabBar::tab:selected {{
    background-color: {t.surface};
    color: {t.text};
    border-bottom: 2px solid {t.accent};
}}

QMenuBar, QMenu {{ background-color: {t.surface}; color: {t.text}; }}
QMenuBar::item:selected, QMenu::item:selected {{
    background-color: {t.sel_bg}; color: {t.sel_text};
}}

QLabel#sectionHeader {{
    color: {t.accent};
    font-weight: bold;
    padding: 6px 2px 4px 2px;
    letter-spacing: 1px;
}}
QLabel#welcomeTitle {{ font-size: {pt + 13}px; color: {t.text}; font-weight: bold; }}
QLabel#welcomeHint  {{ font-size: {pt + 1}px; color: {t.subtext}; }}

QTreeWidget, QTableView {{
    background-color: {t.surface};
    alternate-background-color: {t.surface_alt};
    border: 1px solid {t.border};
    gridline-color: {t.border};
    selection-background-color: {t.sel_bg};
    selection-color: {t.sel_text};
    font-family: {DATA_FAMILY};
    font-size: {pt}px;
}}
QHeaderView::section {{
    background-color: {t.header_bg};
    color: {t.text};
    padding: 4px 6px;
    border: none;
    border-right: 1px solid {t.border};
}}

QComboBox {{
    background-color: {t.header_bg};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 5px 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {t.surface};
    color: {t.text};
    selection-background-color: {t.sel_bg};
    selection-color: {t.sel_text};
}}

QCheckBox {{ color: {t.text}; padding: 2px 4px; }}

QPushButton {{
    background-color: {t.accent};
    color: #ffffff;
    border: none;
    border-radius: 5px;
    padding: 9px 22px;
    font-weight: bold;
}}
QPushButton:hover {{ background-color: {t.accent_hover}; }}
QPushButton:disabled {{ background-color: {t.border}; color: {t.subtext}; }}

/* Compact secondary buttons (expand/collapse, add/remove) */
QPushButton#miniButton {{
    background-color: {t.header_bg};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 4px 12px;
    font-weight: normal;
}}
QPushButton#miniButton:hover {{ background-color: {t.accent}; color: #ffffff; }}
QPushButton#miniButton:disabled {{ color: {t.subtext}; background-color: {t.surface}; }}

/* Lock toggle — compact, not a full-size action button */
QPushButton#lockButton {{
    background-color: {t.header_bg};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 4px 12px;
    font-weight: normal;
    text-align: left;
}}
QPushButton#lockButton:hover {{ background-color: {t.accent}; color: #ffffff; }}

/* Square icon buttons (colour picker, per-row format buttons) */
QPushButton#iconButton {{
    background-color: {t.header_bg};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 0px;
    font-weight: normal;
}}
QPushButton#iconButton:hover {{ background-color: {t.accent}; color: #ffffff; }}

QLineEdit, QAbstractSpinBox {{
    background-color: {t.header_bg};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {t.sel_bg};
    selection-color: {t.sel_text};
}}
QLineEdit:read-only {{ color: {t.subtext}; }}
QLineEdit:disabled, QAbstractSpinBox:disabled {{ color: {t.subtext}; }}

QDialog {{ background-color: {t.bg}; }}
QGroupBox {{
    border: 1px solid {t.border};
    border-radius: 5px;
    margin-top: 12px;
    padding-top: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {t.accent};
    font-weight: bold;
}}

QSlider::groove:horizontal {{
    height: 5px;
    background: {t.border};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {t.text};
    width: 12px;
    margin: -5px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {t.accent_hover}; }}

QToolBar {{ background-color: {t.surface}; border: none; spacing: 2px; }}

QScrollBar:vertical {{ background: {t.bg}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 6px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {t.accent}; }}
QScrollBar:horizontal {{ background: {t.bg}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 6px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

QSplitter::handle {{ background-color: {t.border}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
""" + custom_icon_qss()


# ── Loading + value helpers ─────────────────────────────────────────────────────
def load_graf_file(path) -> Graf:
    g = Graf()
    if hasattr(g, "read_graf"):
        g.read_graf(str(path))
    elif hasattr(g, "load_hdf"):
        g.load_hdf(str(path))
    else:
        raise RuntimeError("Installed 'graf' exposes neither read_graf nor load_hdf.")
    return g


APP_NAME = "GrAF Explorer"
APP_VERSION = "1.0"
APP_ID = f"{APP_NAME} {APP_VERSION}"


def write_packet_as_graf(packet, path, action=None, source_format=None):
    """Write a packet to a .graf through Graf.write_graf so provenance is stamped
    and the mutation history is updated. Returns the Graf (whose info now holds
    the freshly stamped provenance/history) so the caller can sync it back into
    an in-memory packet. Falls back to a raw tome write on older libraries."""
    g = Graf()
    g.unpack(copy.deepcopy(packet))
    if hasattr(g, "write_graf"):
        try:
            g.write_graf(str(path), source_app=APP_ID, action=action,
                         source_format=source_format)
            return g
        except TypeError:
            g.write_graf(str(path))       # older write_graf without prov kwargs
            return g
    dict_to_tome(copy.deepcopy(packet), str(path), show_detail=False)
    return g


def deep_listify(obj):
    """Convert tuples to lists in-place throughout a packed dict, so every
    array element (e.g. an RGB colour) is mutable and therefore editable."""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if isinstance(obj[k], tuple):
                obj[k] = list(obj[k])
            deep_listify(obj[k])
    elif isinstance(obj, list):
        for i in range(len(obj)):
            if isinstance(obj[i], tuple):
                obj[i] = list(obj[i])
            deep_listify(obj[i])


def decode_bytes(v):
    """Turn a bytes/np.bytes_ value into str (HDF5/TOME hands strings back as
    bytes). '\\xe2\\x88\\x92' etc. are UTF-8, so decode as UTF-8 first."""
    if isinstance(v, (bytes, bytearray, np.bytes_)):
        try:
            return bytes(v).decode("utf-8")
        except Exception:
            return bytes(v).decode("latin-1", "replace")
    return v


def deep_decode_bytes(obj):
    """Recursively decode byte strings to str throughout a packed dict, in place.
    Numeric data is untouched (only bytes are converted)."""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            if isinstance(v, (bytes, bytearray, np.bytes_)):
                obj[k] = decode_bytes(v)
            else:
                deep_decode_bytes(v)
    elif isinstance(obj, list):
        for i in range(len(obj)):
            v = obj[i]
            if isinstance(v, (bytes, bytearray, np.bytes_)):
                obj[i] = decode_bytes(v)
            else:
                deep_decode_bytes(v)


def sanitize_graf_text(g):
    """Decode byte-string tick labels / axis labels / trace names on a loaded
    Graf object so matplotlib renders text, not b'...' reprs. graf leaves these
    as bytes because that's what the HDF5 reader returns."""
    def dec_seq(seq):
        if isinstance(seq, np.ndarray):
            seq = seq.tolist()
        if isinstance(seq, (list, tuple)):
            return [decode_bytes(x) for x in seq]
        return seq
    try:
        for ax in getattr(g, "axes", {}).values():
            for attr in ("x_axis", "y_axis_L", "y_axis_R", "z_axis"):
                sc = getattr(ax, attr, None)
                if sc is None:
                    continue
                if hasattr(sc, "tick_label_list"):
                    sc.tick_label_list = dec_seq(sc.tick_label_list)
                if hasattr(sc, "label"):
                    sc.label = decode_bytes(sc.label)
            for tr in getattr(ax, "traces", {}).values():
                if hasattr(tr, "display_name"):
                    tr.display_name = decode_bytes(tr.display_name)
    except Exception:
        pass


def _unwrap_scalar(v):
    """Reduce numpy scalars and 0-D arrays to plain Python values."""
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray) and v.ndim == 0:
        return v.item()
    return v


def format_scalar(v) -> str:
    v = _unwrap_scalar(v)
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", "replace")
        except Exception:
            return repr(v)
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return FLOAT_FMT % v
    return str(v)


# Preferred display order for provenance fields (unknown keys follow, in order).
_PROV_ORDER = [
    "provenance_schema", "created_utc", "created_by", "created_by_app",
    "graf_version", "source_language", "source_format", "source_filename",
    "source_sha256", "creating_script", "hostname", "os_platform",
    "machine_arch", "cpu_model",
]


def _fmt_meta_value(v) -> str:
    """Human-readable one-line rendering of an info/provenance/condition value."""
    v = _unwrap_scalar(v)
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", "replace")
        except Exception:
            return repr(v)
    if isinstance(v, float):
        return FLOAT_FMT % v
    if isinstance(v, (list, tuple)):
        return ", ".join(_fmt_meta_value(x) for x in v)
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_fmt_meta_value(x)}" for k, x in v.items()) + "}"
    return str(v)


def is_scalar_like(v) -> bool:
    """True for anything that should edit as a single value: Python/NumPy
    scalars, strings, bytes, None, and 0-D arrays (e.g. an empty supertitle
    that pack() returns as a 0-D array rather than a str)."""
    if isinstance(v, dict):
        return False
    if isinstance(v, np.ndarray):
        return v.ndim == 0
    if isinstance(v, (list, tuple)):
        return False
    return True


def classify_value(v):
    """Return (type_label, shape, value_or_dtype) mirroring how TOME stores it."""
    if isinstance(v, dict):
        return ("Group", "", "")
    if is_scalar_like(v):
        return ("attr", "", format_scalar(v))
    if isinstance(v, np.ndarray):
        return ("Dataset", str(v.shape), str(v.dtype))
    if isinstance(v, (list, tuple)):
        try:
            a = np.array(v)
            return ("Dataset", str(a.shape), str(a.dtype))
        except Exception:
            return ("Dataset", f"({len(v)},)", "object")
    return ("attr", "", format_scalar(v))


def is_expandable_array(v) -> bool:
    """A 1-D array of scalars, small enough to expand into editable rows."""
    if isinstance(v, np.ndarray):
        return v.ndim == 1 and v.size <= ARRAY_EXPAND_LIMIT
    if isinstance(v, (list, tuple)):
        if len(v) > ARRAY_EXPAND_LIMIT:
            return False
        return all(not isinstance(e, (list, tuple, dict, np.ndarray)) for e in v)
    return False


def coerce_like(old, text):
    """Parse edited text into a value compatible with the existing one."""
    old = _unwrap_scalar(old)
    s = text.strip()
    if isinstance(old, bool):
        low = s.lower()
        if low in ("true", "1", "yes", "y", "t"):
            return True
        if low in ("false", "0", "no", "n", "f"):
            return False
        raise ValueError(f"Cannot parse boolean from {s!r}")
    if isinstance(old, int) and not isinstance(old, bool):
        return int(s, 0) if s.lower().startswith(("0x", "0o", "0b")) else int(s)
    if isinstance(old, float):
        return float(s)
    if isinstance(old, bytes):
        return s.encode("utf-8")
    if isinstance(old, str):
        return s                      # keep strings as strings
    # old is None or unknown — best-effort auto parse
    for caster in (int, float):
        try:
            return caster(s)
        except ValueError:
            pass
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def enumerate_data_items(packet):
    """Flatten traces and surfaces (with live dict refs) from a packed Graf."""
    items = []
    for ax_key, ax in (packet.get("axes", {}) or {}).items():
        for tr_key, tr in (ax.get("traces", {}) or {}).items():
            name = tr.get("display_name", "") or ""
            items.append({"label": f"{ax_key} · {tr_key}   {name or '(unnamed)'}",
                          "kind": "trace", "node": tr,
                          "axis": ax_key, "trace": tr_key, "name": name})
        for sf_key, sf in (ax.get("surfaces", {}) or {}).items():
            name = sf.get("display_name", "") or ""
            items.append({"label": f"{ax_key} · {sf_key}   {name or '(unnamed)'}   [surface]",
                          "kind": "surface", "node": sf,
                          "axis": ax_key, "trace": sf_key, "name": name})
    return items


def _to_number_list(v):
    """Coerce a data array (list / tuple / ndarray) into a flat list of plain
    Python numbers, unwrapping any numpy scalar types."""
    if v is None:
        return []
    if isinstance(v, np.ndarray):
        v = v.tolist()
    elif isinstance(v, tuple):
        v = list(v)
    elif not isinstance(v, list):
        try:
            v = list(v)
        except TypeError:
            return []
    return [_unwrap_scalar(e) for e in v]


def collect_trace_data(packet):
    """Trace data only (no surfaces), labelled by axis / trace / display name."""
    out = []
    for ax_key, ax in (packet.get("axes", {}) or {}).items():
        for tr_key, tr in (ax.get("traces", {}) or {}).items():
            out.append({
                "axis": ax_key,
                "trace": tr_key,
                "name": tr.get("display_name", "") or "",
                "x": _to_number_list(tr.get("x_data", [])),
                "y": _to_number_list(tr.get("y_data", [])),
                "z": _to_number_list(tr.get("z_data", [])),
            })
    return out


def _axis_label(ax, key):
    """Pull a Scale's label out of a packed axis dict, defensively."""
    scale = ax.get(key)
    if isinstance(scale, dict):
        lab = scale.get("label", "")
        return lab if isinstance(lab, str) else ""
    return ""


def unique_file_labels(paths):
    """Short display labels for the open files that stay distinct when two of
    them share a basename: only the colliding names grow parent directories,
    and identical paths (the same file opened twice) are numbered."""
    paths = [Path(p) for p in paths]
    labels = [p.name for p in paths]
    depth = 1
    while True:
        groups = {}
        for i, lab in enumerate(labels):
            groups.setdefault(lab, []).append(i)
        clashes = [idxs for idxs in groups.values() if len(idxs) > 1]
        if not clashes:
            break
        depth += 1
        grew = False
        for idxs in clashes:
            for i in idxs:
                parts = paths[i].parts
                if depth <= len(parts):
                    labels[i] = os.path.join(*parts[-depth:])
                    grew = True
        if not grew:                  # paths exhausted: they are the same file
            for idxs in clashes:
                for n, i in enumerate(idxs, 1):
                    labels[i] = f"{labels[i]} ({n})"
            break
    return labels


def overlay_traces_from_packet(packet, fname, uid=None):
    """Flatten a file's traces for the comparison view, keeping each trace's
    parent-axis X/Y labels and its own formatting (so the overlay can either
    reuse the source format or fall back to defaults).

    `uid` identifies the source tab; it -- not the file name -- is the trace's
    identity, so files that share a basename never collide."""
    out = []
    for ax_key, ax in (packet.get("axes", {}) or {}).items():
        xlabel = _axis_label(ax, "x_axis")
        ylabel = _axis_label(ax, "y_axis_L")
        for tr_key, tr in (ax.get("traces", {}) or {}).items():
            out.append({
                "uid": uid,
                "file": fname,
                "axis": ax_key,
                "trace": tr_key,
                "name": tr.get("display_name", "") or "",
                "x": _to_number_list(tr.get("x_data", [])),
                "y": _to_number_list(tr.get("y_data", [])),
                "xlabel": xlabel,
                "ylabel": ylabel,
                "line_color": tr.get("line_color"),
                "line_type": tr.get("line_type"),
                "marker_type": tr.get("marker_type"),
                "marker_size": tr.get("marker_size"),
                "line_width": tr.get("line_width"),
                "alpha": tr.get("alpha"),
                "marker_color": tr.get("marker_color"),
                "has_error_bars": bool(tr.get("has_error_bars")),
                "err_line_color": tr.get("err_line_color"),
                "err_line_width": tr.get("err_line_width"),
                "err_cap_size": tr.get("err_cap_size"),
                "err_cap_color": tr.get("err_cap_color"),
                "err_cap_width": tr.get("err_cap_width"),
                "err_cap_visible": tr.get("err_cap_visible"),
            })
    return out


def overlay_plot_kwargs(tr):
    """matplotlib plot kwargs reproducing a trace's stored format (graf's
    LINE_TYPES/MARKER_TYPES are already mpl-native, except '[]' → square)."""
    kw = {}
    lc = tr.get("line_color")
    if lc is not None:
        try:
            kw["color"] = tuple(float(c) for c in lc)
        except Exception:
            pass
    lt = tr.get("line_type")
    if lt:
        kw["linestyle"] = lt
    mt = tr.get("marker_type")
    if mt and mt != "None":
        kw["marker"] = "s" if mt == "[]" else mt
        mc = tr.get("marker_color")
        if mc is not None:
            try:
                mc = tuple(float(c) for c in mc)
                kw["markerfacecolor"] = mc
                kw["markeredgecolor"] = mc
            except Exception:
                pass
    for src, dst in (("line_width", "linewidth"), ("alpha", "alpha"),
                     ("marker_size", "markersize")):
        v = tr.get(src)
        if v is not None:
            try:
                kw[dst] = float(v)
            except Exception:
                pass
    return kw


def default_color_cycle():
    """matplotlib's current default color cycle as a list of RGB tuples."""
    try:
        import matplotlib as mpl
        from matplotlib.colors import to_rgb
        cols = mpl.rcParams["axes.prop_cycle"].by_key().get("color", [])
        return [to_rgb(c) for c in cols] or [(0.12, 0.47, 0.71)]
    except Exception:
        return [(0.12, 0.47, 0.71)]


def apply_default_trace_format(tr_dict, rgb):
    """Reset a packed trace dict's format fields to clean defaults in `rgb`."""
    color = [float(c) for c in rgb]
    tr_dict["line_color"] = color
    tr_dict["marker_color"] = color
    tr_dict["line_type"] = "-"
    tr_dict["marker_type"] = "None"
    tr_dict["marker_size"] = 1
    tr_dict["line_width"] = 1
    tr_dict["alpha"] = 1


def rescale_axis_to_data(axis, xvals, yvals):
    """Recompute a packed axis's X and left-Y Scale limits/ticks to span the
    given data. graf's apply_to always forces the stored limits and ticks, so a
    packet built from a template must have these refreshed or the data clips."""
    def clean(vals):
        out = [float(v) for v in vals
               if isinstance(v, (int, float)) and np.isfinite(v)]
        return (min(out), max(out)) if out else None

    def write(scale, bounds):
        if not isinstance(scale, dict) or bounds is None:
            return
        lo, hi = bounds
        if hi <= lo:
            hi = lo + 1.0
        ticks = [float(t) for t in MaxNLocator(nbins=6).tick_values(lo, hi)
                 if lo - 1e-9 <= t <= hi + 1e-9]
        if len(ticks) < 2:
            ticks = list(np.linspace(lo, hi, 5))
        pad = (hi - lo) * 0.05
        scale["is_valid"] = True
        scale["val_min"] = lo - pad
        scale["val_max"] = hi + pad
        scale["tick_list"] = ticks
        scale["minor_tick_list"] = []
        scale["tick_label_list"] = [f"{t:g}" for t in ticks]

    write(axis.get("x_axis"), clean(xvals))
    write(axis.get("y_axis_L"), clean(yvals))


# ── Analysis helpers (stats / FFT / curve fitting) ──────────────────────────────
def trace_statistics(y):
    a = np.asarray(y, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return None
    return [
        ("N", f"{a.size}"),
        ("min", FLOAT_FMT % a.min()),
        ("max", FLOAT_FMT % a.max()),
        ("peak-peak", FLOAT_FMT % np.ptp(a)),
        ("mean", FLOAT_FMT % a.mean()),
        ("median", FLOAT_FMT % np.median(a)),
        ("RMS", FLOAT_FMT % np.sqrt(np.mean(a ** 2))),
        ("std (n-1)", FLOAT_FMT % (a.std(ddof=1) if a.size > 1 else 0.0)),
    ]


FFT_WINDOWS = ["Rectangular", "Hann", "Hamming", "Blackman", "Bartlett",
               "Flattop", "Blackman-Harris"]


def _fft_window(name, n):
    """Return an n-point window. Uses scipy.signal.windows when available for the
    richer set, else falls back to numpy for the common ones."""
    key = name.lower()
    if key in ("rectangular", "rect", "none", "boxcar"):
        return np.ones(n)
    if _scipy_windows is not None:
        mapping = {"hann": "hann", "hamming": "hamming", "blackman": "blackman",
                   "bartlett": "bartlett", "flattop": "flattop",
                   "blackman-harris": "blackmanharris"}
        return _scipy_windows.get_window(mapping.get(key, "hann"), n, fftbins=True)
    # numpy fallback (Flattop / Blackman-Harris unavailable → Blackman)
    return {"hann": np.hanning, "hamming": np.hamming,
            "blackman": np.blackman, "bartlett": np.bartlett,
            "flattop": np.blackman, "blackman-harris": np.blackman
            }.get(key, np.hanning)(n)


def compute_fft(x, y, window="Hann", db=True, xmin=None, xmax=None, resample=False):
    """One-sided amplitude spectrum of y. Returns (freq, mag, info, xunit_known)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    order = np.argsort(x)
    x, y = x[order], y[order]
    if xmin is not None:
        sel = x >= xmin
        x, y = x[sel], y[sel]
    if xmax is not None:
        sel = x <= xmax
        x, y = x[sel], y[sel]
    n = y.size
    if n < 4:
        raise ValueError("need at least 4 points in the selected range")

    resampled = False
    if resample:
        xu = np.linspace(x[0], x[-1], n)        # uniform grid over the same span
        y = np.interp(xu, x, y)
        x = xu
        resampled = True

    diffs = np.diff(x)
    uniform = diffs.size > 0 and np.allclose(diffs, diffs[0], rtol=1e-3, atol=0) \
        and diffs[0] > 0
    dt = float(np.mean(diffs)) if diffs.size and np.mean(diffs) > 0 else 1.0

    w = _fft_window(window, n)
    yw = (y - y.mean()) * w
    spec = np.fft.rfft(yw)
    freq = np.fft.rfftfreq(n, d=dt)
    mag = np.abs(spec) * 2.0 / np.sum(w)        # amplitude-correct normalization
    if db:
        mag = 20.0 * np.log10(np.maximum(mag, 1e-300))
    info = f"{n} pts · {window} · {'dB' if db else 'linear'}"
    if resampled:
        info += " · resampled to uniform"
    elif not uniform:
        info += " · non-uniform X (Δx averaged)"
    return freq, mag, info, (uniform or resampled)


# Curve-fit models: name -> (function(x, *p), param_names, guess(x, y)).
def _g_linear(x, y):
    return (1.0, 0.0)


def _g_exp(x, y):
    span = max(x.max() - x.min(), 1e-9)
    return (y.max() - y.min() or 1.0, 1.0 / span, y.min())


def _g_gauss(x, y):
    amp = y.max() - y.min() or 1.0
    mu = x[np.argmax(y)]
    sigma = max((x.max() - x.min()) / 6.0, 1e-9)
    return (amp, mu, sigma, y.min())


def _g_lorentz(x, y):
    amp = y.max() - y.min() or 1.0
    x0 = x[np.argmax(y)]
    gamma = max((x.max() - x.min()) / 6.0, 1e-9)
    return (amp, x0, gamma, y.min())


def _g_power(x, y):
    return (1.0, 1.0)


def _g_sine(x, y):
    amp = (y.max() - y.min()) / 2.0 or 1.0
    span = max(x.max() - x.min(), 1e-9)
    return (amp, 1.0 / span, 0.0, y.mean())


FIT_MODELS = {
    "Linear":      (lambda x, a, b: a * x + b, ["a", "b"], _g_linear),
    "Exponential": (lambda x, a, b, c: a * np.exp(b * x) + c, ["a", "b", "c"], _g_exp),
    "Gaussian":    (lambda x, a, mu, sig, c: a * np.exp(-(x - mu) ** 2 / (2 * sig ** 2)) + c,
                    ["a", "mu", "sigma", "c"], _g_gauss),
    "Lorentzian":  (lambda x, a, x0, g, c: a * g ** 2 / ((x - x0) ** 2 + g ** 2) + c,
                    ["a", "x0", "gamma", "c"], _g_lorentz),
    "Power law":   (lambda x, a, b: a * np.power(x, b), ["a", "b"], _g_power),
    "Sine":        (lambda x, a, f, phi, c: a * np.sin(2 * np.pi * f * x + phi) + c,
                    ["a", "f", "phi", "c"], _g_sine),
}
# "Polynomial" is handled separately (np.polyfit with a chosen degree).


def fit_trace(x, y, model, degree=2):
    """Fit and return (params:list[(name,val)], r2, xfit, yfit, label)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if x.size < 2:
        raise ValueError("not enough finite points to fit")
    order = np.argsort(x)
    x, y = x[order], y[order]
    xfit = np.linspace(x.min(), x.max(), max(200, x.size))

    if model == "Polynomial":
        coeffs = np.polyfit(x, y, int(degree))
        yhat = np.polyval(coeffs, x)
        yfit = np.polyval(coeffs, xfit)
        params = [(f"c{int(degree) - i}", c) for i, c in enumerate(coeffs)]
        label = f"poly deg {int(degree)}"
    else:
        if _scipy_curve_fit is None:
            raise RuntimeError("curve fitting needs scipy (pip install scipy)")
        func, names, guess = FIT_MODELS[model]
        p0 = guess(x, y)
        popt, _ = _scipy_curve_fit(func, x, y, p0=p0, maxfev=10000)
        yhat = func(x, *popt)
        yfit = func(xfit, *popt)
        params = list(zip(names, popt))
        label = model

    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return params, r2, xfit, yfit, label


# ── Editable trace-data table model ─────────────────────────────────────────────
class TraceTableModel(QAbstractTableModel):
    """Edits operate on the actual packet lists, so changes propagate to render
    and save. Surfaces are passed as read-only copies."""

    def __init__(self, columns, editable, on_commit=None, on_before_edit=None):
        super().__init__()
        self._headers = [c[0] for c in columns]
        self._cols = [c[1] for c in columns]      # list references (traces) or copies
        self._editable = editable
        self._on_commit = on_commit               # callable(row, col) -> render_ok(bool)
        self._on_before_edit = on_before_edit     # called once before a mutation (undo)
        self._invalid = set()
        self._recount()

    def _recount(self):
        self._nrows = max((len(c) for c in self._cols), default=0)

    def rowCount(self, parent=QModelIndex()):
        return self._nrows

    def columnCount(self, parent=QModelIndex()):
        return len(self._cols)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            col = self._cols[c]
            if r < len(col):
                v = col[r]
                if role == Qt.EditRole:
                    return str(v)
                try:
                    return FLOAT_FMT % float(v)
                except (TypeError, ValueError):
                    return str(v)
            return ""
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.BackgroundRole and (r, c) in self._invalid:
            return QBrush(QColor(INVALID_RED))
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        return self._headers[section] if orientation == Qt.Horizontal else str(section)

    def flags(self, index):
        f = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if self._editable:
            f |= Qt.ItemIsEditable
        return f

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not self._editable:
            return False
        r, c = index.row(), index.column()
        col = self._cols[c]
        if r >= len(col):
            return False
        try:
            newv = float(value)
        except (TypeError, ValueError):
            self._invalid.add((r, c))
            self.dataChanged.emit(index, index)
            return False
        if self._on_before_edit:
            self._on_before_edit()        # undo snapshot, before mutating
        col[r] = newv
        ok = self._on_commit(r, c) if self._on_commit else True
        if ok:
            self._invalid.discard((r, c))
        else:
            self._invalid.add((r, c))
        self.dataChanged.emit(index, index)
        return True

    def set_editable(self, b):
        self._editable = b
        self.layoutChanged.emit()

    def add_row(self):
        self.beginResetModel()
        for col in self._cols:
            col.append(0.0)
        self._recount()
        self.endResetModel()

    def remove_row(self, r):
        if r < 0:
            return
        self.beginResetModel()
        for col in self._cols:
            if r < len(col):
                del col[r]
        self._recount()
        self.endResetModel()


# ── A tab: one file = lock bar + plot + sidebar ─────────────────────────────────
FORMAT_BTN_TIP = ("Edit line / marker / error-bar formatting for every trace "
                  "in this file")
FORMAT_BTN_TIP_LOCKED = "Unlock the file to edit trace formatting"


class FileTab(QWidget):
    _n_created = 0
    modifiedChanged = pyqtSignal()

    def __init__(self, graf: Graf, path: Path, parent=None):
        super().__init__(parent)
        FileTab._n_created += 1
        self._fig_id = FileTab._n_created
        self.uid = self._fig_id           # stable identity for comparison tabs
        self._render_seq = 0

        self.graf = graf
        self.path = Path(path)
        self.fig = None
        self.canvas = None
        self.fft_fig = None
        self.fft_canvas = None
        self._cursors = []

        self.locked = True
        self.modified = False
        self._building = False
        self._invalid_items = {}   # id(item) -> QTreeWidgetItem
        self._table_model = None
        self._current_is_trace = False
        self._undo_stack = []
        self._redo_stack = []
        self._undo_limit = 50

        # analysis / render state
        self._fft_on = False              # show the separate FFT panel below
        self._fit_result = None           # dict(ax_key,x,y,label) or None
        self._hidden_traces = set()       # {(ax_key, tr_key)} hidden from the plot
        self.legend_on = True             # viewer-drawn legend (graf draws none)
        self.cursors_enabled = True
        self.analysis_visible = False

        # metadata / provenance state
        self._provenance_color = "#c8963e"     # theme-driven; set by the window
        self._allow_provenance_edits = False   # provenance is read-only by default
        self.metadata_visible = False

        # Packed dict is the single source of truth for edits, render, and save.
        self.packet = self.graf.pack()
        deep_listify(self.packet)
        deep_decode_bytes(self.packet)      # HDF5 returns str fields as bytes
        self._original_packet = copy.deepcopy(self.packet)   # for "revert to original"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(self._build_lockbar())

        self.hsplit = QSplitter(Qt.Horizontal)
        self.analysis = self._build_analysis_sidebar()
        self.hsplit.addWidget(self.analysis)
        self.hsplit.addWidget(self._build_plot_panel())
        self.sidebar = self._build_sidebar()
        self.hsplit.addWidget(self.sidebar)
        self.metadata = self._build_metadata_panel()
        self.hsplit.addWidget(self.metadata)
        self.hsplit.setStretchFactor(0, 0)
        self.hsplit.setStretchFactor(1, 3)
        self.hsplit.setStretchFactor(2, 2)
        self.hsplit.setStretchFactor(3, 0)
        self.hsplit.setSizes([300, 760, 480, 320])
        self.analysis.setVisible(False)
        self.metadata.setVisible(False)
        self._saved_split_sizes = None
        outer.addWidget(self.hsplit, 1)  # the splitter takes all extra vertical space

        self._refresh_analysis()
        # First render (routes through _show_current so overlays/cursors apply).
        ok, _ = self._show_current(graf=self.graf)
        if not ok:
            self._set_figure(plt.figure())   # blank fallback; should not happen
        self.set_locked(True)

    # -- top: lock / undo / revert --------------------------------------------
    def _build_lockbar(self):
        bar = QWidget()
        bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 4)

        self.lock_btn = QPushButton()
        self.lock_btn.setObjectName("lockButton")
        self.lock_btn.setFixedHeight(28)
        self.lock_btn.clicked.connect(self.toggle_lock)

        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setObjectName("miniButton")
        self.undo_btn.setFixedHeight(28)
        self.undo_btn.clicked.connect(self.undo)

        self.redo_btn = QPushButton("Redo")
        self.redo_btn.setObjectName("miniButton")
        self.redo_btn.setFixedHeight(28)
        self.redo_btn.clicked.connect(self.redo)

        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setObjectName("miniButton")
        self.revert_btn.setFixedHeight(28)
        self.revert_btn.clicked.connect(self.revert)

        self.format_btn = QPushButton("Format…")
        self.format_btn.setObjectName("miniButton")
        self.format_btn.setFixedHeight(28)
        apply_themed_icon(self.format_btn, "format.png", size=15)
        self.format_btn.setToolTip(FORMAT_BTN_TIP)
        self.format_btn.clicked.connect(self.open_format_dialog)

        h.addWidget(self.lock_btn)
        h.addSpacing(8)
        h.addWidget(self.undo_btn)
        h.addWidget(self.redo_btn)
        h.addWidget(self.revert_btn)
        h.addSpacing(8)
        h.addWidget(self.format_btn)
        h.addStretch(1)
        return bar

    # -- trace formatting ------------------------------------------------------
    def open_format_dialog(self):
        """Tabbed format editor over every trace in this file. Formatting is an
        edit to the file, so it follows the same lock as the tree and table."""
        if self.locked:
            self.struct_status.setText("Unlock the file to edit formatting")
            return
        traces = [it for it in enumerate_data_items(self.packet)
                  if it["kind"] == "trace"]
        if not traces:
            QMessageBox.information(self, "No traces",
                                    f"{self.path.name} has no traces to format.")
            return
        entries = []
        for it in traces:
            label = f"{it['trace']}  {it['name']}".strip()
            entries.append({"key": (it["axis"], it["trace"]),
                            "label": label or it["trace"],
                            "style": it["node"]})
        # Edits apply live, but the whole dialog session is one undo step.
        state = {"snapped": False, "was_modified": self.modified}

        def apply(styles):
            if not state["snapped"]:
                self._snapshot()
                state["snapped"] = True
            self._apply_trace_styles(styles)

        dlg = TraceStyleDialog(entries, self, on_apply=apply,
                               title=f"Trace formatting — {self.path.name}")
        if dlg.exec_() != QDialog.Accepted and state["snapped"]:
            # Cancel already restored the original formatting; drop the undo
            # entry and the modified flag it earned along the way.
            if self._undo_stack:
                self._undo_stack.pop()
            self.modified = state["was_modified"]
            self.modifiedChanged.emit()
            self.struct_status.setText("Formatting unchanged")

    def _apply_trace_styles(self, styles):
        """Write {(axis, trace): style dict} into the packet and re-render."""
        axes = self.packet.get("axes", {}) or {}
        applied = 0
        for (ax_key, tr_key), style in styles.items():
            tr = (axes.get(ax_key, {}) or {}).get("traces", {}).get(tr_key)
            if tr is None:
                continue
            tr.update(copy.deepcopy(style))
            applied += 1
        self._reload_views()
        self._mark_modified()
        self.struct_status.setText(f"Applied formatting to {applied} trace(s)")

    # -- left: embedded matplotlib (rebuilt on every render) -------------------
    def _build_plot_panel(self):
        self._plot_container = QWidget()
        outer = QVBoxLayout(self._plot_container)
        outer.setContentsMargins(0, 0, 0, 0)

        # Main graph (top) and FFT (bottom) live in separate canvases inside a
        # splitter, so the FFT panel can be dragged to any size and hidden when
        # off. self._plot_layout / self._fft_layout host the toolbar + canvas.
        self.plot_split = QSplitter(Qt.Vertical)

        self._main_holder = QWidget()
        self._plot_layout = QVBoxLayout(self._main_holder)
        self._plot_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_split.addWidget(self._main_holder)

        self._fft_holder = QWidget()
        self._fft_layout = QVBoxLayout(self._fft_holder)
        self._fft_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_split.addWidget(self._fft_holder)
        self._fft_holder.setVisible(False)

        self.plot_split.setStretchFactor(0, 3)
        self.plot_split.setStretchFactor(1, 1)
        self.plot_split.setSizes([560, 240])
        outer.addWidget(self.plot_split)
        return self._plot_container

    # -- left: analysis (stats / fit / FFT) -----------------------------------
    def _build_analysis_sidebar(self):
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(4)

        hdr = QLabel("ANALYSIS"); hdr.setObjectName("sectionHeader")
        v.addWidget(hdr)

        self.an_combo = QComboBox()
        self.an_combo.currentIndexChanged.connect(self._an_select)
        v.addWidget(self.an_combo)

        # Statistics
        st = QLabel("STATISTICS"); st.setObjectName("sectionHeader")
        v.addWidget(st)
        self.stats_tree = QTreeWidget()
        self.stats_tree.setColumnCount(2)
        self.stats_tree.setHeaderLabels(["Metric", "Value"])
        self.stats_tree.setRootIsDecorated(False)
        self.stats_tree.setMaximumHeight(190)
        v.addWidget(self.stats_tree)

        # Curve fit
        cf = QLabel("CURVE FIT"); cf.setObjectName("sectionHeader")
        v.addWidget(cf)
        fitrow = QFormLayout(); fitrow.setContentsMargins(0, 0, 0, 0)
        self.fit_model = QComboBox()
        self.fit_model.addItems(["Linear", "Polynomial"] + [k for k in FIT_MODELS if k != "Linear"])
        self.fit_model.currentTextChanged.connect(self._on_fit_model_changed)
        fitrow.addRow("Model", self.fit_model)
        self.fit_degree = QSpinBox(); self.fit_degree.setRange(1, 12); self.fit_degree.setValue(2)
        self.fit_degree.setEnabled(False)        # only used by the Polynomial model
        fitrow.addRow("Degree", self.fit_degree)
        v.addLayout(fitrow)
        fbtns = QHBoxLayout(); fbtns.setContentsMargins(0, 0, 0, 0)
        self.fit_btn = QPushButton("Fit"); self.fit_btn.setObjectName("miniButton")
        self.fit_btn.clicked.connect(self._do_fit)
        self.fit_clear_btn = QPushButton("Clear"); self.fit_clear_btn.setObjectName("miniButton")
        self.fit_clear_btn.clicked.connect(self._clear_fit)
        fbtns.addWidget(self.fit_btn); fbtns.addWidget(self.fit_clear_btn)
        self.fit_show = QCheckBox("Show fit"); self.fit_show.setChecked(True)
        self.fit_show.toggled.connect(self._on_fit_show_toggled)
        fbtns.addWidget(self.fit_show); fbtns.addStretch(1)
        v.addLayout(fbtns)
        self.fit_result_label = QLabel(""); self.fit_result_label.setObjectName("welcomeHint")
        self.fit_result_label.setWordWrap(True)
        v.addWidget(self.fit_result_label)

        # FFT
        ft = QLabel("FFT"); ft.setObjectName("sectionHeader")
        v.addWidget(ft)
        self.fft_check = QCheckBox("Show FFT panel")
        self.fft_check.toggled.connect(self._on_fft_toggle)
        v.addWidget(self.fft_check)
        fftform = QFormLayout(); fftform.setContentsMargins(0, 0, 0, 0)
        self.fft_window = QComboBox(); self.fft_window.addItems(FFT_WINDOWS)
        self.fft_window.setCurrentText("Hann")
        self.fft_window.currentTextChanged.connect(self._on_fft_param_changed)
        fftform.addRow("Window", self.fft_window)
        self.fft_db = QCheckBox("magnitude in dB"); self.fft_db.setChecked(True)
        self.fft_db.toggled.connect(self._on_fft_param_changed)
        fftform.addRow("", self.fft_db)
        self.fft_resample = QCheckBox("uniform resample")
        self.fft_resample.toggled.connect(self._on_fft_param_changed)
        fftform.addRow("", self.fft_resample)
        self.fft_markers = QCheckBox("dotted line + markers")
        self.fft_markers.toggled.connect(self._on_fft_param_changed)
        fftform.addRow("", self.fft_markers)
        self.fft_from = QLineEdit(); self.fft_from.setValidator(QDoubleValidator())
        self.fft_from.setPlaceholderText("min")
        self.fft_from.editingFinished.connect(self._on_fft_param_changed)
        fftform.addRow("From X", self.fft_from)
        self.fft_to = QLineEdit(); self.fft_to.setValidator(QDoubleValidator())
        self.fft_to.setPlaceholderText("max")
        self.fft_to.editingFinished.connect(self._on_fft_param_changed)
        fftform.addRow("To X", self.fft_to)
        v.addLayout(fftform)
        rng = QHBoxLayout(); rng.setContentsMargins(0, 0, 0, 0)
        self.fft_reset_btn = QPushButton("Full range"); self.fft_reset_btn.setObjectName("miniButton")
        self.fft_reset_btn.clicked.connect(self._reset_fft_range)
        self.fft_export_btn = QPushButton("Export FFT → GrAF"); self.fft_export_btn.setObjectName("miniButton")
        self.fft_export_btn.clicked.connect(self._export_fft_graf)
        rng.addWidget(self.fft_reset_btn); rng.addWidget(self.fft_export_btn); rng.addStretch(1)
        v.addLayout(rng)
        self.fft_status = QLabel(""); self.fft_status.setObjectName("welcomeHint")
        self.fft_status.setWordWrap(True)
        v.addWidget(self.fft_status)

        v.addStretch(1)
        if mplcursors is None or _scipy_curve_fit is None:
            miss = []
            if mplcursors is None:
                miss.append("mplcursors (cursors)")
            if _scipy_curve_fit is None:
                miss.append("scipy (most fits / Flattop window)")
            note = QLabel("Optional: pip install " +
                          ", ".join(m.split()[0] for m in miss))
            note.setObjectName("welcomeHint"); note.setWordWrap(True)
            v.addWidget(note)
        return panel

    # -- analysis: trace selection + stats ------------------------------------
    def _analysis_traces(self):
        return [it for it in enumerate_data_items(self.packet) if it["kind"] == "trace"]

    def _current_analysis_trace(self):
        traces = self._analysis_traces()
        i = self.an_combo.currentIndex()
        if 0 <= i < len(traces):
            return traces[i]
        return None

    def _refresh_analysis(self):
        """Re-enumerate traces, refresh stats; keep selection where possible."""
        traces = self._analysis_traces()
        cur = self.an_combo.currentIndex()
        self.an_combo.blockSignals(True)
        self.an_combo.clear()
        for it in traces:
            self.an_combo.addItem(it["label"])
        if not traces:
            self.an_combo.addItem("(no traces)")
            self.an_combo.setEnabled(False)
        else:
            self.an_combo.setEnabled(True)
            self.an_combo.setCurrentIndex(min(max(cur, 0), len(traces) - 1))
        self.an_combo.blockSignals(False)
        self._update_stats()

    def _update_stats(self):
        self.stats_tree.clear()
        item = self._current_analysis_trace()
        if item is None:
            return
        stats = trace_statistics(self._as_list(item["node"], "y_data"))
        if stats is None:
            QTreeWidgetItem(self.stats_tree, ["(no finite Y data)", ""])
            return
        for name, val in stats:
            QTreeWidgetItem(self.stats_tree, [name, val])
        self.stats_tree.resizeColumnToContents(0)

    def _an_select(self, _idx):
        self._clear_fit(rerender=False)
        self._reset_fft_range(rerender=False)
        self._update_stats()
        if self._fft_on:
            self._rebuild_fft()
            self._attach_cursors()

    # -- curve fit -------------------------------------------------------------
    def _on_fit_model_changed(self, name):
        self.fit_degree.setEnabled(name == "Polynomial")

    def _do_fit(self):
        item = self._current_analysis_trace()
        if item is None:
            self.fit_result_label.setText("No trace selected.")
            return
        node = item["node"]
        x = self._as_list(node, "x_data")
        y = self._as_list(node, "y_data")
        try:
            params, r2, xfit, yfit, label = fit_trace(
                x, y, self.fit_model.currentText(), self.fit_degree.value())
        except Exception as exc:
            self.fit_result_label.setText(f"Fit failed: {exc}")
            return
        self._fit_result = {"ax_key": item.get("axis"), "x": xfit, "y": yfit, "label": label}
        txt = "  ".join(f"{n}={v:.5g}" for n, v in params)
        self.fit_result_label.setText(f"{label}:  {txt}\nR² = {r2:.5f}")
        self.fit_show.blockSignals(True)
        self.fit_show.setChecked(True)          # a fresh fit is shown by default
        self.fit_show.blockSignals(False)
        self._show_current()

    def _on_fit_show_toggled(self, _on):
        self._show_current()

    def _clear_fit(self, rerender=True):
        had = self._fit_result is not None
        self._fit_result = None
        self.fit_result_label.setText("")
        if had and rerender:
            self._show_current()

    # -- FFT -------------------------------------------------------------------
    def _fft_range(self):
        def parse(le):
            t = le.text().strip()
            try:
                return float(t)
            except ValueError:
                return None
        return parse(self.fft_from), parse(self.fft_to)

    def _on_fft_toggle(self, on):
        self._fft_on = on
        if on:
            self._rebuild_fft()
            self._show_fft_holder(True)
        else:
            self._show_fft_holder(False)
            self._clear_fft_figure()
        self._attach_cursors()

    def _on_fft_param_changed(self, *_):
        if self._fft_on:
            self._rebuild_fft()
            self._attach_cursors()

    def _rebuild_fft(self):
        """Rebuild only the FFT canvas (not the main graph)."""
        try:
            self._set_fft_figure(self._build_fft_figure())
        except Exception as exc:
            self.fft_status.setText(f"FFT error: {exc}")
            self._show_fft_holder(False)

    def _reset_fft_range(self, rerender=True):
        self.fft_from.blockSignals(True); self.fft_to.blockSignals(True)
        self.fft_from.clear(); self.fft_to.clear()
        self.fft_from.blockSignals(False); self.fft_to.blockSignals(False)
        if rerender and self._fft_on:
            self._rebuild_fft()
            self._attach_cursors()

    def _export_fft_graf(self):
        """Compute the current FFT and write it out as a new .graf file, then
        open it as its own tab."""
        item = self._current_analysis_trace()
        if item is None:
            self.fft_status.setText("Select a trace first.")
            return
        node = item["node"]
        try:
            freq, mag, info, uniform = compute_fft(
                self._as_list(node, "x_data"), self._as_list(node, "y_data"),
                window=self.fft_window.currentText(), db=self.fft_db.isChecked(),
                xmin=self._fft_range()[0], xmax=self._fft_range()[1],
                resample=self.fft_resample.isChecked())
        except Exception as exc:
            QMessageBox.warning(self, "FFT failed", f"Could not compute the FFT:\n\n{exc}")
            return
        try:
            packet = self._build_fft_packet(freq, mag, item, uniform)
        except Exception as exc:
            QMessageBox.warning(self, "Could not build GrAF", str(exc))
            return
        host = self.window()
        start = getattr(host, "_last_dir", "") or ""
        default = os.path.join(start, f"{self.path.stem}_fft.graf")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export FFT as GrAF", default, "GrAF files (*.graf)")
        if not path:
            return
        if not path.lower().endswith(".graf"):
            path += ".graf"
        try:
            write_packet_as_graf(packet, path,
                                 action=f"FFT of '{item['name'] or item['trace']}' "
                                        f"from {self.path.name}",
                                 source_format="graf_explorer_fft")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write {os.path.basename(path)}:\n\n{exc}")
            return
        if hasattr(host, "_remember_dir"):
            host._remember_dir(path)
        if hasattr(host, "open_one"):
            host.open_one(path)
        self.fft_status.setText(f"Exported FFT → {os.path.basename(path)}")

    def _build_fft_packet(self, freq, mag, item, uniform):
        """Assemble a one-axis, one-trace GrAF packet holding the spectrum, using
        the current file's structure as a template."""
        base = copy.deepcopy(self.packet)
        axes = base.get("axes", {}) or {}
        if not axes:
            raise RuntimeError("this file has no axes to use as a template")
        ax_key = item.get("axis") if item.get("axis") in axes else next(iter(axes))
        axis = axes[ax_key]
        base["axes"] = {ax_key: axis}
        src_traces = axis.get("traces", {}) or {}
        template = src_traces.get(item.get("trace")) or next(iter(src_traces.values()), None)
        if template is None:
            raise RuntimeError("no template trace available to base the FFT on")
        tr = copy.deepcopy(template)
        axis["traces"] = {}
        if "surfaces" in axis:
            axis["surfaces"] = {}

        tr["x_data"] = [float(f) for f in freq]
        tr["y_data"] = [float(m) for m in mag]
        tr["z_data"] = []
        tr["use_yaxis_R"] = False
        tr["display_name"] = f"FFT of {item['name'] or item['trace']}"
        tr["include_in_legend"] = True
        if self.fft_markers.isChecked():
            tr["line_type"] = ":"; tr["marker_type"] = "."
            tr["marker_size"] = 4; tr["line_width"] = 1
        else:
            tr["line_type"] = "-"; tr["marker_type"] = "None"; tr["line_width"] = 1.2
        axis["traces"]["Tr0"] = tr

        if isinstance(axis.get("x_axis"), dict):
            axis["x_axis"]["label"] = "Frequency" + ("" if uniform else " (per sample)")
        if isinstance(axis.get("y_axis_L"), dict):
            axis["y_axis_L"]["label"] = "Magnitude (dB)" if self.fft_db.isChecked() else "Magnitude"
        if isinstance(axis.get("y_axis_R"), dict):
            axis["y_axis_R"]["is_valid"] = False
        if "title" in axis:
            axis["title"] = ""
        if "supertitle" in base:
            base["supertitle"] = "FFT"
        rescale_axis_to_data(axis, freq, mag)
        return base

    # -- right: structure + trace data ----------------------------------------
    def _build_sidebar(self):
        side = QSplitter(Qt.Vertical)

        # top — file structure
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(4, 0, 0, 0)
        header = QLabel("FILE STRUCTURE")
        header.setObjectName("sectionHeader")

        ctrl = QWidget()
        ch = QHBoxLayout(ctrl)
        ch.setContentsMargins(0, 0, 0, 0)
        self.show_attrs_cb = QCheckBox("Show attributes")
        self.show_attrs_cb.setChecked(True)
        self.show_attrs_cb.toggled.connect(self._apply_attr_filter)
        exp = QPushButton("Expand all"); exp.setObjectName("miniButton")
        exp.clicked.connect(self.tree_expand_all)
        col = QPushButton("Collapse all"); col.setObjectName("miniButton")
        col.clicked.connect(self.tree_collapse_all)
        ch.addWidget(self.show_attrs_cb)
        ch.addStretch(1)
        ch.addWidget(exp)
        ch.addWidget(col)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Name", "Type", "Shape", "Value / dtype"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_click)
        self._populate_structure()

        self.struct_status = QLabel("")
        self.struct_status.setObjectName("welcomeHint")
        self.struct_status.setWordWrap(True)

        tl.addWidget(header)
        tl.addWidget(ctrl)
        tl.addWidget(self.tree)
        tl.addWidget(self.struct_status)

        # bottom — trace data
        bot = QWidget()
        bl = QVBoxLayout(bot)
        bl.setContentsMargins(4, 0, 0, 0)
        header2 = QLabel("TRACE DATA")
        header2.setObjectName("sectionHeader")
        self.combo = QComboBox()
        self.items = enumerate_data_items(self.packet)
        for it in self.items:
            self.combo.addItem(it["label"])
        if not self.items:
            self.combo.addItem("(no traces or surfaces)")
            self.combo.setEnabled(False)
        self.combo.currentIndexChanged.connect(self._on_select)

        selrow = QWidget()
        sr = QHBoxLayout(selrow); sr.setContentsMargins(0, 0, 0, 0)
        self.visible_cb = QCheckBox("Visible")
        self.visible_cb.setChecked(True)
        self.visible_cb.toggled.connect(self._on_visible_toggled)
        sr.addWidget(self.combo, 1)
        sr.addWidget(self.visible_cb)

        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(22)

        rowbtns = QWidget()
        rb = QHBoxLayout(rowbtns)
        rb.setContentsMargins(0, 0, 0, 0)
        self.add_btn = QPushButton("Add row"); self.add_btn.setObjectName("miniButton")
        self.add_btn.clicked.connect(self._add_row)
        self.del_btn = QPushButton("Remove row"); self.del_btn.setObjectName("miniButton")
        self.del_btn.clicked.connect(self._remove_row)
        rb.addWidget(self.add_btn)
        rb.addWidget(self.del_btn)
        rb.addStretch(1)

        self.row_label = QLabel("")
        self.row_label.setObjectName("welcomeHint")

        bl.addWidget(header2)
        bl.addWidget(selrow)
        bl.addWidget(self.table)
        bl.addWidget(rowbtns)
        bl.addWidget(self.row_label)

        if self.items:
            self._on_select(0)

        side.addWidget(top)
        side.addWidget(bot)
        side.setStretchFactor(0, 1)
        side.setStretchFactor(1, 1)
        side.setSizes([380, 340])
        return side

    # -- info / conditions panel ----------------------------------------------
    def _build_info_panel(self):
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(4)
        hdr = QLabel("INFO"); hdr.setObjectName("sectionHeader")
        v.addWidget(hdr)

        v.addWidget(QLabel("Description"))
        self.info_desc = QLabel("")
        self.info_desc.setObjectName("welcomeHint")
        self.info_desc.setWordWrap(True)
        self.info_desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.info_desc)

        sub = QLabel("CONDITIONS"); sub.setObjectName("sectionHeader")
        v.addWidget(sub)
        self.cond_tree = QTreeWidget()
        self.cond_tree.setColumnCount(2)
        self.cond_tree.setHeaderLabels(["Key", "Value"])
        self.cond_tree.setRootIsDecorated(False)
        v.addWidget(self.cond_tree, 1)
        return panel

    # -- provenance panel ------------------------------------------------------
    def _build_provenance_panel(self):
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(4)
        hdr = QLabel("PROVENANCE"); hdr.setObjectName("sectionHeader")
        v.addWidget(hdr)

        v.addWidget(QLabel("Creation"))
        self.prov_tree = QTreeWidget()
        self.prov_tree.setColumnCount(2)
        self.prov_tree.setHeaderLabels(["Field", "Value"])
        self.prov_tree.setRootIsDecorated(False)
        self.prov_tree.setMaximumHeight(240)
        v.addWidget(self.prov_tree)

        v.addWidget(QLabel("Modification history"))
        self.hist_tree = QTreeWidget()
        self.hist_tree.setColumnCount(2)
        self.hist_tree.setHeaderLabels(["Action", "When (UTC)"])
        self.hist_tree.setRootIsDecorated(True)
        v.addWidget(self.hist_tree, 1)
        return panel

    def _refresh_metadata(self):
        """Repopulate the Info and Provenance panels from the current packet."""
        if not hasattr(self, "cond_tree"):
            return
        info = self.packet.get("info") if isinstance(self.packet, dict) else None
        info = info if isinstance(info, dict) else {}

        desc = info.get("description", "")
        self.info_desc.setText(str(desc) if str(desc).strip() else "(no description)")

        self.cond_tree.clear()
        conds = info.get("conditions")
        if isinstance(conds, dict) and conds:
            for k, val in conds.items():
                QTreeWidgetItem(self.cond_tree, [str(k), _fmt_meta_value(val)])
        else:
            QTreeWidgetItem(self.cond_tree, ["(none)", ""])
        self.cond_tree.resizeColumnToContents(0)

        self.prov_tree.clear()
        prov = info.get("provenance")
        if isinstance(prov, dict) and prov:
            keys = [k for k in _PROV_ORDER if k in prov] + \
                   [k for k in prov if k not in _PROV_ORDER]
            for k in keys:
                QTreeWidgetItem(self.prov_tree, [str(k), _fmt_meta_value(prov[k])])
        else:
            QTreeWidgetItem(self.prov_tree, ["(not stamped yet)", ""])
        self.prov_tree.resizeColumnToContents(0)

        self.hist_tree.clear()
        hist = info.get("history")
        if isinstance(hist, list) and hist:
            for i, entry in enumerate(hist):
                if not isinstance(entry, dict):
                    continue
                top = QTreeWidgetItem(self.hist_tree, [
                    f"{i + 1}. {entry.get('action', '?')}",
                    str(entry.get("utc", ""))])
                for k in ("by", "app", "content_sha256"):
                    if k in entry:
                        QTreeWidgetItem(top, [k, _fmt_meta_value(entry[k])])
        else:
            QTreeWidgetItem(self.hist_tree, ["(no history)", ""])
        self.hist_tree.resizeColumnToContents(0)

    def _build_metadata_panel(self):
        """A dedicated, toggle-able panel holding the Info and Provenance views
        in a vertical splitter (each independently resizable)."""
        split = QSplitter(Qt.Vertical)
        split.addWidget(self._build_info_panel())
        split.addWidget(self._build_provenance_panel())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 420])
        self.metadata_split = split
        return split

    def set_metadata_visible(self, visible):
        self.metadata_visible = visible
        if not hasattr(self, "metadata"):
            return
        self.metadata.setVisible(visible)
        if visible:
            sizes = self.hsplit.sizes()
            if sizes and sizes[-1] < 50:        # first reveal → borrow from the plot
                spare = sizes[1] + sizes[-1]
                sizes[-1] = 320
                sizes[1] = max(250, spare - 320)
                self.hsplit.setSizes(sizes)
            self._refresh_metadata()

    def set_provenance_color(self, color):
        self._provenance_color = color
        if not hasattr(self, "tree"):
            return
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if self._is_provenance_path(ch.data(0, Qt.UserRole)):
                    self._color_provenance(ch)
                walk(ch)
        # setForeground emits itemChanged; guard it so recolouring is never
        # mistaken for a user edit of a provenance row.
        self._building = True
        self.tree.blockSignals(True)
        try:
            for i in range(self.tree.topLevelItemCount()):
                walk(self.tree.topLevelItem(i))
        finally:
            self.tree.blockSignals(False)
            self._building = False

    def set_allow_provenance_edits(self, allow):
        self._allow_provenance_edits = allow
        # rebuild the tree so editability flags on protected rows update
        if hasattr(self, "tree"):
            self._populate_structure()

    # -- structure tree --------------------------------------------------------
    def _populate_structure(self):
        self._building = True
        self.tree.blockSignals(True)
        self.tree.clear()
        self._invalid_items.clear()
        root = QTreeWidgetItem(self.tree, [self.path.name, "Root", "", ""])
        root.setData(0, Qt.UserRole, None)
        self._build_node(root, self.packet, [])
        root.setExpanded(True)
        for i in range(root.childCount()):
            root.child(i).setExpanded(True)
        self.tree.blockSignals(False)
        self._building = False
        self._apply_attr_filter()
        for c in range(4):
            self.tree.resizeColumnToContents(c)

    def _build_node(self, parent_item, container, path):
        for key, value in container.items():
            self._add_item(parent_item, str(key), value, path + [key])

    @staticmethod
    def _is_provenance_path(path):
        """True for any node inside info/provenance or info/history."""
        return (isinstance(path, list) and len(path) >= 2
                and path[0] == "info" and path[1] in ("provenance", "history"))

    def _color_provenance(self, item):
        try:
            brush = QBrush(QColor(self._provenance_color))
            for c in range(4):
                item.setForeground(c, brush)
        except Exception:
            pass

    def _add_item(self, parent, name, value, path):
        ttype, shape, disp = classify_value(value)
        item = QTreeWidgetItem(parent, [name, ttype, shape, disp])
        item.setData(0, Qt.UserRole, path)
        protected = self._is_provenance_path(path)
        if protected:
            self._color_provenance(item)
        if ttype == "Group":
            self._build_node(item, value, path)
        elif ttype == "Dataset":
            if is_expandable_array(value):
                for i, elem in enumerate(value):
                    cpath = path + [i]
                    child = QTreeWidgetItem(item, [f"[{i}]", "item", "", format_scalar(elem)])
                    child.setData(0, Qt.UserRole, cpath)
                    cprot = self._is_provenance_path(cpath)
                    if cprot:
                        self._color_provenance(child)
                    if (not cprot) or self._allow_provenance_edits:
                        child.setFlags(child.flags() | Qt.ItemIsEditable)
            # large / multi-dim arrays stay as a non-editable summary leaf
        else:  # attr scalar — editable unless it's protected provenance
            if (not protected) or self._allow_provenance_edits:
                item.setFlags(item.flags() | Qt.ItemIsEditable)
        return item

    def _apply_attr_filter(self, *_):
        show = self.show_attrs_cb.isChecked()

        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.text(1) == "attr":
                    ch.setHidden(not show)
                walk(ch)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def tree_expand_all(self):
        self.tree.expandAll()

    def tree_collapse_all(self):
        self.tree.collapseAll()
        if self.tree.topLevelItemCount():
            self.tree.topLevelItem(0).setExpanded(True)

    def _on_tree_double_click(self, item, column):
        if self.locked:
            return
        if item.flags() & Qt.ItemIsEditable:
            self.tree.editItem(item, 3)     # always edit the Value column

    def _on_tree_item_changed(self, item, column):
        if self._building or column != 3:
            return
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        # Guard the whole handler: any setForeground/setText/setToolTip below
        # emits itemChanged again, which would otherwise re-enter and recurse.
        self._building = True
        try:
            name = item.text(0)
            try:
                cont, last = self._resolve_container(path)
                newv = coerce_like(cont[last], item.text(3))
            except Exception as exc:
                self._mark_tree_invalid(item)
                item.setToolTip(3, f"Could not parse value: {exc}")
                self.struct_status.setText(f"✗ {name}: invalid value ({exc})")
                self._mark_modified()
                return
            self._snapshot()                      # record undo point (valid edit only)
            cont[last] = newv
            ok, err = self._rerender_from_packet()
            self._mark_modified()
            if ok:
                item.setToolTip(3, "")
                self.struct_status.setText(f"✓ updated {name}")
                item.setText(3, self._display_at(path))   # show normalized value
                if isinstance(path, list) and path[:1] == ["info"]:
                    self._refresh_metadata()              # keep Info/Prov panels live
            else:
                self._mark_tree_invalid(item)
                item.setToolTip(3, f"Edit applied but render failed: {err}")
                self.struct_status.setText(f"✗ {name}: render failed — {err}")
        finally:
            self._building = False

    def _resolve_container(self, path):
        cont = self.packet
        for k in path[:-1]:
            if isinstance(cont, dict):
                cont = cont[k]
            elif isinstance(cont, (list, tuple)) and isinstance(k, int):
                cont = cont[k]
            else:
                raise KeyError(f"cannot descend into {type(cont).__name__} at {k!r}")
        last = path[-1]
        if isinstance(cont, dict):
            if last not in cont:
                raise KeyError(last)
        elif isinstance(cont, (list, tuple)):
            if not (isinstance(last, int) and -len(cont) <= last < len(cont)):
                raise KeyError(last)
        else:
            raise KeyError(f"{type(cont).__name__} is not editable at {last!r}")
        return cont, last

    def _display_at(self, path):
        cont = self.packet
        for k in path:
            cont = cont[k]
        return format_scalar(cont)

    def _mark_tree_invalid(self, item):
        self.tree.blockSignals(True)
        item.setForeground(3, QBrush(QColor(INVALID_RED)))
        self.tree.blockSignals(False)
        # keyed by id(): QTreeWidgetItem is unhashable in some PyQt5 builds
        self._invalid_items[id(item)] = item

    def _clear_tree_invalids(self):
        self.tree.blockSignals(True)
        for it in list(self._invalid_items.values()):
            try:
                it.setForeground(3, QBrush())
            except RuntimeError:
                pass
        self.tree.blockSignals(False)
        self._invalid_items.clear()

    # -- trace-data table ------------------------------------------------------
    def _as_list(self, node, key):
        """Return node[key] as a Python list, storing it back so edits stick."""
        v = node.get(key, [])
        if isinstance(v, list):
            return v
        if isinstance(v, np.ndarray):
            lst = v.tolist()
        elif isinstance(v, tuple):
            lst = list(v)
        elif v is None:
            lst = []
        elif hasattr(v, "__iter__") and not isinstance(v, (str, bytes)):
            lst = list(v)
        else:
            lst = []
        node[key] = lst
        return lst

    def _on_select(self, idx):
        if not self.items or idx < 0 or idx >= len(self.items):
            self._table_model = None
            self.table.setModel(None)
            self._current_is_trace = False
            self.row_label.setText("")
            self._sync_edit_state()
            return
        item = self.items[idx]
        kind, node = item["kind"], item["node"]
        self._current_is_trace = (kind == "trace")

        if kind == "trace":
            cols = [("X", self._as_list(node, "x_data")),
                    ("Y", self._as_list(node, "y_data"))]
            z = self._as_list(node, "z_data")
            if len(z) > 0:
                cols.append(("Z", z))
            model = TraceTableModel(cols, editable=False,
                                    on_commit=self._commit_data_edit,
                                    on_before_edit=self._snapshot)
        else:
            cols = [("X", list(np.asarray(node.get("x_grid", []), dtype=float).ravel())),
                    ("Y", list(np.asarray(node.get("y_grid", []), dtype=float).ravel())),
                    ("Z", list(np.asarray(node.get("z_grid", []), dtype=float).ravel()))]
            model = TraceTableModel(cols, editable=False, on_commit=None)

        self._table_model = model
        self.table.setModel(model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.row_label.setText(f"{model.rowCount()} points  ·  {len(cols)} columns  ·  {kind}")
        self._sync_visible_cb(item)
        self._sync_edit_state()

    def _sync_visible_cb(self, item):
        """Reflect the selected item's visibility in the checkbox. Only traces
        can be hidden; for surfaces the checkbox is disabled."""
        self.visible_cb.blockSignals(True)
        if item.get("kind") == "trace":
            key = (item.get("axis"), item.get("trace"))
            self.visible_cb.setEnabled(True)
            self.visible_cb.setChecked(key not in self._hidden_traces)
        else:
            self.visible_cb.setEnabled(False)
            self.visible_cb.setChecked(True)
        self.visible_cb.blockSignals(False)

    def _on_visible_toggled(self, visible):
        idx = self.combo.currentIndex()
        if not self.items or idx < 0 or idx >= len(self.items):
            return
        item = self.items[idx]
        if item.get("kind") != "trace":
            return
        key = (item.get("axis"), item.get("trace"))
        if visible:
            self._hidden_traces.discard(key)
        else:
            self._hidden_traces.add(key)
        # annotate the combo entry so hidden traces are visible at a glance
        base = item["label"]
        self.combo.setItemText(idx, base + ("" if visible else "   — hidden"))
        self._show_current()

    def _commit_data_edit(self, r, c):
        self._mark_modified()
        self._clear_fit(rerender=False)
        ok, _ = self._rerender_from_packet()
        self._update_stats()
        return ok

    def _add_row(self):
        if self.locked or self._table_model is None or not self._current_is_trace:
            return
        self._snapshot()
        self._table_model.add_row()
        self._mark_modified()
        self._clear_fit(rerender=False)
        self._rerender_from_packet()
        self._refresh_structure_preserving()
        self._update_stats()

    def _remove_row(self):
        if self.locked or self._table_model is None or not self._current_is_trace:
            return
        r = self.table.currentIndex().row()
        if r < 0:
            r = self._table_model.rowCount() - 1
        if r < 0:
            return
        self._snapshot()
        self._table_model.remove_row(r)
        self._mark_modified()
        self._clear_fit(rerender=False)
        self._rerender_from_packet()
        self._refresh_structure_preserving()
        self._update_stats()

    def _refresh_structure_preserving(self):
        # Row counts changed, so dataset shapes need refreshing. Rebuild the tree
        # (root + first level re-expanded). Deeper expansion is not preserved.
        self._populate_structure()

    # -- lock / edit gating ----------------------------------------------------
    def set_locked(self, locked):
        self.locked = locked
        self.lock_btn.setText("🔒  Locked" if locked else "🔓  Editing")
        self._sync_edit_state()

    def toggle_lock(self):
        self.set_locked(not self.locked)

    def set_sidebar_visible(self, visible):
        """Show/hide the structure + trace-data sidebar, preserving the split."""
        if visible:
            self.sidebar.setVisible(True)
            if self._saved_split_sizes:
                self.hsplit.setSizes(self._saved_split_sizes)
        else:
            if self.sidebar.isVisible():
                self._saved_split_sizes = self.hsplit.sizes()
            self.sidebar.setVisible(False)

    def _sync_edit_state(self):
        locked = self.locked
        editable_table = (not locked) and self._current_is_trace
        if self._table_model is not None:
            self._table_model.set_editable(editable_table)
        self.table.setEditTriggers(
            (QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
            if editable_table else QAbstractItemView.NoEditTriggers)
        self.add_btn.setEnabled(editable_table)
        self.del_btn.setEnabled(editable_table)
        self.format_btn.setEnabled(not locked)
        self.format_btn.setToolTip(FORMAT_BTN_TIP_LOCKED if locked else FORMAT_BTN_TIP)

    # -- undo / redo / revert --------------------------------------------------
    def _snapshot(self):
        """Push the current packet onto the undo stack (called before a change).
        A fresh change invalidates the redo history."""
        self._undo_stack.append(copy.deepcopy(self.packet))
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self):
        if not self._undo_stack:
            self.struct_status.setText("Nothing to undo")
            return
        self._redo_stack.append(copy.deepcopy(self.packet))   # current → redo
        self.packet = self._undo_stack.pop()
        self._reload_views()
        # Empty stack after popping ⇒ back at the as-loaded state.
        self.modified = bool(self._undo_stack)
        self.modifiedChanged.emit()
        self.struct_status.setText("Undid last change")

    def redo(self):
        if not self._redo_stack:
            self.struct_status.setText("Nothing to redo")
            return
        self._undo_stack.append(copy.deepcopy(self.packet))   # current → undo
        self.packet = self._redo_stack.pop()
        self._reload_views()
        self.modified = True
        self.modifiedChanged.emit()
        self.struct_status.setText("Redid last change")

    def revert(self):
        if not self.modified and not self._undo_stack and not self._redo_stack:
            self.struct_status.setText("Already at original")
            return
        resp = QMessageBox.question(
            self, "Revert to original?",
            f"Discard all changes to {self.path.name} and restore the "
            f"originally loaded contents?",
            QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
        if resp != QMessageBox.Discard:
            return
        self.packet = copy.deepcopy(self._original_packet)
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._reload_views()
        self.modified = False
        self.modifiedChanged.emit()
        self.struct_status.setText("Reverted to original")

    def _reload_views(self):
        """Rebuild every view from self.packet after a wholesale change."""
        self._populate_structure()
        cur = self.combo.currentIndex()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.items = enumerate_data_items(self.packet)
        # Drop visibility entries for traces that no longer exist.
        live = {(it["axis"], it["trace"]) for it in self.items if it["kind"] == "trace"}
        self._hidden_traces &= live
        for it in self.items:
            hidden = (it["kind"] == "trace"
                      and (it["axis"], it["trace"]) in self._hidden_traces)
            self.combo.addItem(it["label"] + ("   — hidden" if hidden else ""))
        if not self.items:
            self.combo.addItem("(no traces or surfaces)")
            self.combo.setEnabled(False)
        self.combo.blockSignals(False)
        if self.items:
            idx = min(max(cur, 0), len(self.items) - 1)
            self.combo.setCurrentIndex(idx)
            self._on_select(idx)
        else:
            self._on_select(-1)
        self._fit_result = None
        self._refresh_analysis()
        self._refresh_metadata()
        self._rerender_from_packet()

    # -- rendering -------------------------------------------------------------
    def _make_title(self):
        self._render_seq += 1
        return f"{self.path.name} [#{self._fig_id}.{self._render_seq}]"

    def _build_graf_from_packet(self):
        """Round-trip the packet through a temp TOME and reload, reusing the exact
        load path that already works (no reliance on pack/unpack symmetry)."""
        fd, tmp = tempfile.mkstemp(suffix=".graf")
        os.close(fd)
        try:
            dict_to_tome(copy.deepcopy(self.packet), tmp, show_detail=False)
            return load_graf_file(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _rerender_from_packet(self):
        try:
            g = self._build_graf_from_packet()
        except Exception as exc:
            return False, exc
        ok, err = self._show_current(graf=g)
        if ok:
            self.graf = g
            self._clear_tree_invalids()
        return ok, err

    def _show_current(self, graf=None):
        """Render the main graph into the top canvas. If the FFT panel is on,
        render the spectrum into the separate (resizable) bottom canvas. Returns
        (ok, err); a base-render failure keeps the previous figure, while an FFT
        failure still shows the main graph and reports why on the FFT status."""
        g = graf if graf is not None else self.graf
        sanitize_graf_text(g)               # decode b'...' tick labels before draw
        try:
            fig = g.to_fig(window_title=self._make_title())
        except Exception as exc:
            return False, exc
        self._apply_trace_visibility(fig)
        self._apply_fit_overlay(fig)
        self._apply_legend(fig)
        self._set_figure(fig)
        if self._fft_on:
            try:
                self._set_fft_figure(self._build_fft_figure())
                self._show_fft_holder(True)
            except Exception as exc:
                self.fft_status.setText(f"FFT error: {exc}")
                self._show_fft_holder(False)
        else:
            self._show_fft_holder(False)
            self._clear_fft_figure()
        self._attach_cursors()
        return True, None

    def _apply_trace_visibility(self, fig):
        """Hide the Line2D artists for any trace in self._hidden_traces. Trace
        plot order matches the packet's traces dict order, so the i-th line on
        axis k is the i-th trace of axis k. (Legend is handled separately.)"""
        if not self._hidden_traces:
            return
        try:
            axes = self.packet.get("axes", {}) or {}
            for ax_idx, (ax_key, ax) in enumerate(axes.items()):
                if ax_idx >= len(fig.axes):
                    break
                lines = fig.axes[ax_idx].get_lines()
                tr_keys = list((ax.get("traces", {}) or {}).keys())
                for tr_idx, tr_key in enumerate(tr_keys):
                    if tr_idx < len(lines) and (ax_key, tr_key) in self._hidden_traces:
                        lines[tr_idx].set_visible(False)
        except Exception:
            pass

    def _apply_fit_overlay(self, fig):
        if not self._fit_result or not self.fit_show.isChecked():
            return
        try:
            ax_keys = list(self.packet.get("axes", {}).keys())
            key = self._fit_result.get("ax_key")
            idx = ax_keys.index(key) if key in ax_keys else 0
            if idx < len(fig.axes):
                fig.axes[idx].plot(
                    self._fit_result["x"], self._fit_result["y"],
                    "--", color="#d1495b", linewidth=1.6,
                    label=f"fit: {self._fit_result['label']}", zorder=10)
        except Exception:
            pass

    def _apply_legend(self, fig):
        """Draw a legend on each axis from visible, named artists. graf itself
        never creates legends, so this is what surfaces trace display names (and
        the fit label); hidden traces are excluded."""
        if not getattr(self, "legend_on", True):
            return
        try:
            for ax in fig.axes:
                handles, labels = [], []
                for ln in ax.get_lines():
                    lab = ln.get_label()
                    if ln.get_visible() and lab and not lab.startswith("_"):
                        handles.append(ln)
                        labels.append(lab)
                existing = ax.get_legend()
                if handles:
                    ax.legend(handles, labels, loc="best", fontsize="small")
                elif existing is not None:
                    existing.remove()
        except Exception:
            pass

    def _build_fft_figure(self):
        """Standalone Figure for the FFT panel (its own canvas, so it can be
        resized independently of the main graph)."""
        item = self._current_analysis_trace()
        if item is None:
            raise ValueError("no trace selected for FFT")
        node = item["node"]
        freq, mag, info, uniform = compute_fft(
            self._as_list(node, "x_data"), self._as_list(node, "y_data"),
            window=self.fft_window.currentText(), db=self.fft_db.isChecked(),
            xmin=self._fft_range()[0], xmax=self._fft_range()[1],
            resample=self.fft_resample.isChecked())
        self.fft_status.setText(info)

        fig = Figure(figsize=(6.0, 2.4))
        ax = fig.add_subplot(111)
        if self.fft_markers.isChecked():
            ax.plot(freq, mag, linestyle=":", marker=".", markersize=4,
                    linewidth=1.0, color="#4b89dc")
        else:
            ax.plot(freq, mag, linestyle="-", linewidth=1.2, color="#4b89dc")
        ax.set_xlabel("Frequency" + ("" if uniform else " (per sample)"), fontsize="small")
        ax.set_ylabel("Mag (dB)" if self.fft_db.isChecked() else "Mag", fontsize="small")
        ax.set_title(f"FFT — {item['name'] or item['trace']}", fontsize="small")
        ax.tick_params(labelsize="small")
        ax.grid(True, alpha=0.3)
        try:
            fig.tight_layout()
        except Exception:
            pass
        return fig

    def _show_fft_holder(self, show):
        was = self._fft_holder.isVisible()
        self._fft_holder.setVisible(show)
        if show and not was:                    # first reveal → give it real height
            sizes = self.plot_split.sizes()
            if len(sizes) == 2 and sizes[1] < 40:
                total = sum(sizes) or 800
                self.plot_split.setSizes([int(total * 0.68), int(total * 0.32)])

    def _set_fft_figure(self, fig):
        while self._fft_layout.count():
            w = self._fft_layout.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        old = self.fft_fig
        self.fft_fig = fig
        self.fft_canvas = FigureCanvas(fig)     # standalone Figure: no pyplot manager
        self.fft_canvas.setAcceptDrops(False)
        toolbar = NavigationToolbar(self.fft_canvas, self)
        self._fft_layout.addWidget(toolbar)
        self._fft_layout.addWidget(self.fft_canvas)
        self.fft_canvas.draw_idle()
        if old is not None:
            try:
                old.clear()
            except Exception:
                pass

    def _clear_fft_figure(self):
        while self._fft_layout.count():
            w = self._fft_layout.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if self.fft_fig is not None:
            try:
                self.fft_fig.clear()
            except Exception:
                pass
        self.fft_fig = None
        self.fft_canvas = None

    def _attach_cursors(self):
        # Tear down all previous cursors first — otherwise disabling does nothing
        # and stale cursors linger on replaced canvases.
        for c in self._cursors:
            try:
                c.remove()
            except Exception:
                pass
        self._cursors = []
        if mplcursors is None or not self.cursors_enabled:
            return
        figs = []
        if self.fig is not None:
            figs.append(self.fig)
        if self._fft_on and self.fft_fig is not None:
            figs.append(self.fft_fig)
        for f in figs:
            cur = self._make_cursor(f)
            if cur is not None:
                self._cursors.append(cur)

    def _make_cursor(self, fig):
        try:
            lines = []
            for ax in fig.axes:
                lines.extend(ax.get_lines())
            if not lines:
                return None
            # Prefix datatips with the line's name when the plot has more than
            # one labelled line — multiple traces, or a trace plus its fit.
            named = [ln for ln in lines
                     if ln.get_label() and not ln.get_label().startswith("_")]
            multi = len(named) >= 2

            cur = mplcursors.cursor(lines, hover=False, multiple=True)

            @cur.connect("add")
            def _on_add(sel, _multi=multi):
                line = sel.artist
                # Snap to the nearest actual data vertex (mplcursors interpolates
                # along the segment otherwise). Compare in pixel space so the
                # differing X/Y scales don't bias the choice.
                x0, y0 = sel.target[0], sel.target[1]
                try:
                    xd = np.asarray(line.get_xdata(), dtype=float)
                    yd = np.asarray(line.get_ydata(), dtype=float)
                    if xd.size:
                        pts = line.axes.transData.transform(np.column_stack([xd, yd]))
                        tx, ty = line.axes.transData.transform((x0, y0))
                        i = int(np.argmin((pts[:, 0] - tx) ** 2 + (pts[:, 1] - ty) ** 2))
                        x0, y0 = float(xd[i]), float(yd[i])
                        sel.annotation.xy = (x0, y0)
                except Exception:
                    pass
                label = line.get_label() if line is not None else ""
                prefix = ""
                if _multi and label and not str(label).startswith("_"):
                    prefix = f"{label}\n"
                sel.annotation.set_text(f"{prefix}x = {x0:.6g}\ny = {y0:.6g}")
                try:
                    sel.annotation.get_bbox_patch().set(alpha=0.9)
                except Exception:
                    pass
            return cur
        except Exception:
            return None

    def set_cursors_enabled(self, enabled):
        self.cursors_enabled = enabled
        self._attach_cursors()

    def set_legend_on(self, enabled):
        self.legend_on = enabled
        self._show_current()

    def apply_tight_layout(self):
        """Re-fit axes into each canvas (Ctrl+R). Preserves zoom/pan; the FFT
        canvas is now independent, so no special-casing is needed."""
        for f, c in ((self.fig, self.canvas), (self.fft_fig, self.fft_canvas)):
            if f is None:
                continue
            try:
                f.tight_layout()
            except Exception:
                pass
            if c is not None:
                c.draw_idle()

    def set_analysis_visible(self, visible):
        self.analysis_visible = visible
        self.analysis.setVisible(visible)
        if visible:
            sizes = self.hsplit.sizes()
            if sizes and sizes[0] < 50:                 # was collapsed → give it room
                spare = sizes[0] + sizes[1]
                sizes[0] = 300
                sizes[1] = max(200, spare - 300)
                self.hsplit.setSizes(sizes)
            self._refresh_analysis()

    def _set_figure(self, fig):
        while self._plot_layout.count():
            w = self._plot_layout.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if self.fig is not None:
            try:
                plt.close(self.fig)
            except Exception:
                pass
        self.fig = fig
        self.canvas = FigureCanvas(fig)
        self.canvas.setAcceptDrops(False)
        toolbar = NavigationToolbar(self.canvas, self)
        self._plot_layout.addWidget(toolbar)
        self._plot_layout.addWidget(self.canvas)
        self.canvas.draw_idle()

    # -- modified flag / save --------------------------------------------------
    def _mark_modified(self):
        if not self.modified:
            self.modified = True
            self.modifiedChanged.emit()

    def save_as(self):
        start = str(self.path)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save GrAF As", start, "GrAF files (*.graf);;All files (*)")
        if not path:
            return False
        if not path.lower().endswith(".graf"):
            path += ".graf"
        try:
            action = "edited in GrAF Explorer" if self.modified else None
            g = write_packet_as_graf(self.packet, path, action=action)
            self._sync_provenance_from_graf(g)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write {path}\n\n{exc}")
            return False
        self.path = Path(path)
        self.modified = False
        self.modifiedChanged.emit()
        self._refresh_metadata()          # show the freshly appended history entry
        return True

    def _sync_provenance_from_graf(self, g):
        """Copy the provenance/history that write_graf just stamped onto `g` back
        into the in-memory packet, so the metadata panels stay current."""
        try:
            info = self.packet.setdefault("info", {})
            if hasattr(g, "info") and hasattr(g.info, "provenance"):
                info["provenance"] = copy.deepcopy(g.info.provenance)
            if hasattr(g, "info") and hasattr(g.info, "history"):
                info["history"] = copy.deepcopy(g.info.history)
        except Exception:
            pass

    # -- exports ---------------------------------------------------------------
    _FIG_FORMATS = {
        "png":    ("png",  "PNG image (*.png)"),
        "jpeg":   ("jpg",  "JPEG image (*.jpg *.jpeg)"),
        "svg":    ("svg",  "SVG vector (*.svg)"),
        "pdf":    ("pdf",  "PDF document (*.pdf)"),
        "pklfig": ("pklfig", "Pickled Matplotlib figure (*.pklfig)"),
    }
    _DATA_FORMATS = {
        "csv":  ("csv",  "CSV (*.csv)"),
        "json": ("json", "JSON (*.json)"),
    }

    @staticmethod
    def _ensure_ext(path, ext):
        return path if path.lower().endswith("." + ext.lower()) else path + "." + ext

    def export_figure(self, fmt):
        if self.fig is None:
            self.struct_status.setText("Nothing to export")
            return
        ext, filt = self._FIG_FORMATS[fmt]
        default = str(self.path.with_suffix("." + ext))
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export figure as {fmt.upper()}", default, filt)
        if not path:
            return
        path = self._ensure_ext(path, ext)
        try:
            if fmt == "pklfig":
                with open(path, "wb") as fh:
                    pickle.dump(self.fig, fh)
            else:
                save_kwargs = {"facecolor": self.fig.get_facecolor()}
                if fmt in ("png", "jpeg"):
                    save_kwargs["dpi"] = 200
                self.fig.savefig(path, format=fmt, **save_kwargs)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed",
                                 f"Could not export the figure:\n\n{exc}")
            return
        self.struct_status.setText(f"Exported figure → {os.path.basename(path)}")

    def export_data(self, fmt):
        data = collect_trace_data(self.packet)
        if not data:
            self.struct_status.setText("No trace data to export")
            return
        ext, filt = self._DATA_FORMATS[fmt]
        default = str(self.path.with_suffix("." + ext))
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export data as {fmt.upper()}", default, filt)
        if not path:
            return
        path = self._ensure_ext(path, ext)
        try:
            if fmt == "csv":
                self._write_csv(path, data)
            else:
                self._write_json(path, data)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed",
                                 f"Could not export the data:\n\n{exc}")
            return
        self.struct_status.setText(
            f"Exported {len(data)} trace(s) → {os.path.basename(path)}")

    def _write_csv(self, path, data):
        # Wide layout: each trace contributes X / Y (and Z if present) columns,
        # headed by its name and axis/trace id. Unequal lengths are blank-filled.
        columns = []   # list of (header, values)
        for d in data:
            tag = d["name"] or d["trace"]
            base = f"{tag} [{d['axis']}/{d['trace']}]"
            columns.append((f"{base} X", d["x"]))
            columns.append((f"{base} Y", d["y"]))
            if d["z"]:
                columns.append((f"{base} Z", d["z"]))
        nrows = max((len(v) for _, v in columns), default=0)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([h for h, _ in columns])
            for i in range(nrows):
                w.writerow([(v[i] if i < len(v) else "") for _, v in columns])

    def _write_json(self, path, data):
        payload = {"source_file": self.path.name,
                   "exported_by": "GrAF Explorer",
                   "traces": []}
        for d in data:
            entry = {"axis": d["axis"], "trace": d["trace"], "name": d["name"],
                     "x": d["x"], "y": d["y"]}
            if d["z"]:
                entry["z"] = d["z"]
            payload["traces"].append(entry)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=float)

    def close_figure(self):
        if self.fig is not None:
            try:
                plt.close(self.fig)
            except Exception:
                pass
            self.fig = None
        if self.fft_fig is not None:
            try:
                self.fft_fig.clear()
            except Exception:
                pass
            self.fft_fig = None


# ── Comparison / overlay tab ────────────────────────────────────────────────────
LEGEND_COL = 2                       # tree column holding the legend entry text
STYLE_COL = 3                        # column holding the per-trace format button


class _LegendColumnDelegate(QStyledItemDelegate):
    """Allows in-place editing in one column only (the legend entry), and only
    on trace rows -- the checkbox columns keep their normal click behaviour."""

    def __init__(self, column, parent=None):
        super().__init__(parent)
        self._column = column

    def createEditor(self, parent, option, index):
        if index.column() != self._column or not index.parent().isValid():
            return None
        return super().createEditor(parent, option, index)


class OverlayTab(QWidget):
    """Overlay selected traces from any number of open files on one plot.

    Each trace gets a checkbox (file → traces); the checked set is the
    visibility control for the comparison. Data is pulled live from the open
    FileTabs' packets, so 'Refresh files' re-reads them after edits."""

    def __init__(self, host):
        super().__init__()
        self.host = host                  # GrafExplorer (enumerates open files)
        self.fig = None
        self.canvas = None
        self._checked = set()             # {(uid, axis, trace)} kept across refresh
        self._bring_format = set()        # {(uid, axis, trace)} → use source format
        self._labels = {}                 # {(uid, axis, trace): custom legend text}
        self._styles = {}                 # {(uid, axis, trace): format overrides}
        self._seq = 0
        self._replot_timer = QTimer(self)
        self._replot_timer.setSingleShot(True)
        self._replot_timer.setInterval(40)
        self._replot_timer.timeout.connect(self._do_replot)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        split = QSplitter(Qt.Horizontal)

        self._plot_container = QWidget()
        self._plot_layout = QVBoxLayout(self._plot_container)
        self._plot_layout.setContentsMargins(0, 0, 0, 0)
        split.addWidget(self._plot_container)

        ctl = QWidget()
        cl = QVBoxLayout(ctl)
        cl.setContentsMargins(4, 0, 0, 0)
        hdr = QLabel("COMPARE TRACES")
        hdr.setObjectName("sectionHeader")
        cl.addWidget(hdr)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File / trace", "Fmt", "Legend entry", ""])
        self.tree.setRootIsDecorated(True)
        self.tree.setColumnWidth(0, 240)
        self.tree.header().setStretchLastSection(False)
        try:
            self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(3, QHeaderView.Fixed)
            self.tree.setColumnWidth(3, 34)
        except Exception:
            pass
        # Only the legend column is editable; a delegate refuses an editor
        # elsewhere so the checkbox columns keep their normal click behaviour.
        self.tree.setItemDelegate(_LegendColumnDelegate(LEGEND_COL, self.tree))
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked
                                  | QAbstractItemView.SelectedClicked
                                  | QAbstractItemView.EditKeyPressed)
        self.tree.itemChanged.connect(self._on_item_changed)
        cl.addWidget(self.tree, 1)

        hint = QLabel("First box shows the trace · “Fmt” keeps its own colour/style "
                      "(off ⇒ default formatting) · double-click a “Legend entry” "
                      "to rename it (blank ⇒ automatic) · the last column's "
                      "button edits that trace's line / marker formatting.")
        hint.setObjectName("welcomeHint"); hint.setWordWrap(True)
        cl.addWidget(hint)

        self.legend_cb = QCheckBox("Legend"); self.legend_cb.setChecked(True)
        self.legend_cb.toggled.connect(self._replot)
        self.grid_cb = QCheckBox("Grid"); self.grid_cb.setChecked(True)
        self.grid_cb.toggled.connect(self._replot)
        self.prefix_cb = QCheckBox("Prefix file name in automatic legend entries")
        self.prefix_cb.setChecked(True)
        self.prefix_cb.setToolTip("Automatic entries read “file · trace”; "
                                  "uncheck for the trace name alone. "
                                  "Renamed entries are left alone.")
        self.prefix_cb.toggled.connect(self._on_prefix_toggled)
        cl.addWidget(self.legend_cb)
        cl.addWidget(self.grid_cb)
        cl.addWidget(self.prefix_cb)

        legrow = QHBoxLayout(); legrow.setContentsMargins(0, 0, 0, 0)
        legrow.addWidget(QLabel("Legend position"))
        self.legend_loc = QComboBox()
        for loc in ("best", "upper right", "upper left", "lower left",
                    "lower right", "center right", "center left",
                    "upper center", "lower center"):
            self.legend_loc.addItem(loc)
        self.legend_loc.currentIndexChanged.connect(self._replot)
        legrow.addWidget(self.legend_loc, 1)
        cl.addLayout(legrow)

        btns = QHBoxLayout(); btns.setContentsMargins(0, 0, 0, 0)
        refresh = QPushButton("Refresh files"); refresh.setObjectName("miniButton")
        refresh.clicked.connect(self.refresh)
        clear = QPushButton("Uncheck all"); clear.setObjectName("miniButton")
        clear.clicked.connect(self._uncheck_all)
        reset = QPushButton("Reset legends"); reset.setObjectName("miniButton")
        reset.setToolTip("Drop every renamed legend entry and go back to the "
                         "automatic labels")
        reset.clicked.connect(self._reset_legends)
        reset_fmt = QPushButton("Reset formats"); reset_fmt.setObjectName("miniButton")
        reset_fmt.setToolTip("Drop every per-trace formatting override set here")
        reset_fmt.clicked.connect(self._reset_styles)
        btns.addWidget(refresh); btns.addWidget(clear); btns.addWidget(reset)
        btns.addWidget(reset_fmt)
        btns.addStretch(1)
        cl.addLayout(btns)

        self.make_btn = QPushButton("Make GrAF…"); self.make_btn.setObjectName("miniButton")
        self.make_btn.clicked.connect(self._make_graf)
        cl.addWidget(self.make_btn)

        self.status = QLabel(""); self.status.setObjectName("welcomeHint")
        self.status.setWordWrap(True)
        cl.addWidget(self.status)

        split.addWidget(ctl)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([820, 360])
        self.split = split
        outer.addWidget(split, 1)

    @staticmethod
    def _key(tr):
        """Identity of one overlay trace. Keyed on the source tab's uid rather
        than its file name, so two open files sharing a basename stay distinct."""
        return (tr.get("uid"), tr["axis"], tr["trace"])

    def _auto_label(self, tr):
        """The legend entry used when the user has not renamed this trace."""
        name = tr["name"] or tr["trace"]
        if self.prefix_cb.isChecked() and tr.get("file"):
            return f"{tr['file']} · {name}"
        return name

    def _label_for(self, tr):
        """The legend entry actually drawn: the user's text, else automatic."""
        custom = self._labels.get(self._key(tr), "")
        return custom if custom else self._auto_label(tr)

    def refresh(self, *args):
        """Rescan open files; rebuild the tree, preserving checked traces."""
        self.tree.blockSignals(True)
        self.tree.clear()
        files = self.host.file_tabs()
        names = unique_file_labels([tab.path for tab in files])
        for tab, fname in zip(files, names):
            fitem = QTreeWidgetItem(self.tree, [fname])
            fitem.setFlags(fitem.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            fitem.setToolTip(0, str(tab.path))
            fitem.setExpanded(True)
            for tr in overlay_traces_from_packet(tab.packet, fname, uid=tab.uid):
                label = f"{tr['trace']}   {tr['name']}".strip()
                key = self._key(tr)
                citem = QTreeWidgetItem(fitem, [label, "", self._label_for(tr)])
                citem.setFlags(citem.flags() | Qt.ItemIsUserCheckable
                               | Qt.ItemIsEditable)
                citem.setData(0, Qt.UserRole, tr)
                citem.setCheckState(0, Qt.Checked if key in self._checked else Qt.Unchecked)
                citem.setCheckState(1, Qt.Checked if key in self._bring_format else Qt.Unchecked)
                citem.setToolTip(1, "Keep this trace's own colour / line style")
                citem.setToolTip(LEGEND_COL, "Double-click to rename this legend "
                                             "entry (clear it for the automatic one)")
                btn = QPushButton()
                btn.setObjectName("iconButton")
                btn.setFixedSize(28, 20)
                apply_themed_icon(btn, "format.png", size=13)
                btn.setToolTip("Edit this trace's line / marker formatting")
                btn.clicked.connect(lambda _=False, t=tr: self._open_style_dialog(t))
                self.tree.setItemWidget(citem, STYLE_COL, btn)
        self.tree.blockSignals(False)
        if not files:
            self.status.setText("No files open. Open a .graf file, then Refresh.")
        self._replot()

    def _trace_index(self, tr):
        """Position of a trace among the checked ones (drives the default
        colour, so a trace keeps the colour it has in the plot)."""
        key = self._key(tr)
        for i, e in enumerate(self._iter_checked()):
            if self._key(e) == key:
                return i
        return 0

    def _effective_style(self, tr):
        """The formatting currently drawn for a trace: an explicit override if
        one was set here, else its source format, else the overlay default."""
        key = self._key(tr)
        if key in self._styles:
            style = dict(self._styles[key])
        elif key in self._bring_format:
            style = {k: tr.get(k) for k in
                     ("line_color", "line_type", "line_width", "marker_type",
                      "marker_size", "alpha") if tr.get(k) is not None}
            style.setdefault("marker_color", style.get("line_color"))
        else:
            style = {}
            cycle = default_color_cycle()
            apply_default_trace_format(style, cycle[self._trace_index(tr) % len(cycle)])
            style["line_width"] = 1.3
        style["has_error_bars"] = bool(tr.get("has_error_bars"))
        for k in ("err_line_color", "err_line_width", "err_cap_size",
                  "err_cap_color", "err_cap_width", "err_cap_visible"):
            if k not in style and tr.get(k) is not None:
                style[k] = tr[k]
        return style

    def _open_style_dialog(self, tr):
        """Per-trace format editor, opened from the button on its row."""
        key = self._key(tr)
        entry = {"key": key,
                 "label": self._label_for(tr),
                 "style": self._effective_style(tr)}

        had_override = key in self._styles
        previous = dict(self._styles[key]) if had_override else None

        def apply(styles):
            self._styles[key] = dict(styles[key])
            self._do_replot()

        dlg = TraceStyleDialog([entry], self, on_apply=apply,
                               title=f"Format — {self._label_for(tr)}")
        if dlg.exec_() != QDialog.Accepted:
            if had_override:
                self._styles[key] = previous
            else:
                self._styles.pop(key, None)    # back to Fmt / default formatting
            self._do_replot()

    def _reset_styles(self):
        self._styles.clear()
        self._replot()

    def _refresh_legend_column(self):
        """Re-render the Legend entry cells without rebuilding the tree."""
        self.tree.blockSignals(True)
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            fitem = root.child(i)
            for j in range(fitem.childCount()):
                citem = fitem.child(j)
                tr = citem.data(0, Qt.UserRole)
                if tr is not None:
                    citem.setText(LEGEND_COL, self._label_for(tr))
        self.tree.blockSignals(False)

    def _iter_checked(self):
        out = []
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            fitem = root.child(i)
            for j in range(fitem.childCount()):
                citem = fitem.child(j)
                if citem.checkState(0) == Qt.Checked:
                    tr = citem.data(0, Qt.UserRole)
                    if tr is not None:
                        out.append(tr)
        return out

    def _on_item_changed(self, item, col):
        tr = item.data(0, Qt.UserRole)
        if tr is not None:                # leaf (trace) item
            key = self._key(tr)
            if col == 0:
                if item.checkState(0) == Qt.Checked:
                    self._checked.add(key)
                else:
                    self._checked.discard(key)
            elif col == 1:
                if item.checkState(1) == Qt.Checked:
                    self._bring_format.add(key)
                else:
                    self._bring_format.discard(key)
            elif col == LEGEND_COL:
                text = item.text(LEGEND_COL).strip()
                if not text or text == self._auto_label(tr):
                    self._labels.pop(key, None)
                else:
                    self._labels[key] = text
                # Put the effective label back in the cell (an emptied cell
                # shows the automatic entry again).
                shown = self._label_for(tr)
                if item.text(LEGEND_COL) != shown:
                    self.tree.blockSignals(True)
                    item.setText(LEGEND_COL, shown)
                    self.tree.blockSignals(False)
        self._replot()

    def _on_prefix_toggled(self, *args):
        self._refresh_legend_column()
        self._replot()

    def _reset_legends(self):
        self._labels.clear()
        self._refresh_legend_column()
        self._replot()

    def _uncheck_all(self):
        self._checked.clear()
        self._bring_format.clear()
        self.refresh()

    def _replot(self, *args):
        self._replot_timer.start()        # debounce bursts (e.g. parent toggles)

    def _do_replot(self):
        sel = self._iter_checked()
        # A standalone Figure (not plt.figure) keeps this off pyplot's global
        # state, which is what caused intermittent missing ticks/legend when it
        # competed with the file tabs' pyplot figures.
        fig = Figure(figsize=(7.6, 5.0))
        ax = fig.add_subplot(111)
        n = 0
        xlabel = ylabel = ""
        for tr in sel:
            x, y = tr.get("x", []), tr.get("y", [])
            m = min(len(x), len(y))
            if m == 0:
                continue
            key = self._key(tr)
            if key in self._styles:
                kw = overlay_plot_kwargs(self._styles[key])
            elif key in self._bring_format:
                kw = overlay_plot_kwargs(tr)
            else:
                kw = {"linewidth": 1.3}
            label = self._label_for(tr)
            try:
                ax.plot(x[:m], y[:m], label=label, **kw)
            except Exception:
                ax.plot(x[:m], y[:m], linewidth=1.3, label=label)   # bad stored fmt
            n += 1
            if not xlabel and tr.get("xlabel"):
                xlabel = tr["xlabel"]
            if not ylabel and tr.get("ylabel"):
                ylabel = tr["ylabel"]
        ax.set_xlabel(xlabel or "X")
        ax.set_ylabel(ylabel or "Y")
        if self.grid_cb.isChecked():
            ax.grid(True, alpha=0.3)
        if self.legend_cb.isChecked():
            handles, labels = ax.get_legend_handles_labels()
            if labels:
                ax.legend(handles, labels, loc=self.legend_loc.currentText(),
                          fontsize="small")
        try:
            fig.tight_layout()
        except Exception:
            pass
        self._set_figure(fig)
        nfiles = len(self.host.file_tabs())
        self.status.setText(f"{n} trace(s) overlaid from {nfiles} open file(s)")

    def _make_graf(self):
        """Build a new single-axis GrAF from the checked traces and open it."""
        sel = self._iter_checked()
        if not sel:
            self.status.setText("Check at least one trace first.")
            return
        try:
            packet = self._build_comparison_packet(sel)
        except Exception as exc:
            QMessageBox.warning(self, "Could not build GrAF",
                                f"Failed to assemble the comparison:\n\n{exc}")
            return
        start = self.host._last_dir or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save comparison as GrAF", os.path.join(start, "comparison.graf"),
            "GrAF files (*.graf)")
        if not path:
            return
        if not path.lower().endswith(".graf"):
            path += ".graf"
        try:
            write_packet_as_graf(packet, path,
                                 action=f"comparison of {len(sel)} trace(s)",
                                 source_format="graf_explorer_comparison")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write {os.path.basename(path)}:\n\n{exc}")
            return
        self.host._remember_dir(path)
        self.host.open_one(path)          # opens the new file as its own tab
        self.status.setText(f"Made GrAF with {len(sel)} trace(s) → {os.path.basename(path)}")

    def _build_comparison_packet(self, sel):
        """Use the first selected trace's source file as a structural template,
        reduce it to one axis, and fill it with the selected traces."""
        template_tab = self.host.find_file_tab_by_uid(sel[0].get("uid"))
        if template_tab is None:
            raise RuntimeError("source file is no longer open")
        base = copy.deepcopy(template_tab.packet)
        axes = base.get("axes", {}) or {}
        if not axes:
            raise RuntimeError("template file has no axes")
        ax_key = sel[0]["axis"] if sel[0]["axis"] in axes else next(iter(axes))
        axis = axes[ax_key]
        base["axes"] = {ax_key: axis}          # single axis holds everything
        axis["traces"] = {}
        if "surfaces" in axis:
            axis["surfaces"] = {}
        if "supertitle" in base:
            base["supertitle"] = "Comparison"

        cycle = default_color_cycle()
        for i, entry in enumerate(sel):
            src_tab = self.host.find_file_tab_by_uid(entry.get("uid"))
            if src_tab is None:
                continue
            try:
                src = src_tab.packet["axes"][entry["axis"]]["traces"][entry["trace"]]
            except (KeyError, TypeError):
                continue
            tr = copy.deepcopy(src)
            key = self._key(entry)
            if key not in self._bring_format and key not in self._styles:
                apply_default_trace_format(tr, cycle[i % len(cycle)])
            if key in self._styles:
                tr.update(copy.deepcopy(self._styles[key]))
            tr["display_name"] = self._label_for(entry)
            tr["include_in_legend"] = True
            tr["use_yaxis_R"] = False         # everything shares the left axis
            axis["traces"][f"Tr{i}"] = tr

        # The stored Scale forces fixed limits/ticks on render, so recompute them
        # to span the combined data (otherwise traces from other files get clipped).
        xs, ys = [], []
        for e in sel:
            xs.extend(e.get("x", []))
            ys.extend(e.get("y", []))
        rescale_axis_to_data(axis, xs, ys)
        if isinstance(axis.get("y_axis_R"), dict):
            axis["y_axis_R"]["is_valid"] = False
        return base

    def _set_figure(self, fig):
        while self._plot_layout.count():
            w = self._plot_layout.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        old = self.fig
        self.fig = fig
        self.canvas = FigureCanvas(fig)        # standalone Figure: no pyplot manager
        toolbar = NavigationToolbar(self.canvas, self)
        self._plot_layout.addWidget(toolbar)
        self._plot_layout.addWidget(self.canvas)
        self.canvas.draw_idle()
        if old is not None:
            try:
                old.clear()
            except Exception:
                pass

    def close_figure(self):
        if self.fig is not None:
            try:
                self.fig.clear()
            except Exception:
                pass
            self.fig = None

    def apply_tight_layout(self):
        if self.fig is None:
            return
        try:
            self.fig.tight_layout()
        except Exception:
            pass
        if self.canvas is not None:
            self.canvas.draw_idle()


# ── Main window ────────────────────────────────────────────────────────────────
class GrafExplorer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GrAF Explorer")
        self.setWindowIcon(application_icon())
        self.resize(1320, 840)
        self.setAcceptDrops(True)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_welcome())

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_context_menu)
        self.tabs.tabBarDoubleClicked.connect(self._rename_tab)
        self.stack.addWidget(self.tabs)

        self.settings = QSettings()
        self._last_dir = self.settings.value("last_dir", "", type=str)
        self._load_prefs()
        self._build_menu()
        self._apply_theme()
        self._restore_geometry()
        self._sync_view()

    # -- settings persistence (QSettings: macOS plist / Windows registry) -------
    def _load_prefs(self):
        s = self.settings
        name = s.value("theme", DEFAULT_THEME, type=str)
        if name not in THEMES:
            name = DEFAULT_THEME
        fam = s.value("font_family", "Sans", type=str)
        if fam not in FONT_FAMILIES:
            fam = "Sans"
        try:
            size = int(s.value("font_size", THEMES[DEFAULT_THEME].base_pt))
        except (TypeError, ValueError):
            size = THEMES[DEFAULT_THEME].base_pt
        self._theme_name = name
        self._font_family_key = fam
        self.theme = replace(THEMES[name],
                             ui_family=FONT_FAMILIES[fam],
                             base_pt=max(9, min(22, size)))
        self._sidebar_visible = s.value("sidebar_visible", True, type=bool)
        self._analysis_visible = s.value("analysis_visible", False, type=bool)
        self._cursors_enabled = s.value("cursors_enabled", True, type=bool)
        self._legend_on = s.value("legend_on", True, type=bool)
        self._metadata_visible = s.value("metadata_visible", False, type=bool)
        self._allow_provenance_edits = False   # always start protected each session

    def _restore_geometry(self):
        geo = self.settings.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                self.resize(1320, 840)
        else:
            self.resize(1320, 840)

    def _remember_dir(self, path):
        d = os.path.dirname(str(path))
        if d:
            self._last_dir = d
            self.settings.setValue("last_dir", d)

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)

    def _build_welcome(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        title = QLabel("GrAF Explorer"); title.setObjectName("welcomeTitle")
        title.setAlignment(Qt.AlignCenter)
        hint = QLabel("Open a .graf file  (Cmd+O)   or   drag files onto this window")
        hint.setObjectName("welcomeHint"); hint.setAlignment(Qt.AlignCenter)
        btn = QPushButton("Open file…")
        btn.clicked.connect(self.open_dialog)
        lay.addWidget(title); lay.addSpacing(8); lay.addWidget(hint)
        lay.addSpacing(22); lay.addWidget(btn, alignment=Qt.AlignCenter)
        return w

    def _build_menu(self):
        bar = self.menuBar()
        filemenu = bar.addMenu("File")

        act_open = filemenu.addAction("Open…")
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self.open_dialog)

        act_save = filemenu.addAction("Save As…")
        act_save.setShortcut(QKeySequence.Save)        # Cmd+S → always Save As
        act_save.triggered.connect(self._save_current)

        act_close = filemenu.addAction("Close Tab")
        act_close.setShortcut(QKeySequence.Close)
        act_close.triggered.connect(self._close_current_tab)

        act_compare = filemenu.addAction("New Comparison…")
        act_compare.setShortcut("Ctrl+Shift+N")
        act_compare.triggered.connect(self._new_comparison)

        filemenu.addSeparator()
        export_fig = filemenu.addMenu("Export to")
        for label, key in [("PNG", "png"), ("JPEG", "jpeg"), ("SVG", "svg"),
                           ("PDF", "pdf"), ("Pickled figure (.pklfig)", "pklfig")]:
            a = export_fig.addAction(label)
            a.triggered.connect(lambda _c, k=key: self._export_fig_current(k))

        export_data = filemenu.addMenu("Export data")
        for label, key in [("CSV", "csv"), ("JSON", "json")]:
            a = export_data.addAction(label)
            a.triggered.connect(lambda _c, k=key: self._export_data_current(k))

        filemenu.addSeparator()
        act_quit = filemenu.addAction("Quit")
        act_quit.setShortcut(QKeySequence.Quit)
        act_quit.triggered.connect(self.close)

        editmenu = bar.addMenu("Edit")
        act_undo = editmenu.addAction("Undo")
        act_undo.setShortcut(QKeySequence.Undo)        # Cmd+Z / Ctrl+Z
        act_undo.triggered.connect(self._undo_current)
        act_redo = editmenu.addAction("Redo")
        act_redo.setShortcut(QKeySequence.Redo)        # Cmd+Shift+Z / Ctrl+Y
        act_redo.triggered.connect(self._redo_current)
        act_lock = editmenu.addAction("Toggle Lock")
        act_lock.setShortcut("Ctrl+L")                 # Cmd+L on macOS
        act_lock.triggered.connect(self._toggle_lock_current)
        editmenu.addSeparator()
        act_revert = editmenu.addAction("Revert to Original…")
        act_revert.triggered.connect(self._revert_current)

        editmenu.addSeparator()
        prov_menu = editmenu.addMenu("Provenance")
        self._allow_prov_action = prov_menu.addAction("Allow Edits")
        self._allow_prov_action.setCheckable(True)
        self._allow_prov_action.setChecked(False)
        self._allow_prov_action.toggled.connect(self._toggle_allow_provenance)

        # Actions/menus that only make sense for a file tab (greyed out when a
        # Comparison tab is active or no tab is open).
        self._filetab_actions = [act_save, act_undo, act_redo, act_lock, act_revert]
        self._filetab_menus = [export_fig, export_data, prov_menu]

        viewmenu = bar.addMenu("View")

        self._sidebar_action = viewmenu.addAction("Show Sidebar")
        self._sidebar_action.setCheckable(True)
        self._sidebar_action.setChecked(self._sidebar_visible)
        self._sidebar_action.setShortcut("Ctrl+B")     # Cmd+B on macOS
        self._sidebar_action.toggled.connect(self._toggle_sidebar)

        self._analysis_action = viewmenu.addAction("Show Analysis Panel")
        self._analysis_action.setCheckable(True)
        self._analysis_action.setChecked(self._analysis_visible)
        self._analysis_action.setShortcut("Ctrl+Shift+A")
        self._analysis_action.toggled.connect(self._toggle_analysis)

        self._cursors_action = viewmenu.addAction("Data Cursors")
        self._cursors_action.setCheckable(True)
        self._cursors_action.setChecked(self._cursors_enabled)
        self._cursors_action.toggled.connect(self._toggle_cursors)

        self._legend_action = viewmenu.addAction("Show Legend")
        self._legend_action.setCheckable(True)
        self._legend_action.setChecked(self._legend_on)
        self._legend_action.toggled.connect(self._toggle_legend)

        self._metadata_action = viewmenu.addAction("Show Metadata Panel")
        self._metadata_action.setCheckable(True)
        self._metadata_action.setChecked(self._metadata_visible)
        self._metadata_action.setShortcut("Ctrl+Shift+I")
        self._metadata_action.toggled.connect(self._toggle_metadata_panel)

        act_tight = viewmenu.addAction("Tight Layout")
        act_tight.setShortcut("Ctrl+R")
        act_tight.triggered.connect(self._tight_layout_all)
        viewmenu.addSeparator()

        theme_menu = viewmenu.addMenu("Theme")
        self._theme_group = QActionGroup(self); self._theme_group.setExclusive(True)
        for tname in THEMES:
            a = theme_menu.addAction(tname); a.setCheckable(True)
            a.setChecked(tname == self._theme_name)
            a.triggered.connect(lambda _c, n=tname: self._set_theme(n))
            self._theme_group.addAction(a)

        font_menu = viewmenu.addMenu("Font")
        fam_menu = font_menu.addMenu("Family")
        self._family_group = QActionGroup(self); self._family_group.setExclusive(True)
        for fkey in FONT_FAMILIES:
            a = fam_menu.addAction(fkey); a.setCheckable(True)
            a.setChecked(fkey == self._font_family_key)
            a.triggered.connect(lambda _c, k=fkey: self._set_font_family(k))
            self._family_group.addAction(a)
        font_menu.addSeparator()
        a_big = font_menu.addAction("Increase Size"); a_big.setShortcut(QKeySequence.ZoomIn)
        a_big.triggered.connect(lambda: self._change_font_size(+1))
        a_small = font_menu.addAction("Decrease Size"); a_small.setShortcut(QKeySequence.ZoomOut)
        a_small.triggered.connect(lambda: self._change_font_size(-1))
        a_reset = font_menu.addAction("Reset Size")
        a_reset.triggered.connect(lambda: self._set_font_size(THEMES[DEFAULT_THEME].base_pt))

    # -- theme -----------------------------------------------------------------
    def _apply_theme(self):
        QApplication.instance().setStyleSheet(build_stylesheet(self.theme))
        set_icon_tint(self.theme.text)       # re-tint themed button glyphs
        if hasattr(self, "tabs"):
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, FileTab):
                    w.set_provenance_color(self.theme.provenance)

    def _set_theme(self, name):
        self.theme = replace(THEMES[name], ui_family=self.theme.ui_family,
                             base_pt=self.theme.base_pt)
        self._theme_name = name
        self.settings.setValue("theme", name)
        self._apply_theme()

    def _set_font_family(self, key):
        self.theme.ui_family = FONT_FAMILIES[key]
        self._font_family_key = key
        self.settings.setValue("font_family", key)
        self._apply_theme()

    def _change_font_size(self, delta):
        self._set_font_size(self.theme.base_pt + delta)

    def _set_font_size(self, pt):
        self.theme.base_pt = max(9, min(22, int(pt)))
        self.settings.setValue("font_size", self.theme.base_pt)
        self._apply_theme()

    # -- file ops --------------------------------------------------------------
    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open GrAF file(s)", self._last_dir,
            "GrAF files (*.graf);;All files (*)")
        if paths:
            self._remember_dir(paths[0])
        self.open_paths(paths)

    def open_paths(self, paths):
        for p in paths:
            self.open_one(p)

    def open_one(self, path):
        path = Path(path)
        try:
            graf = load_graf_file(path)
            tab = FileTab(graf, path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Could not open file",
                f"{path.name} could not be loaded.\n\n{exc}\n\n"
                f"{traceback.format_exc(limit=3)}")
            return
        idx = self.tabs.addTab(tab, path.name)
        self.tabs.setTabToolTip(idx, str(path.resolve()))
        self.tabs.setCurrentIndex(idx)
        tab.modifiedChanged.connect(lambda t=tab: self._refresh_tab_text(t))
        tab.legend_on = self._legend_on        # set before view setup re-renders
        tab.set_sidebar_visible(self._sidebar_visible)
        tab.set_cursors_enabled(self._cursors_enabled)
        tab.set_analysis_visible(self._analysis_visible)
        tab.set_legend_on(self._legend_on)
        tab.set_provenance_color(self.theme.provenance)
        tab.set_allow_provenance_edits(self._allow_provenance_edits)
        tab.set_metadata_visible(self._metadata_visible)
        self._restore_split("hsplit_sizes", tab.hsplit)
        self._restore_split("sidebar_sizes", tab.sidebar)
        self._restore_split("plotsplit_sizes", tab.plot_split)
        tab.hsplit.splitterMoved.connect(
            lambda *a, t=tab: self._save_split("hsplit_sizes", t.hsplit))
        tab.sidebar.splitterMoved.connect(
            lambda *a, t=tab: self._save_split("sidebar_sizes", t.sidebar))
        tab.plot_split.splitterMoved.connect(
            lambda *a, t=tab: self._save_split("plotsplit_sizes", t.plot_split))
        self._refresh_overlays()
        self._update_menu_state()
        self._sync_view()

    # -- splitter (panel tiling) persistence -----------------------------------
    def _save_split(self, key, splitter):
        sizes = splitter.sizes()
        if any(sizes):
            self.settings.setValue(key, ",".join(str(int(s)) for s in sizes))

    def _restore_split(self, key, splitter):
        raw = self.settings.value(key, "", type=str)
        if not raw:
            return
        try:
            sizes = [int(x) for x in raw.split(",") if x != ""]
        except ValueError:
            return
        if sizes and len(sizes) == splitter.count():
            splitter.setSizes(sizes)

    def file_tabs(self):
        """All open FileTabs, in tab order (used by comparison tabs)."""
        return [self.tabs.widget(i) for i in range(self.tabs.count())
                if isinstance(self.tabs.widget(i), FileTab)]

    def find_file_tab_by_uid(self, uid):
        """The open FileTab with this uid, or None if it has been closed."""
        for tab in self.file_tabs():
            if getattr(tab, "uid", None) == uid:
                return tab
        return None

    def _refresh_overlays(self):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, OverlayTab):
                w.refresh()

    def _new_comparison(self):
        tab = OverlayTab(self)
        idx = self.tabs.addTab(tab, "Comparison")
        self.tabs.setCurrentIndex(idx)
        self._restore_split("overlay_sizes", tab.split)
        tab.split.splitterMoved.connect(
            lambda *a, t=tab: self._save_split("overlay_sizes", t.split))
        self._update_menu_state()
        self._sync_view()

    def _save_current(self):
        w = self.tabs.currentWidget()
        if isinstance(w, FileTab) and w.save_as():
            self._refresh_tab_text(w)

    def _current_tab(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, FileTab) else None

    def _undo_current(self):
        t = self._current_tab()
        if t:
            t.undo()

    def _redo_current(self):
        t = self._current_tab()
        if t:
            t.redo()

    def _toggle_lock_current(self):
        t = self._current_tab()
        if t:
            t.toggle_lock()

    def _revert_current(self):
        t = self._current_tab()
        if t:
            t.revert()

    def _export_fig_current(self, fmt):
        t = self._current_tab()
        if t:
            t.export_figure(fmt)

    def _export_data_current(self, fmt):
        t = self._current_tab()
        if t:
            t.export_data(fmt)

    def _toggle_sidebar(self, visible):
        self._sidebar_visible = visible
        self.settings.setValue("sidebar_visible", visible)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FileTab):
                w.set_sidebar_visible(visible)

    def _toggle_analysis(self, visible):
        self._analysis_visible = visible
        self.settings.setValue("analysis_visible", visible)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FileTab):
                w.set_analysis_visible(visible)

    def _toggle_cursors(self, enabled):
        self._cursors_enabled = enabled
        self.settings.setValue("cursors_enabled", enabled)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FileTab):
                w.set_cursors_enabled(enabled)

    def _toggle_legend(self, enabled):
        self._legend_on = enabled
        self.settings.setValue("legend_on", enabled)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FileTab):
                w.set_legend_on(enabled)

    def _toggle_metadata_panel(self, enabled):
        self._metadata_visible = enabled
        self.settings.setValue("metadata_visible", enabled)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FileTab):
                w.set_metadata_visible(enabled)

    def _toggle_allow_provenance(self, enabled):
        if enabled:
            resp = QMessageBox.warning(
                self, "Allow editing provenance?",
                "Provenance records where this file came from and what has "
                "happened to it — it is meant to be a trustworthy, tamper-evident "
                "archive.\n\nEditing it by hand can make the file misrepresent its "
                "own history. Are you sure you want to allow provenance edits?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if resp != QMessageBox.Yes:
                self._allow_prov_action.blockSignals(True)
                self._allow_prov_action.setChecked(False)
                self._allow_prov_action.blockSignals(False)
                return
        self._allow_provenance_edits = enabled
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, FileTab):
                w.set_allow_provenance_edits(enabled)

    def _tight_layout_all(self):
        """Re-fit axes into the canvas for every open tab (Ctrl+R). Useful after
        dragging the side panels leaves a plot's axes clipped or off-screen."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if hasattr(w, "apply_tight_layout"):
                w.apply_tight_layout()

    def _refresh_tab_text(self, tab):
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            star = "*" if tab.modified else ""
            base = getattr(tab, "_custom_title", None) or tab.path.name
            self.tabs.setTabText(idx, star + base)
            self.tabs.setTabToolTip(idx, str(tab.path.resolve()))

    def _tab_context_menu(self, pos):
        idx = self.tabs.tabBar().tabAt(pos)
        if idx < 0:
            return
        menu = QMenu(self)
        act_rename = menu.addAction("Rename…")
        act_close = menu.addAction("Close")
        chosen = menu.exec_(self.tabs.tabBar().mapToGlobal(pos))
        if chosen == act_rename:
            self._rename_tab(idx)
        elif chosen == act_close:
            self._close_tab(idx)

    def _rename_tab(self, idx):
        if idx < 0:
            return
        current = self.tabs.tabText(idx).lstrip("*")
        name, ok = QInputDialog.getText(self, "Rename tab", "Tab name:", text=current)
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        w = self.tabs.widget(idx)
        # Persist the name so a FileTab's modified-marker refresh keeps it.
        if w is not None:
            w._custom_title = name
        if isinstance(w, FileTab):
            self._refresh_tab_text(w)     # re-applies "*" + custom name
        else:
            self.tabs.setTabText(idx, name)

    def _close_current_tab(self):
        if self.tabs.count():
            self._close_tab(self.tabs.currentIndex())

    def _close_tab(self, index):
        w = self.tabs.widget(index)
        if isinstance(w, FileTab) and w.modified:
            resp = QMessageBox.question(
                self, "Discard changes?",
                f"{w.path.name} has unsaved changes. Discard them?",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if resp != QMessageBox.Discard:
                return
        was_file = isinstance(w, FileTab)
        if hasattr(w, "close_figure"):
            w.close_figure()
        self.tabs.removeTab(index)
        w.deleteLater()
        if was_file:
            self._refresh_overlays()      # comparisons reflect the closed file
        self._sync_view()

    def _sync_view(self):
        self.stack.setCurrentIndex(1 if self.tabs.count() else 0)
        self._update_menu_state()

    def _on_tab_changed(self, index):
        tab = self.tabs.widget(index)
        if isinstance(tab, OverlayTab):
            tab.refresh()                 # pick up edits made since last shown
        if getattr(tab, "canvas", None) is not None:
            QTimer.singleShot(0, tab.canvas.draw_idle)
        self._update_menu_state()

    def _update_menu_state(self):
        """Enable file-only menu items only when a file tab is current."""
        is_file = isinstance(self.tabs.currentWidget(), FileTab)
        for act in getattr(self, "_filetab_actions", []):
            act.setEnabled(is_file)
        for menu in getattr(self, "_filetab_menus", []):
            menu.setEnabled(is_file)

    # -- drag & drop -----------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.open_paths(paths)
            e.acceptProposedAction()


class DropForwarder(QObject):
    """App-wide filter so drops land even over a child widget, plus macOS FileOpen."""
    def __init__(self, window):
        super().__init__()
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.DragEnter and event.mimeData().hasUrls():
            event.acceptProposedAction()
            return True
        if event.type() == QEvent.Drop:
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.window.open_paths(paths)
                event.acceptProposedAction()
                return True
        if event.type() == QEvent.FileOpen:
            self.window.open_one(event.file())
            return True
        return False


def main():
    # On Windows, give the process its own taskbar identity so the taskbar uses
    # our window icon instead of grouping the app under the Python launcher.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "com.grantgiesbrecht.grafexplorer")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("GrAF Explorer")
    app.setOrganizationName("grantgiesbrecht")          # QSettings scope...
    app.setOrganizationDomain("com.grantgiesbrecht")    # ...macOS plist / Windows registry
    app.setWindowIcon(application_icon())     # title bar + taskbar icon
    app.setStyle(QStyleFactory.create("Fusion"))

    win = GrafExplorer()
    win._forwarder = DropForwarder(win)
    app.installEventFilter(win._forwarder)

    for arg in sys.argv[1:]:
        if Path(arg).exists():
            win.open_one(arg)

    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()