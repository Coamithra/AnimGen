"""The Assets tab's "Replace background" dialog.

Picks a SOURCE screen colour to key out (prefilled by snapping the asset's corner sample to
the nearest supported chroma; editable, includes an AUTO corner-threshold fallback) and a
separate TARGET fill colour to composite onto (default magenta, the generation contract).
Thin Qt over pipeline.bg_replace: it only gathers the two choices — the keying/compositing
and persistence live in AssetsView. Built without .exec() in the constructor so it's
headless-constructable (rule #4); the caller execs it.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from pipeline import bg_replace


class BackgroundReplaceDialog(QDialog):
    def __init__(self, prefill_source: str, initial_fill=bg_replace.CONTRACT_FILL,
                 reusing: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Replace background")
        self._fill = (int(initial_fill[0]), int(initial_fill[1]), int(initial_fill[2]))

        self.source_combo = QComboBox()
        self.source_combo.addItem(bg_replace.AUTO)
        self.source_combo.addItems(list(bg_replace.SUPPORTED_CHROMA))
        i = self.source_combo.findText(prefill_source)
        self.source_combo.setCurrentIndex(i if i >= 0 else 0)
        if reusing:
            # The asset already has a stored transparent sprite; Replace only re-fills it, so
            # the source screen is not re-keyed. Disable the selector rather than imply it does.
            self.source_combo.setEnabled(False)
            self.source_combo.setToolTip(
                "This asset already has a cleaned transparent sprite — Replace background will "
                "reuse it and only change the fill (the source screen is not re-keyed).")

        self.fill_btn = QPushButton()
        self.fill_btn.clicked.connect(self._pick_fill)
        self._refresh_fill_btn()
        fill_row = QHBoxLayout()
        fill_row.addWidget(self.fill_btn)
        fill_row.addWidget(QLabel("magenta recommended for generation"))
        fill_row.addStretch(1)

        form = QFormLayout()
        form.addRow("Screen to remove:", self.source_combo)
        form.addRow("Fill with:", fill_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Key the screen colour out and composite the sprite onto a\n"
                             "solid fill so the video model gets a flat, opaque keyframe."))
        lay.addLayout(form)
        lay.addWidget(buttons)

    def _pick_fill(self) -> None:
        c = QColorDialog.getColor(QColor(*self._fill), self, "Fill colour")
        if c.isValid():
            self._fill = (c.red(), c.green(), c.blue())
            self._refresh_fill_btn()

    def _refresh_fill_btn(self) -> None:
        r, g, b = self._fill
        self.fill_btn.setText(f"#{r:02X}{g:02X}{b:02X}")
        # readable text on either a light or dark swatch
        fg = "#000" if (r + g + b) > 384 else "#fff"
        self.fill_btn.setStyleSheet(f"background-color: rgb({r},{g},{b}); color: {fg};")

    def source(self) -> str:
        return self.source_combo.currentText()

    def fill_rgb(self) -> tuple[int, int, int]:
        return self._fill
