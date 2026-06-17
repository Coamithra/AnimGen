"""ComfyUI monitor window.

A separate, live view of the local ComfyUI backend: status, version, memory use
(RAM + VRAM), what it's working on (queue), launch settings (argv), and installed
models. Read-only except for a Launch button (when the server is down) and a manual
models refresh.

Like the Launch-ComfyUI flow, polling runs OFF the GUI thread: probing a down localhost
port costs a full socket timeout on this machine, so a GUI-thread poll would freeze the
window. `_MonitorPoller` polls comfy_client.monitor_snapshot() on a daemon thread and
delivers each snapshot via a queued signal; the models list (heavier, rarely changes) is
fetched on open and on demand by `_ModelsFetcher`.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from backends import comfy_client

_GB = 1024 ** 3


class _MonitorPoller(QObject):
    """Polls live ComfyUI state on a daemon thread until stopped."""
    snapshot = Signal(object)  # the monitor_snapshot() dict

    def __init__(self, interval: float = 2.0):
        super().__init__()
        self._interval = interval
        self._stop = False

    def start(self) -> None:
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        while not self._stop:
            self.snapshot.emit(comfy_client.monitor_snapshot(timeout=2))
            for _ in range(max(1, int(self._interval / 0.1))):  # responsive to stop()
                if self._stop:
                    return
                time.sleep(0.1)


class _ModelsFetcher(QObject):
    """One-shot off-thread fetch of the installed-models map."""
    fetched = Signal(object)  # {folder: [filenames]}

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self.fetched.emit(comfy_client.list_models(timeout=10))


class _AsyncCall(QObject):
    """Runs a blocking client call (stop work / shut down) off the GUI thread."""
    done = Signal(bool, str)  # ok, message

    def __init__(self, fn, ok_msg: str):
        super().__init__()
        self._fn = fn
        self._ok_msg = ok_msg

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self._fn()
            self.done.emit(True, self._ok_msg)
        except Exception as e:  # noqa: BLE001 - surface any failure to the user
            self.done.emit(False, f"{type(e).__name__}: {e}")


class ComfyMonitorWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ComfyUI Monitor")
        self.resize(640, 760)
        self._busy = False  # an async stop/shutdown is in flight - freeze button states
        self._models_fetched_once = False
        self._build()

        self._poller = _MonitorPoller()
        self._poller.snapshot.connect(self._apply_snapshot)
        # Polling is driven by the host via start_monitoring()/stop_monitoring() so that,
        # embedded as a tab, this only probes the (possibly down) port while on screen -
        # a closed localhost port costs a full socket timeout per probe on this machine.

    # ---- monitoring lifecycle -------------------------------------------
    def start_monitoring(self) -> None:
        """Begin live polling; fetch the installed-models list once, lazily."""
        self._poller.start()
        if not self._models_fetched_once:
            self._models_fetched_once = True
            self._fetch_models()

    def stop_monitoring(self) -> None:
        self._poller.stop()

    # ---- construction ---------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # status header + actions
        self.status_lbl = QLabel("Checking...")
        self.status_lbl.setMinimumHeight(28)
        self.launch_btn = QPushButton("Launch ComfyUI")
        self.launch_btn.setToolTip("Start ComfyUI with --disable-dynamic-vram")
        self.launch_btn.clicked.connect(self._launch)
        self.stop_work_btn = QPushButton("Stop working")
        self.stop_work_btn.setToolTip("Interrupt the current render and clear the queue (server stays up)")
        self.stop_work_btn.clicked.connect(self._stop_work)
        self.shutdown_btn = QPushButton("Shut down")
        self.shutdown_btn.setToolTip("Stop the ComfyUI server process")
        self.shutdown_btn.clicked.connect(self._shutdown)
        head = QHBoxLayout()
        head.addWidget(self.status_lbl, 1)
        head.addWidget(self.launch_btn)
        head.addWidget(self.stop_work_btn)
        head.addWidget(self.shutdown_btn)
        root.addLayout(head)

        self.versions_lbl = QLabel("-")
        self.versions_lbl.setStyleSheet("color: gray;")
        root.addWidget(self.versions_lbl)

        # memory
        mem_box = QGroupBox("Memory")
        mem = QVBoxLayout(mem_box)
        self.ram_bar = self._make_bar()
        self.vram_bar = self._make_bar()
        mem.addWidget(QLabel("System RAM"))
        mem.addWidget(self.ram_bar)
        mem.addWidget(QLabel("GPU VRAM"))
        mem.addWidget(self.vram_bar)
        root.addWidget(mem_box)

        # activity
        act_box = QGroupBox("Activity")
        act = QVBoxLayout(act_box)
        self.activity_lbl = QLabel("-")
        act.addWidget(self.activity_lbl)
        root.addWidget(act_box)

        # settings (launch argv)
        set_box = QGroupBox("Launch settings (argv)")
        st = QVBoxLayout(set_box)
        self.settings_view = QPlainTextEdit()
        self.settings_view.setReadOnly(True)
        self.settings_view.setMaximumHeight(64)
        st.addWidget(self.settings_view)
        root.addWidget(set_box)

        # models installed
        mod_box = QGroupBox("Models installed")
        md = QVBoxLayout(mod_box)
        self.refresh_models_btn = QPushButton("Refresh models")
        self.refresh_models_btn.clicked.connect(self._fetch_models)
        mod_head = QHBoxLayout()
        mod_head.addStretch(1)
        mod_head.addWidget(self.refresh_models_btn)
        md.addLayout(mod_head)
        self.models_tree = QTreeWidget()
        self.models_tree.setHeaderLabels(["Folder / file", "Count"])
        self.models_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.models_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        md.addWidget(self.models_tree)
        root.addWidget(mod_box, 1)

    def _make_bar(self) -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFormat("n/a")
        return bar

    # ---- live updates ---------------------------------------------------
    def _apply_snapshot(self, snap: dict) -> None:
        if not snap.get("running"):
            self._set_status("OFFLINE", "#b03030")
            self._set_buttons(running=False)
            self.versions_lbl.setText("-")
            self.activity_lbl.setText("server not running")
            self.settings_view.setPlainText("(offline)")
            for bar in (self.ram_bar, self.vram_bar):
                bar.setValue(0)
                bar.setFormat("n/a")
            return

        self._set_buttons(running=True)
        dv = snap.get("dynamic_vram")
        ao = snap.get("async_offload")
        ver = snap.get("version") or "?"
        if dv or ao:  # amber: up but misconfigured - local jobs will be refused by preflight
            on = " + ".join(p for p, f in (("dynamic VRAM", dv), ("async offload", ao)) if f)
            self._set_status(f"RUNNING  -  ComfyUI {ver}  -  {on} ENABLED (TDR risk)", "#b07000")
        else:
            self._set_status(f"RUNNING  -  ComfyUI {ver}  -  weight streaming off", "#2e7d32")

        self.versions_lbl.setText(
            f"Python {snap.get('python_version') or '?'}   |   "
            f"PyTorch {snap.get('pytorch_version') or '?'}   |   {snap.get('os') or ''}")

        self._set_mem(self.ram_bar, snap.get("ram_total"), snap.get("ram_free"))
        self._set_mem(self.vram_bar, snap.get("vram_total"), snap.get("vram_free"),
                      label=snap.get("device_name"))

        running, pending = snap.get("queue_running", 0), snap.get("queue_pending", 0)
        if running:
            rp = str(snap.get("running_prompt") or "?")[:8]
            extra = f"  (+{pending} queued)" if pending else ""
            self.activity_lbl.setText(f"working on {rp}{extra}")
        elif pending:
            self.activity_lbl.setText(f"{pending} queued")
        else:
            self.activity_lbl.setText("idle")

        argv = snap.get("argv") or []
        self.settings_view.setPlainText(" ".join(argv) if argv else "(no argv reported)")

    def _set_status(self, text: str, color: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(
            f"QLabel {{ background: {color}; color: white; padding: 5px 8px; "
            "border-radius: 4px; font-weight: bold; }}")

    def _set_mem(self, bar: QProgressBar, total, free, label=None) -> None:
        if not total:
            bar.setValue(0)
            bar.setFormat("n/a")
            return
        used = max(0, total - (free or 0))
        pct = int(used * 100 / total)
        prefix = f"{label}:  " if label else ""
        bar.setValue(pct)
        bar.setFormat(f"{prefix}{used / _GB:.1f} / {total / _GB:.1f} GB  ({pct}%)")

    # ---- actions --------------------------------------------------------
    def _set_buttons(self, running: bool) -> None:
        """Launch when down; Stop working / Shut down when up. Frozen while an async
        stop/shutdown action is mid-flight (so a stale poll can't re-enable them)."""
        if self._busy:
            return
        self.launch_btn.setEnabled(not running)
        self.stop_work_btn.setEnabled(running)
        self.shutdown_btn.setEnabled(running)

    def _launch(self) -> None:
        try:
            comfy_client.launch_server()
        except comfy_client.ComfyError as e:
            QMessageBox.warning(self, "Launch ComfyUI", str(e))
            return
        self.launch_btn.setEnabled(False)
        self.activity_lbl.setText("starting... (first model load can take a minute)")
        # the poller will reflect the server coming up

    def _stop_work(self) -> None:
        self.activity_lbl.setText("stopping current work...")
        self._run_action(comfy_client.stop_work, "stopped current work")

    def _shutdown(self) -> None:
        if QMessageBox.question(
                self, "Shut down ComfyUI",
                "Stop the ComfyUI server? Any running render will be lost.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.activity_lbl.setText("shutting down...")
        self._run_action(comfy_client.stop_server, "ComfyUI shut down")

    def _run_action(self, fn, ok_msg: str) -> None:
        self._busy = True
        for b in (self.launch_btn, self.stop_work_btn, self.shutdown_btn):
            b.setEnabled(False)
        self._action = _AsyncCall(fn, ok_msg)  # kept on self so it isn't GC'd
        self._action.done.connect(self._on_action_done)
        self._action.start()

    def _on_action_done(self, ok: bool, message: str) -> None:
        self._busy = False
        if not ok:
            QMessageBox.warning(self, "ComfyUI", message)
        # the next poller snapshot re-sets the button states from live status

    def _fetch_models(self) -> None:
        self.refresh_models_btn.setEnabled(False)
        self.models_tree.clear()
        self.models_tree.addTopLevelItem(QTreeWidgetItem(["loading...", ""]))
        self._models_fetcher = _ModelsFetcher()  # kept on self so it isn't GC'd
        self._models_fetcher.fetched.connect(self._apply_models)
        self._models_fetcher.start()

    def _apply_models(self, models: dict) -> None:
        self.refresh_models_btn.setEnabled(True)
        self.models_tree.clear()
        if not models:
            self.models_tree.addTopLevelItem(
                QTreeWidgetItem(["(server offline, or no model folders)", ""]))
            return
        for folder in sorted(models):
            files = models.get(folder) or []
            top = QTreeWidgetItem([folder, str(len(files))])
            for name in files:
                top.addChild(QTreeWidgetItem([name, ""]))
            self.models_tree.addTopLevelItem(top)

    # ---- teardown -------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._poller.stop()
        super().closeEvent(event)
