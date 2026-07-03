"""Widget-tree introspection + action primitives for the remote-control server.

Pure GUI-thread helpers (no server, no threads) so they're unit-testable headless:
build a small offscreen widget tree, snapshot it, resolve a target, drive it. The HTTP
layer (remote/server.py) only marshals these onto the GUI thread via the bridge.

A widget's ``ref`` is its ``objectName`` when set, else a deterministic ``Class:ordinal``
(ordinal = its index among same-class descendants in findChildren order). A ref resolves
on a re-walk as long as the tree is unchanged; ``objectName`` / visible ``text`` are the
stable selectors, while ``Class:ordinal`` is best-effort (any added/removed/shown sibling
shifts later ordinals).
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QLabel, QLineEdit,
    QPlainTextEdit, QSpinBox, QTabBar, QTabWidget, QTextEdit, QWidget,
)

# QTabBar and QTabWidget share the count()/tabText()/currentIndex()/setCurrentIndex() API,
# so they're handled together for snapshot detail + tab-switching.
_TABS = (QTabBar, QTabWidget)

# Widgets always worth surfacing, even when they carry no text.
_INTERACTIVE = (
    QAbstractButton, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QTabBar, QTabWidget,
    QSpinBox, QDoubleSpinBox)

# Single-line text accessors, tried in order.
_TEXT_GETTERS = ("text", "currentText", "title")

_KEYS = {
    "enter": Qt.Key.Key_Return, "return": Qt.Key.Key_Return,
    "tab": Qt.Key.Key_Tab, "backtab": Qt.Key.Key_Backtab,
    "escape": Qt.Key.Key_Escape, "esc": Qt.Key.Key_Escape,
    "backspace": Qt.Key.Key_Backspace, "delete": Qt.Key.Key_Delete, "del": Qt.Key.Key_Delete,
    "space": Qt.Key.Key_Space,
    "up": Qt.Key.Key_Up, "down": Qt.Key.Key_Down,
    "left": Qt.Key.Key_Left, "right": Qt.Key.Key_Right,
    "home": Qt.Key.Key_Home, "end": Qt.Key.Key_End,
    "pageup": Qt.Key.Key_PageUp, "pagedown": Qt.Key.Key_PageDown,
}


def _class_name(w: QWidget) -> str:
    return type(w).__name__


def _widget_text(w: QWidget) -> str:
    for attr in _TEXT_GETTERS:
        fn = getattr(w, attr, None)
        if callable(fn):
            try:
                val = fn()
            except Exception:  # noqa: BLE001 - some getters need args / raise; just skip
                continue
            if isinstance(val, str) and val:
                return val
    for attr in ("toPlainText", "placeholderText"):  # editors: short preview only
        fn = getattr(w, attr, None)
        if callable(fn):
            try:
                val = fn()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(val, str) and val:
                return val[:80]
    return ""


def _is_relevant(w: QWidget) -> bool:
    if isinstance(w, _INTERACTIVE):
        return True
    if isinstance(w, (QLabel, QGroupBox)) and _widget_text(w):
        return True
    return bool(w.objectName())  # an explicitly named widget is worth surfacing


def _window_rect(window: QWidget, w: QWidget) -> list[int]:
    """Top-left + size in window coordinates (matches a /screenshot of window.grab())."""
    try:
        tl = w.mapTo(window, QPoint(0, 0))
    except Exception:  # noqa: BLE001 - not a descendant; fall back to global
        tl = w.mapToGlobal(QPoint(0, 0))
    return [tl.x(), tl.y(), w.width(), w.height()]


def _describe(window: QWidget, w: QWidget, ordinal: int) -> dict[str, Any]:
    d: dict[str, Any] = {
        "ref": w.objectName() or f"{_class_name(w)}:{ordinal}",
        "class": _class_name(w),
        "name": w.objectName(),
        "text": _widget_text(w),
        "rect": _window_rect(window, w),
        "enabled": w.isEnabled(),
    }
    if isinstance(w, _TABS):
        d["tabs"] = [w.tabText(i) for i in range(w.count())]
        d["current"] = w.currentIndex()
    if isinstance(w, QComboBox):
        d["options"] = [w.itemText(i) for i in range(w.count())]
    if isinstance(w, QCheckBox):
        d["checked"] = w.isChecked()
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        d["value"] = w.value()
    return d


def build_snapshot(window: QWidget) -> list[dict[str, Any]]:
    """Flat list of the relevant *visible* widgets under ``window``."""
    descendants = window.findChildren(QWidget)
    ordinals: dict[int, int] = {}
    counts: dict[str, int] = {}
    for w in descendants:
        cls = _class_name(w)
        ordinals[id(w)] = counts.get(cls, 0)
        counts[cls] = counts.get(cls, 0) + 1
    return [
        _describe(window, w, ordinals[id(w)])
        for w in descendants
        if w.isVisible() and _is_relevant(w)
    ]


def resolve_target(
    window: QWidget,
    ref: Optional[str] = None,
    object_name: Optional[str] = None,
    text: Optional[str] = None,
) -> Optional[QWidget]:
    """Find a widget by objectName, ref (objectName or ``Class:ordinal``), or visible text.
    The text path prefers visible widgets, then within each visibility group matches exact
    before case-insensitive substring."""
    if object_name:
        w = window.findChild(QWidget, object_name)
        if w:
            return w
    if ref:
        w = window.findChild(QWidget, ref)
        if w:
            return w
        if ":" in ref:
            cls, _, idx = ref.rpartition(":")
            same = [x for x in window.findChildren(QWidget) if _class_name(x) == cls]
            try:
                n = int(idx)
            except ValueError:
                return None
            if n < 0 or n >= len(same):  # reject negative ordinals (no Python wrap-around)
                return None
            return same[n]
    if text:
        want = text.strip()
        if not want:  # whitespace-only would otherwise substring-match every widget
            return None
        relevant = [w for w in window.findChildren(QWidget) if _is_relevant(w)]
        # Prefer visible candidates (matching build_snapshot's visibility gate): run the full
        # exact-then-substring match within the visible widgets before falling back to hidden
        # ones, so a /click by text lands on the on-screen control rather than a hidden
        # duplicate on an inactive tab. A hidden match is returned only when no visible widget
        # matches at all.
        visible = [w for w in relevant if w.isVisible()]
        hidden = [w for w in relevant if not w.isVisible()]
        for group in (visible, hidden):
            for w in group:
                if _widget_text(w).strip() == want:
                    return w
            for w in group:
                if want.lower() in _widget_text(w).strip().lower():
                    return w
    return None


# ---- actions (all run on the GUI thread) --------------------------------------

def grab_png(widget: QWidget) -> bytes:
    """PNG bytes of a widget via Qt's own compositor — no OS screen-capture needed."""
    pix = widget.grab()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def do_click(widget: QWidget) -> None:
    if isinstance(widget, QAbstractButton):
        widget.click()
        return
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=widget.rect().center())


