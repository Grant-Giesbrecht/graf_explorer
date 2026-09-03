#!/usr/bin/env python3
"""
Trace formatting dialogs
========================
Reusable Qt dialogs for editing a GrAF trace's line / marker / error-bar
formatting, plus the color picker they hang off.

    ColorPickerDialog   HSL / RGB / HSV / swatch tabs, live preview, copyable
                        hex + rgb readouts.
    ColorField          one-line color editor: text box (hex, CSS name,
                        "r,g,b", "rgb(...)") + a palette button opening the
                        picker, with a swatch preview.
    TraceStyleDialog    the format editor. One trace ⇒ a plain form; several
                        ⇒ one tab per trace plus "Copy to all".

Styles are read and written as packed-trace dicts (the same field names GrAF's
Trace uses), so a dialog result can be dropped straight into a packet.
"""

import colorsys
import os
import sys
import weakref

from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QWidget, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QDoubleSpinBox, QSlider, QTabWidget, QGroupBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QScrollArea,
    QShortcut, QSizePolicy,
)

# GrAF's own vocabulary; the literals are the fallback if graf isn't importable
# (e.g. when this module is exercised on its own).
try:
    from graf.base import LINE_TYPES, MARKER_TYPES
except Exception:                                        # pragma: no cover
    LINE_TYPES = ["-", "-.", ":", "--", "None"]
    MARKER_TYPES = [".", "+", "^", "v", "o", "x", "[]", "|", "_", "*", "None"]

LINE_TYPE_LABELS = {
    "-": "—  solid",
    "--": "– – –  dashed",
    "-.": "–·–·  dash-dot",
    ":": "······  dotted",
    "None": "(none)",
}
MARKER_TYPE_LABELS = {
    ".": ".   point",
    "+": "+   plus",
    "^": "^   triangle up",
    "v": "v   triangle down",
    "o": "o   circle",
    "x": "x   cross",
    "[]": "□   square",
    "|": "|   vertical line",
    "_": "_   horizontal line",
    "*": "*   star",
    "None": "(none)",
}

# Swatch tab: matplotlib's default cycle followed by common plain colors.
SWATCHES = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#000000", "#404040", "#808080", "#c0c0c0", "#ffffff",
    "#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff",
    "#00ffff", "#800000", "#008000", "#000080", "#808000",
    "#800080", "#008080", "#ffa500", "#a52a2a", "#ffc0cb",
]


# ── themed icons ───────────────────────────────────────────────────────────────
# icons/*.png are single-color glyphs on transparency, so they are re-tinted to
# the active theme's text color. Buttons carrying one register here and are
# re-tinted when the theme changes.
_icon_tint = "#e8eaed"
_icon_buttons = []          # weakrefs to buttons showing a themed icon


