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
    plus your GrAF stack: graf, stardust, pylogfile, ganymede, colorama
"""

import os
import sys
import copy
import tempfile
import traceback
from pathlib import Path
from dataclasses import dataclass, replace

import numpy as np

import matplotlib
matplotlib.use("Qt5Agg")           # Lock the backend BEFORE graf.base pulls in pyplot.
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
    NavigationToolbar2QT as NavigationToolbar,
)

from PyQt5.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QEvent, QObject, QTimer, pyqtSignal,
)
from PyQt5.QtGui import QKeySequence, QBrush, QColor, QIcon
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QSplitter, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTableView, QTreeWidget,
    QTreeWidgetItem, QFileDialog, QMessageBox, QStyleFactory, QHeaderView,
    QPushButton, QAbstractItemView, QActionGroup, QCheckBox, QSizePolicy,
)

# ── GrAF stack ────────────────────────────────────────────────────────────────
from graf.base import Graf
from stardust.sandbox import dict_to_tome   # same import graf.base itself uses


# ── Theme system ───────────────────────────────────────────────────────────────
FLOAT_FMT = "%.8g"
INVALID_RED = "#ff5b5b"
ARRAY_EXPAND_LIMIT = 100   # 1-D arrays at or below this are expanded to editable rows


def resource_path(*parts) -> str:
    """Resolve a path that works both when run as a script and when frozen by
    PyInstaller (which unpacks bundled data under sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def find_app_icon() -> str:
    """Locate the application icon for the window/taskbar (NOT the same thing as
    the .exe's embedded icon or the .graf document icon). Searches the frozen
    bundle, the script dir, and its parent (so dev runs from src/ still work).
    Prefers .ico on Windows for crisp multi-resolution rendering."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        bases.append(mp)
    bases.extend([here, os.path.dirname(here)])
    names = [("icons", "app", "graf_app.ico"), ("icons", "app", "graf_app.png")]
    for base in bases:
        for rel in names:
            p = os.path.join(base, *rel)
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
    ui_family: str = FONT_FAMILIES["Sans"]
    base_pt: int = 13


THEMES = {
    "Graphite": Theme(
        name="Graphite",
        bg="#21252b", surface="#282c34", surface_alt="#2e333d",
        header_bg="#2f3540", accent="#4b89dc", accent_hover="#5d97e6",
        text="#e8eaed", subtext="#9aa0a6", border="#3a3f4b",
        sel_bg="#3d5a80", sel_text="#ffffff",
    ),
    "Daylight": Theme(
        name="Daylight",
        bg="#f4f6f8", surface="#ffffff", surface_alt="#eef1f5",
        header_bg="#e6eaf0", accent="#2f6fde", accent_hover="#1f5fce",
        text="#1b1f24", subtext="#5a626b", border="#d3d8df",
        sel_bg="#cfe2ff", sel_text="#0b2545",
    ),
    "Midnight": Theme(
        name="Midnight",
        bg="#1a1a2e", surface="#16213e", surface_alt="#1b2747",
        header_bg="#0f3460", accent="#e94560", accent_hover="#ff5b78",
        text="#eaeaea", subtext="#8888aa", border="#2a2a4a",
        sel_bg="#e94560", sel_text="#ffffff",
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
            name = tr.get("display_name", "") or "(unnamed)"
            items.append({"label": f"{ax_key} · {tr_key}   {name}",
                          "kind": "trace", "node": tr})
        for sf_key, sf in (ax.get("surfaces", {}) or {}).items():
            name = sf.get("display_name", "") or "(unnamed)"
            items.append({"label": f"{ax_key} · {sf_key}   {name}   [surface]",
                          "kind": "surface", "node": sf})
    return items


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
class FileTab(QWidget):
    _n_created = 0
    modifiedChanged = pyqtSignal()

    def __init__(self, graf: Graf, path: Path, parent=None):
        super().__init__(parent)
        FileTab._n_created += 1
        self._fig_id = FileTab._n_created
        self._render_seq = 0

        self.graf = graf
        self.path = Path(path)
        self.fig = None
        self.canvas = None

        self.locked = True
        self.modified = False
        self._building = False
        self._invalid_items = set()
        self._table_model = None
        self._current_is_trace = False
        self._undo_stack = []
        self._undo_limit = 50

        # Packed dict is the single source of truth for edits, render, and save.
        self.packet = self.graf.pack()
        deep_listify(self.packet)
        self._original_packet = copy.deepcopy(self.packet)   # for "revert to original"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(self._build_lockbar())

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_plot_panel())
        split.addWidget(self._build_sidebar())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([760, 480])
        outer.addWidget(split, 1)        # the splitter takes all extra vertical space

        # First render from the known-good loaded object.
        try:
            self._set_figure(self.graf.to_fig(window_title=self._make_title()))
        except Exception:
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

        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setObjectName("miniButton")
        self.revert_btn.setFixedHeight(28)
        self.revert_btn.clicked.connect(self.revert)

        h.addWidget(self.lock_btn)
        h.addSpacing(8)
        h.addWidget(self.undo_btn)
        h.addWidget(self.revert_btn)
        h.addStretch(1)
        return bar

    # -- left: embedded matplotlib (rebuilt on every render) -------------------
    def _build_plot_panel(self):
        self._plot_container = QWidget()
        self._plot_layout = QVBoxLayout(self._plot_container)
        self._plot_layout.setContentsMargins(0, 0, 0, 0)
        return self._plot_container

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
        bl.addWidget(self.combo)
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

    def _add_item(self, parent, name, value, path):
        ttype, shape, disp = classify_value(value)
        item = QTreeWidgetItem(parent, [name, ttype, shape, disp])
        item.setData(0, Qt.UserRole, path)
        if ttype == "Group":
            self._build_node(item, value, path)
        elif ttype == "Dataset":
            if is_expandable_array(value):
                for i, elem in enumerate(value):
                    child = QTreeWidgetItem(item, [f"[{i}]", "item", "", format_scalar(elem)])
                    child.setData(0, Qt.UserRole, path + [i])
                    child.setFlags(child.flags() | Qt.ItemIsEditable)
            # large / multi-dim arrays stay as a non-editable summary leaf
        else:  # attr scalar — editable
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
            cont, last = self._resolve_container(path)
            try:
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
            else:
                self._mark_tree_invalid(item)
                item.setToolTip(3, f"Edit applied but render failed: {err}")
                self.struct_status.setText(f"✗ {name}: render failed — {err}")
        finally:
            self._building = False

    def _resolve_container(self, path):
        cont = self.packet
        for k in path[:-1]:
            cont = cont[k]
        return cont, path[-1]

    def _display_at(self, path):
        cont = self.packet
        for k in path:
            cont = cont[k]
        return format_scalar(cont)

    def _mark_tree_invalid(self, item):
        self.tree.blockSignals(True)
        item.setForeground(3, QBrush(QColor(INVALID_RED)))
        self.tree.blockSignals(False)
        self._invalid_items.add(item)

    def _clear_tree_invalids(self):
        self.tree.blockSignals(True)
        for it in list(self._invalid_items):
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
        self._sync_edit_state()

    def _commit_data_edit(self, r, c):
        self._mark_modified()
        ok, _ = self._rerender_from_packet()
        return ok

    def _add_row(self):
        if self.locked or self._table_model is None or not self._current_is_trace:
            return
        self._snapshot()
        self._table_model.add_row()
        self._mark_modified()
        self._rerender_from_packet()
        self._refresh_structure_preserving()

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
        self._rerender_from_packet()
        self._refresh_structure_preserving()

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

    # -- undo / revert ---------------------------------------------------------
    def _snapshot(self):
        """Push the current packet onto the undo stack (called before a change)."""
        self._undo_stack.append(copy.deepcopy(self.packet))
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack.pop(0)

    def undo(self):
        if not self._undo_stack:
            self.struct_status.setText("Nothing to undo")
            return
        self.packet = self._undo_stack.pop()
        self._reload_views()
        # Empty stack after popping ⇒ back at the as-loaded state.
        self.modified = bool(self._undo_stack)
        self.modifiedChanged.emit()
        self.struct_status.setText("Undid last change")

    def revert(self):
        if not self.modified and not self._undo_stack:
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
        for it in self.items:
            self.combo.addItem(it["label"])
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
            fig = g.to_fig(window_title=self._make_title())
        except Exception as exc:
            return False, exc
        self._set_figure(fig)
        self.graf = g
        self._clear_tree_invalids()
        return True, None

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
            dict_to_tome(copy.deepcopy(self.packet), path, show_detail=False)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write {path}\n\n{exc}")
            return False
        self.path = Path(path)
        self.modified = False
        self.modifiedChanged.emit()
        return True

    def close_figure(self):
        if self.fig is not None:
            try:
                plt.close(self.fig)
            except Exception:
                pass
            self.fig = None


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
        self.stack.addWidget(self.tabs)

        self.theme = replace(THEMES[DEFAULT_THEME])
        self._build_menu()
        self._apply_theme()
        self._sync_view()

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

        filemenu.addSeparator()
        act_quit = filemenu.addAction("Quit")
        act_quit.setShortcut(QKeySequence.Quit)
        act_quit.triggered.connect(self.close)

        editmenu = bar.addMenu("Edit")
        act_undo = editmenu.addAction("Undo")
        act_undo.setShortcut(QKeySequence.Undo)        # Cmd+Z / Ctrl+Z
        act_undo.triggered.connect(self._undo_current)
        act_lock = editmenu.addAction("Toggle Lock")
        act_lock.setShortcut("Ctrl+L")                 # Cmd+L on macOS
        act_lock.triggered.connect(self._toggle_lock_current)
        editmenu.addSeparator()
        act_revert = editmenu.addAction("Revert to Original…")
        act_revert.triggered.connect(self._revert_current)

        viewmenu = bar.addMenu("View")
        theme_menu = viewmenu.addMenu("Theme")
        self._theme_group = QActionGroup(self); self._theme_group.setExclusive(True)
        for tname in THEMES:
            a = theme_menu.addAction(tname); a.setCheckable(True)
            a.setChecked(tname == DEFAULT_THEME)
            a.triggered.connect(lambda _c, n=tname: self._set_theme(n))
            self._theme_group.addAction(a)

        font_menu = viewmenu.addMenu("Font")
        fam_menu = font_menu.addMenu("Family")
        self._family_group = QActionGroup(self); self._family_group.setExclusive(True)
        for fkey in FONT_FAMILIES:
            a = fam_menu.addAction(fkey); a.setCheckable(True)
            a.setChecked(fkey == "Sans")
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

    def _set_theme(self, name):
        self.theme = replace(THEMES[name], ui_family=self.theme.ui_family,
                             base_pt=self.theme.base_pt)
        self._apply_theme()

    def _set_font_family(self, key):
        self.theme.ui_family = FONT_FAMILIES[key]
        self._apply_theme()

    def _change_font_size(self, delta):
        self._set_font_size(self.theme.base_pt + delta)

    def _set_font_size(self, pt):
        self.theme.base_pt = max(9, min(22, int(pt)))
        self._apply_theme()

    # -- file ops --------------------------------------------------------------
    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open GrAF file(s)", "", "GrAF files (*.graf);;All files (*)")
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

    def _toggle_lock_current(self):
        t = self._current_tab()
        if t:
            t.toggle_lock()

    def _revert_current(self):
        t = self._current_tab()
        if t:
            t.revert()

    def _refresh_tab_text(self, tab):
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            star = "*" if tab.modified else ""
            self.tabs.setTabText(idx, star + tab.path.name)
            self.tabs.setTabToolTip(idx, str(tab.path.resolve()))

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
        if isinstance(w, FileTab):
            w.close_figure()
        self.tabs.removeTab(index)
        w.deleteLater()
        self._sync_view()

    def _sync_view(self):
        self.stack.setCurrentIndex(1 if self.tabs.count() else 0)

    def _on_tab_changed(self, index):
        tab = self.tabs.widget(index)
        if isinstance(tab, FileTab) and tab.canvas is not None:
            QTimer.singleShot(0, tab.canvas.draw_idle)

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