def do_type(widget: QWidget, text: str) -> None:
    """Synthesize keystrokes (exercises the widget's own key handling). Reliable only for
    simple single-line ASCII: newlines/tabs and many unicode codepoints don't map to key
    events and are silently dropped — use do_set (the /set route) for multi-line or
    unicode text."""
    QTest.keyClicks(widget, text)


def do_key(widget: QWidget, key_name: str) -> None:
    key = _KEYS.get(key_name.strip().lower())
    if key is None:
        raise ValueError(f"unknown key {key_name!r}; known: {sorted(_KEYS)}")
    QTest.keyClick(widget, key)


def do_set(
    widget: QWidget,
    value: Optional[Any] = None,
    checked: Optional[bool] = None,
) -> dict[str, Any]:
    """Set a value directly (more reliable than synthesizing keystrokes): check state,
    combo current text, tab by title, or line/plain-text edit text."""
    if checked is not None:
        if not hasattr(widget, "setChecked"):
            raise ValueError(f"{type(widget).__name__} cannot hold a checked state")
        if hasattr(widget, "isCheckable") and not widget.isCheckable():
            raise ValueError(f"{type(widget).__name__} is not checkable")
        widget.setChecked(bool(checked))
        return {"checked": widget.isChecked()}
    if value is not None:
        if isinstance(widget, QComboBox):
            want = str(value)
            if widget.isEditable():
                widget.setCurrentText(want)
                return {"currentText": widget.currentText()}
            # A non-editable combo silently ignores setCurrentText for an unknown value, so
            # match against the item list and report an honest failure on a miss (not ok:true).
            idx = widget.findText(want)
            if idx < 0:
                options = [widget.itemText(i) for i in range(widget.count())]
                raise ValueError(f"no option {want!r} in combo (options: {options})")
            widget.setCurrentIndex(idx)
            return {"currentText": widget.currentText()}
        if isinstance(widget, _TABS):
            for i in range(widget.count()):
                if widget.tabText(i) == str(value):
                    widget.setCurrentIndex(i)
                    return {"current": widget.currentIndex()}
            raise ValueError(f"no tab named {value!r}")
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            cast = float if isinstance(widget, QDoubleSpinBox) else int
            try:
                num = cast(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{type(widget).__name__} needs a numeric value, got {value!r}") from exc
            widget.setValue(num)
            return {"value": widget.value()}
        if isinstance(widget, (QPlainTextEdit, QTextEdit)):
            widget.setPlainText(str(value))
            return {"text": str(value)}
        if hasattr(widget, "setText"):
            widget.setText(str(value))
            return {"text": str(value)}
    raise ValueError("nothing to set (provide 'value' or 'checked')")