def icon_path(name):
    """Locate an icons/<name> that works from src/, the project root and a
    PyInstaller bundle. Returns None when the icon isn't there."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        bases.append(mp)
    bases += [here, os.path.dirname(here), os.getcwd()]
    for base in bases:
        p = os.path.join(base, "icons", name)
        if os.path.exists(p):
            return p
    return None


def tinted_icon(name, color=None, size=18):
    """The named icon recolored to `color` (the current theme tint by default)."""
    path = icon_path(name)
    if not path:
        return QIcon()
    pm = QPixmap(path)
    if pm.isNull():
        return QIcon()
    pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    tinted = QPixmap(pm.size())
    tinted.fill(Qt.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pm)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), QColor(color or _icon_tint))
    painter.end()
    return QIcon(tinted)


def apply_themed_icon(button, name, size=16):
    """Give a button a themed icon and keep it in sync with the theme."""
    button.setIcon(tinted_icon(name, size=size))
    button.setIconSize(QSize(size, size))
    button.setProperty("_themed_icon", (name, size))
    _icon_buttons.append(weakref.ref(button))
    return button


def set_icon_tint(color):
    """Re-tint every themed icon (called when the app theme changes)."""
    global _icon_tint
    _icon_tint = color
    live = []
    for ref in _icon_buttons:
        btn = ref()
        if btn is None:
            continue
        live.append(ref)
        try:
            name, size = btn.property("_themed_icon")
            btn.setIcon(tinted_icon(name, size=size))
        except Exception:
            pass
    _icon_buttons[:] = live


# ── color conversion helpers ──────────────────────────────────────────────────
def clamp01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


def rgb_to_hex(rgb):
    r, g, b = (int(round(clamp01(c) * 255)) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def rgb_to_255(rgb):
    return tuple(int(round(clamp01(c) * 255)) for c in rgb)


def parse_color(text, default=None):
    """Parse a color written as hex (#rgb/#rrggbb), a CSS/matplotlib name,
    "r,g,b" (0-255 ints or 0-1 floats) or "rgb(r,g,b)". Returns an (r,g,b)
    float triple, or `default` if the text is not a color."""
    if text is None:
        return default
    s = str(text).strip()
    if not s:
        return default
    low = s.lower()
    if low.startswith("rgb(") and low.endswith(")"):
        s = s[4:-1]
        low = s.lower()
    # Comma / whitespace separated triples
    parts = [p for p in low.replace(",", " ").split() if p]
    if len(parts) == 3:
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            vals = None
        if vals is not None:
            if all(0.0 <= v <= 1.0 for v in vals) and any("." in p for p in parts):
                return tuple(vals)                      # already 0-1 floats
            if all(0.0 <= v <= 255.0 for v in vals):
                return tuple(v / 255.0 for v in vals)
    # Hex and named colors: matplotlib knows the most names, Qt is the backup.
    try:
        from matplotlib.colors import to_rgb
        return tuple(float(c) for c in to_rgb(s))
    except Exception:
        pass
    qc = QColor(s)
    if qc.isValid():
        return (qc.redF(), qc.greenF(), qc.blueF())
    return default


def color_from_style(value, default=(0.0, 0.0, 0.0)):
    """Coerce a packed trace's color field (list/tuple of floats, or a string)
    into an (r,g,b) float triple."""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return tuple(clamp01(float(c)) for c in value[:3])
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        return parse_color(value, default)
    return default


class _Swatch(QLabel):
    """Flat color chip."""

    def __init__(self, rgb=(0, 0, 0), w=44, h=22, parent=None):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.setRgb(rgb)

    def setRgb(self, rgb):
        self._rgb = tuple(rgb)
        self.setStyleSheet(
            f"background-color: {rgb_to_hex(rgb)};"
            "border: 1px solid rgba(128,128,128,160); border-radius: 3px;")


def no_default_buttons(dialog):
    """Stop any button stealing Enter, so Enter commits the field being edited
    instead of closing the dialog (or firing whichever button Qt picked)."""
    for btn in dialog.findChildren(QPushButton):
        btn.setAutoDefault(False)
        btn.setDefault(False)


class _NoDefaultButtonDialog(QDialog):
    """QDialog whose buttons never become the default one. Qt (and
    QDialogButtonBox) install a default button when a dialog is shown, which
    makes Enter close it; these dialogs want Enter to commit the field being
    edited instead, and reserve ⌘/Ctrl+Enter for closing."""

    def showEvent(self, event):
        super().showEvent(event)
        no_default_buttons(self)


def add_accept_shortcuts(dialog, slot):
    """⌘/Ctrl+Enter (both Return and the keypad Enter) accepts the dialog."""
    for seq in ("Ctrl+Return", "Ctrl+Enter"):
        QShortcut(QKeySequence(seq), dialog, activated=slot)


# ── color picker ──────────────────────────────────────────────────────────────
class _SliderRow(QWidget):
    """Slider + spin box locked together, reported in 0-1 regardless of range."""

    valueChanged = pyqtSignal(float)

    def __init__(self, lo, hi, decimals=0, suffix="", parent=None):
        super().__init__(parent)
        self._lo, self._hi = float(lo), float(hi)
        self._guard = False
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setRange(self._lo, self._hi)
        self.spin.setSingleStep(1 if decimals == 0 else 0.01)
        self.spin.setSuffix(suffix)
        self.spin.setFixedWidth(84)
        h.addWidget(self.slider, 1)
        h.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _emit(self):
        self.valueChanged.emit(self.fraction())

    def _from_slider(self, v):
        if self._guard:
            return
        self._guard = True
        self.spin.setValue(self._lo + (self._hi - self._lo) * v / 1000.0)
        self._guard = False
        self._emit()

    def _from_spin(self, v):
        if self._guard:
            return
        self._guard = True
        span = (self._hi - self._lo) or 1.0
        self.slider.setValue(int(round((v - self._lo) / span * 1000)))
        self._guard = False
        self._emit()

    def fraction(self):
        span = (self._hi - self._lo) or 1.0
        return (self.spin.value() - self._lo) / span

    def setFraction(self, frac):
        """Set the value from a 0-1 fraction without emitting valueChanged."""
        self._guard = True
        self.spin.setValue(self._lo + (self._hi - self._lo) * clamp01(frac))
        self.slider.setValue(int(round(clamp01(frac) * 1000)))
        self._guard = False


class ColorPickerDialog(_NoDefaultButtonDialog):
    """Pick a color by HSL, RGB, HSV or swatch, with a live preview and
    copyable hex / rgb readouts."""

    def __init__(self, rgb=(0, 0, 0), parent=None, title="Choose color"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._rgb = color_from_style(rgb)
        self._start_rgb = self._rgb
        self._syncing = False
        self._build_ui()
        self._push_to_widgets()

    # -- construction ---------------------------------------------------------
    def _build_ui(self):
        v = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_hsl_tab(), "HSL")
        self.tabs.addTab(self._build_rgb_tab(), "RGB")
        self.tabs.addTab(self._build_hsv_tab(), "HSV")
        self.tabs.addTab(self._build_swatch_tab(), "Swatches")
        v.addWidget(self.tabs)

        prev = QHBoxLayout()
        prev.addWidget(QLabel("Before"))
        self.before_swatch = _Swatch(self._rgb, w=52, h=30)
        prev.addWidget(self.before_swatch)
        prev.addSpacing(6)
        prev.addWidget(QLabel("After"))
        self.preview = _Swatch(self._rgb, w=110, h=30)
        prev.addWidget(self.preview, 1)
        v.addLayout(prev)

        form = QFormLayout()
        self.hex_edit = QLineEdit()
        self.hex_edit.setToolTip("Hex code — editable and copyable")
        self.hex_edit.editingFinished.connect(self._from_hex_edit)
        form.addRow("Hex", self.hex_edit)
        self.rgb_edit = QLineEdit()
        self.rgb_edit.setToolTip("R, G, B (0-255) — editable and copyable")
        self.rgb_edit.editingFinished.connect(self._from_rgb_edit)
        form.addRow("RGB", self.rgb_edit)
        self.float_edit = QLineEdit()
        self.float_edit.setReadOnly(True)
        self.float_edit.setToolTip("R, G, B (0-1), as GrAF stores it")
        form.addRow("Float", self.float_edit)
        v.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel
                              | QDialogButtonBox.Reset)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Reset).setText("Revert")
        bb.button(QDialogButtonBox.Reset).clicked.connect(self._revert)
        # Enter commits the field being edited rather than closing the dialog;
        # ⌘/Ctrl+Enter is what accepts it.
        add_accept_shortcuts(self, self.accept)
        bb.button(QDialogButtonBox.Ok).setText("OK  (⌘⏎)" if sys.platform == "darwin"
                                               else "OK  (Ctrl+⏎)")
        v.addWidget(bb)
        no_default_buttons(self)
        self.resize(430, 400)

    def _build_hsl_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.hsl_h = _SliderRow(0, 360, 0, "°")
        self.hsl_s = _SliderRow(0, 100, 0, "%")
        self.hsl_l = _SliderRow(0, 100, 0, "%")
        for row, lab in ((self.hsl_h, "Hue"), (self.hsl_s, "Saturation"),
                         (self.hsl_l, "Lightness")):
            row.valueChanged.connect(self._from_hsl)
            f.addRow(lab, row)
        return w

    def _build_rgb_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.rgb_r = _SliderRow(0, 255)
        self.rgb_g = _SliderRow(0, 255)
        self.rgb_b = _SliderRow(0, 255)
        for row, lab in ((self.rgb_r, "Red"), (self.rgb_g, "Green"),
                         (self.rgb_b, "Blue")):
            row.valueChanged.connect(self._from_rgb_sliders)
            f.addRow(lab, row)
        return w

    def _build_hsv_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.hsv_h = _SliderRow(0, 360, 0, "°")
        self.hsv_s = _SliderRow(0, 100, 0, "%")
        self.hsv_v = _SliderRow(0, 100, 0, "%")
        for row, lab in ((self.hsv_h, "Hue"), (self.hsv_s, "Saturation"),
                         (self.hsv_v, "Value")):
            row.valueChanged.connect(self._from_hsv)
            f.addRow(lab, row)
        return w

    def _build_swatch_tab(self):
        w = QWidget()
        g = QGridLayout(w)
        g.setSpacing(4)
        for i, hexcode in enumerate(SWATCHES):
            btn = QPushButton()
            btn.setFixedSize(40, 26)
            btn.setToolTip(hexcode)
            btn.setStyleSheet(f"background-color: {hexcode};"
                              "border: 1px solid rgba(128,128,128,160);")
            btn.clicked.connect(lambda _=False, h=hexcode: self.set_rgb(parse_color(h)))
            g.addWidget(btn, i // 6, i % 6)
        g.setRowStretch(g.rowCount(), 1)
        return w

    # -- value plumbing -------------------------------------------------------
    def rgb(self):
        return self._rgb

    def hex(self):
        return rgb_to_hex(self._rgb)

    def set_rgb(self, rgb):
        self._rgb = color_from_style(rgb, self._rgb)
        self._push_to_widgets()

    def _push_to_widgets(self):
        """Mirror self._rgb into every tab and readout."""
        if self._syncing:
            return
        self._syncing = True
        r, g, b = self._rgb
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        self.hsl_h.setFraction(h)
        self.hsl_s.setFraction(s)
        self.hsl_l.setFraction(l)
        hv, sv, vv = colorsys.rgb_to_hsv(r, g, b)
        self.hsv_h.setFraction(hv)
        self.hsv_s.setFraction(sv)
        self.hsv_v.setFraction(vv)
        self.rgb_r.setFraction(r)
        self.rgb_g.setFraction(g)
        self.rgb_b.setFraction(b)
        self.preview.setRgb(self._rgb)
        self.hex_edit.setText(rgb_to_hex(self._rgb))
        self.rgb_edit.setText("{}, {}, {}".format(*rgb_to_255(self._rgb)))
        self.float_edit.setText("{:.4f}, {:.4f}, {:.4f}".format(r, g, b))
        self._syncing = False

    def _from_hsl(self, *_):
        if self._syncing:
            return
        self._rgb = colorsys.hls_to_rgb(self.hsl_h.fraction(),
                                        self.hsl_l.fraction(),
                                        self.hsl_s.fraction())
        self._push_to_widgets()

    def _from_hsv(self, *_):
        if self._syncing:
            return
        self._rgb = colorsys.hsv_to_rgb(self.hsv_h.fraction(),
                                        self.hsv_s.fraction(),
                                        self.hsv_v.fraction())
        self._push_to_widgets()

    def _from_rgb_sliders(self, *_):
        if self._syncing:
            return
        self._rgb = (self.rgb_r.fraction(), self.rgb_g.fraction(),
                     self.rgb_b.fraction())
        self._push_to_widgets()

    def _from_hex_edit(self):
        rgb = parse_color(self.hex_edit.text())
        if rgb is None:
            self.hex_edit.setText(rgb_to_hex(self._rgb))   # unparseable: put it back
            return
        self._rgb = rgb
        self._push_to_widgets()

    def _from_rgb_edit(self):
        rgb = parse_color(self.rgb_edit.text())
        if rgb is None:
            self.rgb_edit.setText("{}, {}, {}".format(*rgb_to_255(self._rgb)))
            return
        self._rgb = rgb
        self._push_to_widgets()

    def _revert(self):
        self.set_rgb(self._start_rgb)


# ── color field ───────────────────────────────────────────────────────────────
class ColorField(QWidget):
    """Text box (hex / name / r,g,b) + swatch + picker button."""

    colorChanged = pyqtSignal(tuple)

    def __init__(self, rgb=(0, 0, 0), parent=None, label="color"):
        super().__init__(parent)
        self._rgb = color_from_style(rgb)
        self._label = label
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.swatch = _Swatch(self._rgb, w=26, h=22)
        self.edit = QLineEdit(rgb_to_hex(self._rgb))
        self.edit.setToolTip("#rrggbb, a color name, or r,g,b")
        self.edit.editingFinished.connect(self._from_text)
        self.btn = QPushButton()
        self.btn.setObjectName("iconButton")
        self.btn.setFixedSize(30, 24)
        apply_themed_icon(self.btn, "color.png", size=15)
        self.btn.setToolTip("Open the color picker")
        self.btn.clicked.connect(self._open_picker)
        h.addWidget(self.swatch)
        h.addWidget(self.edit, 1)
        h.addWidget(self.btn)

    def rgb(self):
        return self._rgb

    def setRgb(self, rgb, emit=True):
        rgb = color_from_style(rgb, self._rgb)
        self._rgb = rgb
        self.swatch.setRgb(rgb)
        if self.edit.text().strip() != rgb_to_hex(rgb):
            self.edit.setText(rgb_to_hex(rgb))
        if emit:
            self.colorChanged.emit(rgb)

    def _from_text(self):
        rgb = parse_color(self.edit.text())
        if rgb is None:
            self.edit.setText(rgb_to_hex(self._rgb))       # keep the last good one
            return
        self.setRgb(rgb)

    def _open_picker(self):
        dlg = ColorPickerDialog(self._rgb, self, title=f"Choose {self._label}")
        if dlg.exec_() == QDialog.Accepted:
            self.setRgb(dlg.rgb())


# ── one trace's format form ────────────────────────────────────────────────────
class TraceStyleForm(QWidget):
    """Editor for a single trace's formatting. `style()` returns the edited
    fields as a packed-trace dict fragment."""

    changed = pyqtSignal()

    # (packet field, default) for the plain scalar fields
    SIZE_FIELDS = (
        ("line_width", 1.0), ("marker_size", 1.0), ("alpha", 1.0),
        ("err_line_width", 1.0), ("err_cap_size", 3.0), ("err_cap_width", 1.0),
    )

    def __init__(self, style, parent=None):
        super().__init__(parent)
        self._style = dict(style or {})
        self._guard = False
        self._build_ui()
        self.set_style(self._style)

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)

        name_box = QGroupBox("Trace")
        nf = QFormLayout(name_box)
        self.display_name = QLineEdit()
        self.display_name.setPlaceholderText("(unnamed)")
        self.display_name.setToolTip("The trace's display name — its legend entry")
        nf.addRow("Name", self.display_name)
        v.addWidget(name_box)

        line_box = QGroupBox("Line")
        lf = QFormLayout(line_box)
        self.line_type = QComboBox()
        for lt in LINE_TYPES:
            self.line_type.addItem(LINE_TYPE_LABELS.get(lt, lt), lt)
        lf.addRow("Style", self.line_type)
        self.line_width = self._spin(0.0, 30.0, 0.1, 2)
        lf.addRow("Width", self.line_width)
        self.line_color = ColorField(label="line color")
        lf.addRow("Color", self.line_color)
        self.alpha = self._spin(0.0, 1.0, 0.05, 2)
        lf.addRow("Alpha", self.alpha)
        v.addWidget(line_box)

        mk_box = QGroupBox("Marker")
        mf = QFormLayout(mk_box)
        self.marker_type = QComboBox()
        for mt in MARKER_TYPES:
            self.marker_type.addItem(MARKER_TYPE_LABELS.get(mt, mt), mt)
        mf.addRow("Style", self.marker_type)
        self.marker_size = self._spin(0.0, 60.0, 0.5, 2)
        mf.addRow("Size", self.marker_size)
        self.marker_color = ColorField(label="marker color")
        mf.addRow("Color", self.marker_color)
        v.addWidget(mk_box)

        self.err_box = QGroupBox("Error bars")
        ef = QFormLayout(self.err_box)
        self.err_cap_visible = QCheckBox("show caps")
        ef.addRow("", self.err_cap_visible)
        self.err_line_color = ColorField(label="error bar color")
        ef.addRow("Bar color", self.err_line_color)
        self.err_line_width = self._spin(0.0, 30.0, 0.1, 2)
        ef.addRow("Bar width", self.err_line_width)
        self.err_cap_size = self._spin(0.0, 60.0, 0.5, 2)
        ef.addRow("Cap size", self.err_cap_size)
        self.err_cap_color = ColorField(label="cap color")
        ef.addRow("Cap color", self.err_cap_color)
        self.err_cap_width = self._spin(0.0, 30.0, 0.1, 2)
        ef.addRow("Cap width", self.err_cap_width)
        v.addWidget(self.err_box)

        self.no_err_hint = QLabel("This trace has no error bars.")
        self.no_err_hint.setObjectName("welcomeHint")
        v.addWidget(self.no_err_hint)
        v.addStretch(1)

        for combo in (self.line_type, self.marker_type):
            combo.currentIndexChanged.connect(self._emit_changed)
        for name, _ in self.SIZE_FIELDS:
            getattr(self, name).valueChanged.connect(self._emit_changed)
        for name in ("line_color", "marker_color", "err_line_color", "err_cap_color"):
            getattr(self, name).colorChanged.connect(self._emit_changed)
        self.err_cap_visible.toggled.connect(self._emit_changed)
        # textEdited (not textChanged) so set_style() loading a name is silent.
        self.display_name.textEdited.connect(self._emit_changed)

    @staticmethod
    def _spin(lo, hi, step, decimals):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(decimals)
        s.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return s

    def _emit_changed(self, *_):
        if not self._guard:
            self.changed.emit()

    # -- value plumbing -------------------------------------------------------
    def set_style(self, style):
        """Load a packed-trace dict into the widgets."""
        self._guard = True
        self._style = dict(style or {})
        s = self._style
        self.display_name.setText(str(s.get("display_name", "") or ""))
        self._select(self.line_type, s.get("line_type", "-"), LINE_TYPES)
        self._select(self.marker_type, s.get("marker_type", "None"), MARKER_TYPES)
        for name, default in self.SIZE_FIELDS:
            getattr(self, name).setValue(_as_float(s.get(name), default))
        self.line_color.setRgb(color_from_style(s.get("line_color"), (0, 0, 0)),
                               emit=False)
        self.marker_color.setRgb(
            color_from_style(s.get("marker_color"), self.line_color.rgb()), emit=False)
        self.err_line_color.setRgb(
            color_from_style(s.get("err_line_color"), (0.5, 0.5, 0.5)), emit=False)
        self.err_cap_color.setRgb(
            color_from_style(s.get("err_cap_color"), self.err_line_color.rgb()),
            emit=False)
        self.err_cap_visible.setChecked(bool(s.get("err_cap_visible", True)))
        has_err = bool(s.get("has_error_bars", False))
        self.err_box.setVisible(has_err)
        self.no_err_hint.setVisible(not has_err)
        self._guard = False

    @staticmethod
    def _select(combo, value, allowed):
        idx = combo.findData(value if value in allowed else allowed[0])
        combo.setCurrentIndex(max(idx, 0))

    def base_style(self):
        """The packed-trace dict this form was loaded from (unedited)."""
        return dict(self._style)

    def style(self):
        """The edited formatting, as packed-trace fields. Error-bar fields are
        only included when the trace actually has error bars."""
        out = {
            "display_name": self.display_name.text(),
            "line_type": self.line_type.currentData(),
            "line_width": self.line_width.value(),
            "line_color": list(self.line_color.rgb()),
            "alpha": self.alpha.value(),
            "marker_type": self.marker_type.currentData(),
            "marker_size": self.marker_size.value(),
            "marker_color": list(self.marker_color.rgb()),
        }
        if self._style.get("has_error_bars"):
            out.update({
                "err_line_color": list(self.err_line_color.rgb()),
                "err_line_width": self.err_line_width.value(),
                "err_cap_size": self.err_cap_size.value(),
                "err_cap_color": list(self.err_cap_color.rgb()),
                "err_cap_width": self.err_cap_width.value(),
                "err_cap_visible": bool(self.err_cap_visible.isChecked()),
            })
        return out


def _as_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


# ── the dialog ─────────────────────────────────────────────────────────────────
class TraceStyleDialog(_NoDefaultButtonDialog):
    """Format editor for one or many traces.

    entries : [{"key": hashable, "label": str, "style": packed-trace dict}]
    on_apply: optional callable(styles_dict) invoked by Apply / OK so the host
              can re-render live. styles_dict maps key → edited style dict."""

    def __init__(self, entries, parent=None, on_apply=None,
                 title="Trace formatting"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._entries = list(entries)
        self._on_apply = on_apply
        self._forms = {}                    # key → TraceStyleForm, in tab order
        # Edits apply live; a short debounce keeps a spin box being dragged from
        # re-rendering on every intermediate value.
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(120)
        self._apply_timer.timeout.connect(self.apply)
        self._build_ui()
        self.resize(430, 700)      # tall enough for the error-bar group; scrolls below that

    def _build_ui(self):
        v = QVBoxLayout(self)
        multi = len(self._entries) > 1
        self.tabs = QTabWidget() if multi else None

        for entry in self._entries:
            form = TraceStyleForm(entry.get("style") or {})
            form.changed.connect(self._schedule_apply)
            self._forms[entry["key"]] = form
            if multi:
                self.tabs.addTab(self._scrolled(form), entry.get("label", "Trace"))
            else:
                head = QLabel(self._entries[0].get("label", ""))
                head.setObjectName("sectionHeader")
                v.addWidget(head)
                v.addWidget(self._scrolled(form), 1)

        # Settings as they were when the dialog opened, so Cancel can put them
        # back (edits apply live, so closing must undo them).
        self._original = self.styles()

        if multi:
            v.addWidget(self.tabs, 1)
            copy_btn = QPushButton("Copy to all")
            copy_btn.setToolTip("Apply the settings on this tab to every trace")
            copy_btn.clicked.connect(self._copy_to_all)
            v.addWidget(copy_btn)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        # Changes apply as they are made, so Enter is left to the field being
        # edited and ⌘/Ctrl+Enter closes the dialog.
        add_accept_shortcuts(self, self._accept)
        bb.button(QDialogButtonBox.Ok).setText("Done  (⌘⏎)" if sys.platform == "darwin"
                                               else "Done  (Ctrl+⏎)")
        v.addWidget(bb)
        no_default_buttons(self)

    @staticmethod
    def _scrolled(widget):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(widget)
        return area

    # Fields that are per-trace identity rather than formatting, so "Copy to
    # all" must leave them alone.
    NOT_COPYABLE = ("display_name",)

    def _copy_to_all(self):
        """Push the visible tab's settings onto every other trace, keeping each
        trace's own name and error-bar applicability."""
        if self.tabs is None:
            return
        keys = list(self._forms)
        src_key = keys[self.tabs.currentIndex()]
        src = self._forms[src_key].style()
        for key, form in self._forms.items():
            if key == src_key:
                continue
            merged = form.base_style()
            merged.update({k: v for k, v in src.items()
                           if k not in self.NOT_COPYABLE
                           and (merged.get("has_error_bars")
                                or not k.startswith("err_"))})
            form.set_style(merged)
        self._schedule_apply()

    def styles(self):
        """key → edited style dict, for every trace in the dialog."""
        return {key: form.style() for key, form in self._forms.items()}

    def _schedule_apply(self):
        if self._on_apply is not None:
            self._apply_timer.start()

    def apply(self):
        """Push the current settings to the host (called automatically as the
        controls change, and once more when the dialog is closed with Done)."""
        self._apply_timer.stop()
        if self._on_apply is not None:
            self._on_apply(self.styles())

    def _accept(self):
        self.apply()
        self.accept()

    def reject(self):
        """Cancel: restore the formatting the dialog opened with, then close."""
        self._apply_timer.stop()
        if self._on_apply is not None and self._original:
            self._on_apply(self._original)
        super().reject()
