"""The 'Generate batch...' dialog - scope, takes-per-shot, and the when-finished power action.

Thin Qt over backends.batch: it only collects three choices. The actual planning
(eligibility, the cost-gate item list) and the report/power logic live in backends.batch
so they're headless-testable; this dialog has no business logic to unit-test.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QRadioButton,
    QSpinBox, QVBoxLayout,
)

from backends import batch

SCOPE_ALL = "all"
SCOPE_VIEW = "view"


class BatchDialog(QDialog):
    def __init__(self, parent=None, *, view_count: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Generate batch")
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("Queue every eligible shot for an unattended (overnight) run.\n"
                             "You'll confirm the total cost once before anything launches."))

        self._all = QRadioButton("All shots in this project")
        self._view = QRadioButton(f"Only the current filtered view ({view_count} shown)")
        self._all.setChecked(True)
        lay.addWidget(self._all)
        lay.addWidget(self._view)

        form = QFormLayout()
        self._count = QSpinBox()
        self._count.setRange(1, 20)
        self._count.setValue(1)
        self._count.setToolTip("Takes to generate per shot. Random-seed shots vary each take.")
        form.addRow("Takes per shot:", self._count)

        self._power = QComboBox()
        self._power.addItem("Do nothing", batch.POWER_NONE)
        self._power.addItem("Stop ComfyUI when finished", batch.POWER_STOP_COMFY)
        # "Sleep/hibernate": on Windows SetSuspendState hibernates instead if hibernation
        # is enabled (the documented rundll32 limitation), so the label can't promise sleep.
        self._power.addItem("Sleep/hibernate the PC when finished", batch.POWER_SLEEP)
        self._power.setToolTip("What to do once the whole batch has drained.")
        form.addRow("When finished:", self._power)
        lay.addLayout(form)

        bb = QDialogButtonBox()
        bb.addButton(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        bb.addButton("Continue...", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def scope(self) -> str:
        return SCOPE_VIEW if self._view.isChecked() else SCOPE_ALL

    def takes_per_shot(self) -> int:
        return self._count.value()

    def power_action(self) -> str:
        return self._power.currentData()
