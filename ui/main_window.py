"""Main window.

Shows a project's shots as expandable cards (header + inline takes folder view), with
global filters (model, starred). Generate resolves a shot's model + params, runs the
cost-confirm gate, creates a pending take with an immutable settings_snapshot, and
enqueues a background job whose status streams into the log panel and refreshes the
originating card.

The window owns the current Project document and the File-menu lifecycle (New / Open /
Save / Save As). Authoring edits buffer (dirty marker in the title; prompt before
discarding); finished takes auto-persist (see store/project.py).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

import applog
import library
import paths
from qt_guard import guarded_emit
from backends import batch, comfy_client, crash_recovery, recovery, replicate_client, restart
from backends.jobs import JobManager
from pipeline import export, extract, framing
from store import app_settings
from store.project import Project
from store.models import (
    STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING, Shot,
)
from ui.assets_view import AssetsView
from ui.batch_dialog import BatchDialog, SCOPE_VIEW
from ui.shot_card import ShotCard
from ui.comfy_monitor_window import ComfyMonitorWindow
from ui.shot_tab import ShotTab
from ui.cost_confirm import confirm_launch, total_price_text
from ui.model_library_window import ModelLibraryWindow
from ui.queue_view import QueueView
from ui.take_player import TakePlayerTab

# settings keys passed to the hosted client explicitly (everything else -> extra/--set)
_EXPLICIT_SETTINGS = ("duration", "resolution", "seed", "length")
_PROJECT_FILTER = "AnimGen project (*.animproj)"


def _skipped_text(skipped: list) -> str:
    """Bullet list of '(name): reason' for shots a batch can't generate."""
    return "\n".join(f"  - {name}: {reason}" for name, reason in skipped)


def recovery_banner_text(interrupted_count: int) -> Optional[str]:
    """The one-time load-banner message when a prior session left takes interrupted, else
    None (pure, so it's smoke-testable without a window). Crash recovery is otherwise
    silent - only log-dock lines and a button in a maybe-closed tab (review UX #3)."""
    if interrupted_count <= 0:
        return None
    n = interrupted_count
    return (f"Your last session ended while {n} take{'' if n == 1 else 's'} "
            f"{'was' if n == 1 else 'were'} interrupted - restart them?")


def generating_takes(project) -> list:
    """Takes currently mid-render (status GENERATING), either backend - the ones that keep
    spending money (hosted) or GPU (local) if the app exits without stopping them. Pure, so
    the closeEvent decision is smoke-testable without a window. Excludes binned takes: a
    delete-to-bin already neutralized them in the queue (H2)."""
    return [t for t in project.list_takes() if t.status == STATUS_GENERATING]


def close_warning_text(takes: list) -> Optional[str]:
    """The closeEvent warning body when takes are still rendering, else None (pure). Notes the
    hosted-vs-local split so the user knows some renders are still BILLING on Replicate."""
    if not takes:
        return None
    n = len(takes)
    hosted = sum(1 for t in takes
                 if (t.settings_snapshot or {}).get("backend") == "replicate")
    lines = [f"{n} take{'' if n == 1 else 's'} {'is' if n == 1 else 'are'} still rendering."]
    if hosted:
        lines.append(f"{hosted} {'is' if hosted == 1 else 'are'} hosted (Replicate) and will "
                     "keep billing on Replicate's servers after this window closes.")
    lines.append("Close anyway, or stop the in-flight renders first?")
    return "\n\n".join(lines)


class _OrphanReconciler(QObject):
    """Off-thread fetch of ComfyUI /history + /queue for orphan-take recovery.

    Probing a down localhost port costs a full socket timeout on this machine, so the
    fetch runs on a daemon thread and results are applied on the GUI thread. Emits
    (history, queue); both are None if ComfyUI is unreachable."""
    ready = Signal(object, object)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            hist = comfy_client.history_view(timeout=4)
            queue = comfy_client.queue_view(timeout=4)
        except Exception:  # noqa: BLE001 - unreachable/down server -> recover what we can offline
            hist = queue = None
        # Guard the emit (card #48): _run is a daemon thread; if the window tears down before the
        # fetch returns, a raw emit would raise 'Signal source has been deleted' and abort the
        # process at the C++ layer.
        guarded_emit(self, "ready", hist, queue)


class _ReplicateReconciler(QObject):
    """Off-thread poll of each orphaned hosted take's prediction (an idempotent GET - it
    OBSERVES, never re-runs or re-charges). Runs on a daemon thread because a slow/unreachable
    Replicate would otherwise block the GUI thread. Emits {take_id: prediction_or_None}: a take
    with no backend_job_id, or one whose poll raised (network down / HTTP error), maps to None
    so the planner marks it FAILED + interrupted (restartable) rather than a frozen zombie."""
    ready = Signal(object)

    def __init__(self, orphans, parent=None):
        super().__init__(parent)
        # (id, backend_job_id) snapshot so the worker never touches the live take objects.
        self._items = [(o.id, o.backend_job_id) for o in orphans]

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        statuses: dict = {}
        for take_id, job_id in self._items:
            if not job_id:
                statuses[take_id] = None      # never recorded a prediction id -> unverifiable
                continue
            try:
                statuses[take_id] = replicate_client.get_prediction(job_id)
            except Exception:  # noqa: BLE001 - unreachable/HTTP error -> unverifiable, don't crash
                statuses[take_id] = None
        self.ready.emit(statuses)


class _InfoBanner(QFrame):
    """A dismissable, NON-modal inline notice strip (message + one action + a close x),
    docked at the top of the Shots tab. Non-modal on purpose: headless smoke must never
    block on a .exec() (rule #4). Hidden until show_message() arms it; the close x or the
    action's own handler hides it again."""

    def __init__(self, action_text: str, on_action, parent=None):
        super().__init__(parent)
        self.setObjectName("infoBanner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "#infoBanner { background: #3a3320; border: 1px solid #6b5d2f; border-radius: 4px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        self._label = QLabel()
        self._label.setWordWrap(True)
        lay.addWidget(self._label, 1)
        action_btn = QPushButton(action_text)
        action_btn.clicked.connect(on_action)
        lay.addWidget(action_btn)
        close_btn = QPushButton("✕")   # x
        close_btn.setObjectName("infoBannerClose")
        close_btn.setToolTip("Dismiss")
        close_btn.setFixedWidth(24)
        close_btn.setFlat(True)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.hide()

    def show_message(self, text: str) -> None:
        self._label.setText(text)
        self.show()


class MainWindow(QMainWindow):
    def __init__(self, project: Project):
        super().__init__()
        self.project = project
        self.cards: dict[str, ShotCard] = {}
        self.shot_tabs: dict[str, ShotTab] = {}   # shot_id -> its open detail/edit tab
        self.take_tabs: dict[str, TakePlayerTab] = {}  # take_id -> its open viewer tab
        self._batch: Optional[batch.BatchRun] = None   # in-flight overnight batch, if any
        self._stop_paused_local = False   # transient non-batch local pause from a manual ComfyUI stop

        self.jobs = JobManager(project)
        self.jobs.progress.connect(self._on_progress)
        self.jobs.status_changed.connect(self._on_status_changed)
        self.jobs.finished.connect(lambda tid: self._after_job(tid, f"✓ done {tid[:8]}"))
        self.jobs.failed.connect(
            lambda tid, err: self._after_job(tid, f"✗ FAILED {tid[:8]}: {err}"))
        self.jobs.queue_abandoned.connect(self._on_queue_abandoned)

        self.resize(1180, 820)
        self._build_body()
        self._build_menu()
        self.reload()
        self._restore_tab_state()   # reopen the tabs this project was last saved with
        self._recover_orphans()   # reclaim/clear takes a prior session left mid-render
        self._refresh_recovery_banner()   # takes already interrupted on load (recovery is async)
        self._maybe_refresh_schemas_on_startup()
        self._remote = None
        self._maybe_start_remote()   # opt-in localhost control server (ANIMGEN_REMOTE)

    # ---- construction ---------------------------------------------------
    def _build_controls(self) -> QToolBar:
        """The Shots-tab control strip (filters + view actions). Lives inside the Shots
        tab, not above the tabs, since every control here acts only on that view."""
        tb = QToolBar("Shot controls")
        tb.setMovable(False)
        tb.addWidget(QLabel(" Model: "))
        self.model_filter = QComboBox()
        self.model_filter.setObjectName("modelFilter")
        self.model_filter.currentIndexChanged.connect(self.reload)
        tb.addWidget(self.model_filter)
        self.starred_filter = QCheckBox("Starred takes")
        self.starred_filter.setObjectName("starredFilter")
        self.starred_filter.setToolTip("Show only shots that have at least one starred take")
        self.starred_filter.stateChanged.connect(self.reload)
        tb.addWidget(self.starred_filter)
        self.starred_shots_filter = QCheckBox("Starred shots")
        self.starred_shots_filter.setObjectName("starredShotsFilter")
        self.starred_shots_filter.setToolTip("Show only shots you have starred")
        self.starred_shots_filter.stateChanged.connect(self.reload)
        tb.addWidget(self.starred_shots_filter)
        exp_view = QAction("Export view", self)
        exp_view.triggered.connect(self.export_current_view)
        tb.addAction(exp_view)
        exp_starred = QAction("Export starred takes", self)
        exp_starred.setToolTip("Export every starred take across the shots currently shown "
                               "(obeys the model / starred-shot view filters)")
        exp_starred.triggered.connect(self.export_starred_takes)
        tb.addAction(exp_starred)
        batch_act = QAction("Generate batch...", self)
        batch_act.setToolTip("Queue every eligible shot for an unattended (overnight) run, "
                             "with optional power-down when it finishes")
        batch_act.triggered.connect(self.start_batch)
        tb.addAction(batch_act)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.total_price = QLabel()
        self.total_price.setToolTip(
            "Estimated cost to generate every shot in this project once "
            "(summed over the per-shot estimates; local renders are free).")
        self.total_price.setStyleSheet("font-weight: 600; padding-right: 6px;")
        tb.addWidget(self.total_price)
        return tb

    def _build_menu(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        for label, shortcut, slot in (
            ("&New Project", "Ctrl+Shift+N", self.new_project),
            ("&Open Project...", "Ctrl+O", self.open_project),
            ("&Save", "Ctrl+S", self.save_project),
            ("Save &As...", "Ctrl+Shift+S", self.save_project_as),
        ):
            act = QAction(label, self)
            act.setShortcut(shortcut)
            act.triggered.connect(slot)
            file_menu.addAction(act)
        file_menu.addSeparator()
        new_shot_act = QAction("New &Shot", self)
        new_shot_act.setShortcut("Ctrl+N")
        new_shot_act.triggered.connect(self.new_shot)
        file_menu.addAction(new_shot_act)
        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        edit_menu = bar.addMenu("&Edit")
        self.purge_cancelled_act = QAction("Remove cancelled takes", self)
        self.purge_cancelled_act.setToolTip(
            "Permanently delete every cancelled take in this project, across all shots "
            "(ignores the view filters). This can't be undone.")
        self.purge_cancelled_act.triggered.connect(self.remove_cancelled_takes)
        edit_menu.addAction(self.purge_cancelled_act)

        view_menu = bar.addMenu("&View")
        for widget, name in self._fixed_tabs:
            act = QAction(name, self)
            act.triggered.connect(
                lambda _checked=False, w=widget, t=name: self._show_fixed_tab(w, t))
            view_menu.addAction(act)
        view_menu.addSeparator()
        # toggleViewAction is a checkable Show/Hide for the dock that tracks its visibility.
        view_menu.addAction(self.log_dock.toggleViewAction())

        settings_menu = bar.addMenu("&Settings")
        self.startup_fetch_act = QAction("Update Replicate model data on startup", self)
        self.startup_fetch_act.setCheckable(True)
        self.startup_fetch_act.setChecked(
            app_settings.get_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP))
        self.startup_fetch_act.toggled.connect(
            lambda on: app_settings.set_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP, on))
        settings_menu.addAction(self.startup_fetch_act)

    def _maybe_refresh_schemas_on_startup(self) -> None:
        """If the user opted in, kick off the off-thread Replicate schema fetch at launch
        (reuses the Model Library tab's fetcher; no GUI block, no-ops without a token)."""
        if app_settings.get_bool(app_settings.UPDATE_SCHEMAS_ON_STARTUP):
            self.library_tab.start_schema_fetch()

    def _maybe_start_remote(self) -> None:
        """Start the opt-in localhost control server (lets an external agent drive the GUI:
        screenshot / snapshot / click / type). Off unless ANIMGEN_REMOTE is truthy; binds
        127.0.0.1 only. See remote/server.py."""
        flag = os.environ.get("ANIMGEN_REMOTE", "").strip().lower()
        if flag in ("", "0", "false", "no", "off"):
            return
        try:
            from remote.server import RemoteControlServer
            self._remote = RemoteControlServer(self)
            port = self._remote.start()
            self._log(f"Remote control listening on http://127.0.0.1:{port}")
        except Exception as exc:  # noqa: BLE001 - never block startup on the dev-only server
            self._log(f"Remote control failed to start: {exc}")
            self._remote = None

    def _build_queue_actions(self) -> None:
        """The three generation-queue actions — Pause/Resume batch, Cancel pending, Restart
        interrupted takes. They live (visually) in the Queue tab header, but the QActions are
        owned by MainWindow so the many _refresh_*_action call sites keep driving their
        enabled/text state; QueueView just renders them as buttons."""
        self.pause_act = QAction("Pause batch", self)
        self.pause_act.setToolTip("Pause the running batch: hold its queued local takes "
                                  "(and optionally halt the current one), then Resume later")
        self.pause_act.triggered.connect(self.toggle_pause_batch)
        self.pause_act.setEnabled(False)
        self.cancel_act = QAction("Cancel pending", self)
        self.cancel_act.setToolTip("Cancel all queued generations that haven't started yet")
        self.cancel_act.triggered.connect(self.cancel_pending)
        self.cancel_act.setEnabled(False)
        self.restart_act = QAction("Restart interrupted takes", self)
        self.restart_act.setToolTip("Re-run takes that were cancelled by a crash or by ComfyUI/the "
                                    "app dying (not ones you cancelled yourself), from their frozen "
                                    "settings (same seed + framing). Takes that can't be replayed "
                                    "exactly are marked failed with a reason")
        self.restart_act.triggered.connect(self.restart_cancelled_takes)
        self.restart_act.setEnabled(False)

    def _build_body(self) -> None:
        self._build_queue_actions()   # created before QueueView, which renders them
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards_layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.cards_container)

        shots_tab = QWidget()
        shots_layout = QVBoxLayout(shots_tab)
        shots_layout.setContentsMargins(0, 0, 0, 0)
        # One-time crash-recovery notice (hidden until orphan recovery / load finds interrupted
        # takes); its action re-fires the same restart path as the Queue tab's button (rule #1
        # cost gate still applies inside restart_cancelled_takes).
        self.recovery_banner = _InfoBanner("Restart interrupted takes",
                                           self._restart_from_banner)
        shots_layout.addWidget(self.recovery_banner)
        shots_layout.addWidget(self._build_controls())
        shots_layout.addWidget(scroll, 1)

        # Model Library and ComfyUI Status used to be separate top-level windows; they're
        # now tabs alongside the shots view. The monitor only polls while its tab is on
        # screen (see _on_tab_changed) to avoid hammering a down port in the background.
        self.queue_tab = QueueView(self.project, self.jobs,
                                   queue_actions=[self.pause_act, self.cancel_act, self.restart_act])
        self.assets_tab = AssetsView(self.project)
        self.library_tab = ModelLibraryWindow(self)
        self.comfy_tab = ComfyMonitorWindow(self)
        self.comfy_tab.stop_intent.connect(self._pause_local_on_stop_intent)

        self.shots_tab = shots_tab
        # Fixed tabs are closable (the x) and reopen from the View menu; shot tabs are
        # dynamic (reopen by opening the shot again).
        self._fixed_tabs = [(shots_tab, "Shots"), (self.queue_tab, "Queue"),
                            (self.assets_tab, "Assets"),
                            (self.library_tab, "Model Library"),
                            (self.comfy_tab, "ComfyUI Status")]

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        for widget, title in self._fixed_tabs:
            self.tabs.addTab(widget, title)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self._on_tab_close)
        self.setCentralWidget(self.tabs)

        self._build_log_dock()

    def _build_log_dock(self) -> None:
        """The generation log lives in a dock panel at the bottom of the window, not inside
        the Shots tab: jobs fire from any shot/tab, so the log persists across tab switches.
        The dock has a drag-to-resize splitter handle and a close (x); reopen from View > Log."""
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Generation log...")
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setObjectName("logDock")
        self.log_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea)
        self.log_dock.setWidget(self.log)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.resizeDocks([self.log_dock], [160], Qt.Orientation.Vertical)

    # ---- project lifecycle ----------------------------------------------
    def _update_title(self) -> None:
        star = "*" if self._has_unsaved_changes() else ""
        self.setWindowTitle(f"{self.project.name}{star} - Animation Generator")

    def _has_unsaved_edits(self) -> bool:
        """Real unsaved edits a discard would lose: buffered authoring edits or an open
        shot tab with uncommitted editor edits. (A pristine untitled project has nothing
        to lose, so it doesn't arm the save-prompt — see _has_unsaved_changes.)"""
        if self.project.dirty:
            return True
        return any(isinstance(w, ShotTab) and w.is_dirty()
                   for w in (self.tabs.widget(i) for i in range(self.tabs.count())))

    def _has_unsaved_changes(self) -> bool:
        """Whether the window title shows the dirty marker: real unsaved edits, or the
        project has never been saved at all (untitled)."""
        return self.project.is_untitled or self._has_unsaved_edits()

    def _on_shot_dirty_changed(self, tab: ShotTab) -> None:
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, tab.title())
        self._update_title()

    def _switch_project(self, project: Project) -> None:
        # Both shot tabs and take-viewer tabs reference the old project's shots/takes - close
        # them before swapping the document so nothing dangles.
        for tab in list(self.shot_tabs.values()):
            idx = self.tabs.indexOf(tab)
            if idx >= 0:
                self.tabs.removeTab(idx)
            tab.deleteLater()
        self.shot_tabs.clear()
        for vtab in list(self.take_tabs.values()):
            idx = self.tabs.indexOf(vtab)
            if idx >= 0:
                self.tabs.removeTab(idx)
            vtab.close_player()
            vtab.deleteLater()
        self.take_tabs.clear()
        self.project = project
        self.jobs.set_project(project)
        self.assets_tab.set_project(project)
        self.queue_tab.set_project(project)
        self.reload()
        self._restore_tab_state()   # reopen the tabs this project was last saved with
        self.recovery_banner.hide()   # stale notice from the previous project
        self._recover_orphans()   # reclaim/clear takes a prior session left mid-render
        self._refresh_recovery_banner()   # takes already interrupted on load (recovery is async)

    def _maybe_save_changes(self) -> bool:
        """Prompt before discarding unsaved authoring edits. Return False to abort. Covers
        uncommitted shot-tab edits too (Save flushes them via _commit_open_shot_tabs)."""
        if not self._has_unsaved_edits():
            return True
        choice = QMessageBox.question(
            self, "Unsaved changes",
            f"Save changes to '{self.project.name}' before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self.save_project()
        return True  # Discard

    def new_project(self) -> None:
        if not self._maybe_save_changes():
            return
        self._switch_project(Project.new())
        self._log("new project")

    def open_project(self) -> None:
        if not self._maybe_save_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", str(paths.DATA_DIR), _PROJECT_FILTER)
        if not path:
            return
        try:
            project = Project.load(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Open project", f"Could not open:\n{e}")
            return
        self._switch_project(project)
        self._remember_last()
        self._log(f"opened {project.name}")

    def save_project(self) -> bool:
        if self.project.is_untitled:
            return self.save_project_as()
        self._commit_open_shot_tabs()
        self._capture_tab_state()
        self.project.save()
        self._remember_last()
        self._update_title()
        self.statusBar().showMessage(f"Saved {self.project.name}", 4000)
        return True

    def save_project_as(self) -> bool:
        start = str(self.project.path or (paths.DATA_DIR / f"{self.project.name}.animproj"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project as", start, _PROJECT_FILTER)
        if not path:
            return False
        self._commit_open_shot_tabs()
        self._capture_tab_state()
        self.project.save_as(path)
        self._remember_last()
        self._update_title()
        self.statusBar().showMessage(f"Saved {self.project.name}", 4000)
        return True

    def _remember_last(self) -> None:
        if self.project.path is None:
            return
        try:
            paths.APP_STATE.write_text(
                json.dumps({"last_project": str(self.project.path)}), encoding="utf-8")
        except OSError:
            pass

    # ---- data -----------------------------------------------------------
    def reload(self) -> None:
        self._refresh_model_filter()
        model_sel = self.model_filter.currentData()
        starred_only = self.starred_filter.isChecked()
        starred_shots_only = self.starred_shots_filter.isChecked()
        expanded = {sid for sid, c in self.cards.items() if c.expand_btn.isChecked()}

        while self.cards_layout.count():
            w = self.cards_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.cards.clear()

        shots = self.project.list_shots()
        shown = 0
        for shot in shots:
            if model_sel and shot.model_id != model_sel:
                continue
            if starred_shots_only and not shot.starred:
                continue
            if starred_only and not self.project.list_takes(shot.id, starred_only=True):
                continue
            card = ShotCard(self.project, shot, jobs=self.jobs)
            card.generate_requested.connect(self.generate_shot)
            card.open_requested.connect(self.open_shot)
            card.duplicate_requested.connect(self.duplicate_shot)
            card.delete_requested.connect(self.delete_shot)
            card.star_toggled.connect(self.toggle_shot_star)
            card.export_takes_requested.connect(self.export_takes)
            card.open_take_requested.connect(self.open_take)
            card.restart_requested.connect(self._restart_takes_by_ids)
            # binning a take from the card's grid must keep the recovery banner honest
            card.changed.connect(self._sync_recovery_banner)
            if shot.id in expanded:
                card.expand_btn.setChecked(True)
            self.cards_layout.addWidget(card)
            self.cards[shot.id] = card
            shown += 1

        self.cards_layout.addWidget(self._make_add_shot_card())
        self.statusBar().showMessage(f"{shown} shots shown · {len(shots)} total")
        self._refresh_total_price(shots)
        self._refresh_restart_action()   # a freshly-opened project may already hold cancelled takes
        self._refresh_purge_cancelled_action()
        self._update_title()

    def _refresh_total_price(self, shots) -> None:
        """Show the full-set generation cost: per-shot estimates over EVERY shot (the
        card asks for the whole set, so this ignores the model/starred view filters)."""
        costs = []
        for shot in shots:
            model = library.get_model(shot.model_id)
            settings = {**((model or {}).get("default_params") or {}), **shot.settings}
            costs.append(library.estimate_cost(shot.model_id, settings))
        self.total_price.setText(total_price_text(costs))

    def _make_add_shot_card(self) -> QPushButton:
        """Placeholder '+ New Shot' card at the end of the list (also the empty state)."""
        btn = QPushButton("+  New Shot")
        btn.setObjectName("addShotCard")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(56)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.clicked.connect(self.new_shot)
        btn.setStyleSheet(
            "QPushButton#addShotCard { border: 2px dashed #3a3f4b; border-radius: 6px;"
            " color: #9aa; font-size: 15px; background: transparent; }"
            "QPushButton#addShotCard:hover { border-color: #5fa97a; color: #cde6d6; }")
        return btn

    def _refresh_model_filter(self) -> None:
        self.model_filter.blockSignals(True)
        prev = self.model_filter.currentData()
        self.model_filter.clear()
        self.model_filter.addItem("All models", None)
        for mid in self.project.used_model_ids():
            model = library.get_model(mid)
            self.model_filter.addItem(model["display_name"] if model else mid, mid)
        idx = self.model_filter.findData(prev)
        self.model_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_filter.blockSignals(False)

    # ---- shot tabs ------------------------------------------------------
    def new_shot(self) -> None:
        tab = ShotTab(self.project, jobs=self.jobs)
        self._wire_shot_tab(tab)
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, tab.title()))

    def open_shot(self, shot_id: str) -> None:
        if shot_id in self.shot_tabs:
            self.tabs.setCurrentWidget(self.shot_tabs[shot_id])
            return
        shot = self.project.get_shot(shot_id)
        if not shot:
            return
        tab = ShotTab(self.project, shot=shot, jobs=self.jobs)
        self._wire_shot_tab(tab)
        self.shot_tabs[shot_id] = tab
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, tab.title()))

    def open_take(self, take_id: str) -> None:
        """Open a take in its own frame-by-frame viewer tab (one tab per take; re-opening a
        take just focuses the existing tab)."""
        if take_id in self.take_tabs:
            self.tabs.setCurrentWidget(self.take_tabs[take_id])
            return
        take = self.project.get_take(take_id)
        if not take:
            return
        shot = self.project.get_shot(take.shot_id)
        title = f"▶ {shot.name if shot else take.shot_id[:6]} · {take_id[:6]}"
        tab = TakePlayerTab(self.project, take_id)
        tab.star_changed.connect(self._on_take_star_changed)
        self.take_tabs[take_id] = tab
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, title))

    def _on_take_star_changed(self, take_id: str) -> None:
        """A take's star was toggled from its player tab: refresh the matching grid tile in
        place (card #75 incremental path) so the shot card / shot tab shows the new star, and
        the header star-filter counts stay accurate."""
        self._refresh_shot_for_take(take_id)

    def duplicate_shot(self, shot_id: str) -> None:
        dup = self.project.duplicate_shot(shot_id)
        if not dup:
            return
        self.reload()
        self._log(f"duplicated shot -> {dup.name}")

    def toggle_shot_star(self, shot_id: str) -> None:
        shot = self.project.get_shot(shot_id)
        if not shot:
            return
        self.project.set_shot_starred(shot_id, not shot.starred)   # write-through, no dirty
        # A full reload rebuilds every shot card (and its thumbnail grid) - 1-2s with many
        # shots. Only the "Starred shots" filter changes which cards are visible on a star
        # toggle, so reload only then; otherwise just repaint the one card's star button.
        if self.starred_shots_filter.isChecked():
            self.reload()
        else:
            card = self.cards.get(shot_id)
            if card:
                card._refresh_star_btn()

    def delete_shot(self, shot_id: str) -> None:
        shot = self.project.get_shot(shot_id)
        if not shot:
            return
        takes = self.project.list_takes(shot_id, include_deleted=True)
        inflight = [t for t in takes if t.status == STATUS_GENERATING]
        msg = f"Delete shot '{shot.name}'?"
        if takes:
            msg += f"\n\nIts {len(takes)} take(s) will also be removed from the project."
        if inflight:
            msg += (f"\n\n{len(inflight)} take(s) are still rendering and will be stopped "
                    "(their spend/GPU is halted).")
        if QMessageBox.question(
                self, "Delete shot", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        tab = self.shot_tabs.pop(shot_id, None)   # close its open editor tab, if any
        if tab is not None:
            idx = self.tabs.indexOf(tab)
            if idx >= 0:
                self.tabs.removeTab(idx)
            tab.deleteLater()
        # Neutralize this shot's in-flight work BEFORE removing it from the index: cancel
        # its queued takes (so they never fire the backend and orphan an .mp4) and stop any
        # mid-render take (so spend/GPU halts). Both read take state from the project, so
        # they must run before delete_shot drops the takes.
        cancelled = self.jobs.cancel_shot_takes(shot_id)
        stopped = sum(1 for t in inflight if self.jobs.request_stop(t.id))
        # Close any open take-viewer tabs for this shot's takes so none dangles on a take
        # that's about to be removed (and none is captured into ui_state as a dead descriptor).
        # Mirrors remove_cancelled_takes, which already does this before purge_takes (M10).
        self._close_take_tabs([t.id for t in takes])
        self.project.delete_shot(shot_id)
        self.reload()
        self._refresh_cancel_action()
        self.queue_tab.refresh()
        note = ""
        if cancelled or stopped:
            bits = []
            if cancelled:
                bits.append(f"cancelled {cancelled} queued")
            if stopped:
                bits.append(f"stopped {stopped} rendering")
            note = " (" + ", ".join(bits) + ")"
        self._log(f"deleted shot '{shot.name}'{note}")

    def _commit_open_shot_tabs(self) -> None:
        """Flush every open shot-tab editor into the project buffer so File > Save
        persists in-progress edits, not just shots saved via their own tab. Blank-named
        but worked-on tabs are auto-named 'Unnamed Shot N'; pristine untouched new tabs
        are skipped. Iterates the tab widget directly, since new tabs aren't in
        shot_tabs until first saved."""
        changed = False
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if not isinstance(tab, ShotTab) or tab.is_blank_new():
                continue
            sid = tab.commit()
            if sid:
                self.shot_tabs[sid] = tab
                self.tabs.setTabText(i, tab.title())
                changed = True
        if changed:
            self.reload()

    def _wire_shot_tab(self, tab: ShotTab) -> None:
        tab.saved.connect(lambda sid, t=tab: self._on_shot_saved(sid, t))
        tab.dirty_changed.connect(lambda t=tab: self._on_shot_dirty_changed(t))
        tab.generate_requested.connect(self.generate_shot)
        tab.export_requested.connect(self.export_takes)
        tab.open_take_requested.connect(self.open_take)
        tab.restart_requested.connect(self._restart_takes_by_ids)
        # binning a take from the tab's grid must keep the recovery banner honest
        tab.takes_changed.connect(self._sync_recovery_banner)

    def _on_shot_saved(self, shot_id: str, tab: ShotTab) -> None:
        # A blank tab just became a real shot (or an existing shot was re-saved): register
        # it, refresh the tab label, and rebuild the list so the card appears/updates.
        self.shot_tabs[shot_id] = tab
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, tab.title())
        self.reload()

    def generate_shot(self, shot_id: str) -> None:
        if not self.project.get_shot(shot_id):
            # Existence probe only (the real fetch happens after the save below).
            # generate_requested is a queued signal; the shot can be deleted between the
            # emit and this slot running (benign double-click / stale-tab race). Guard the
            # deref like open_shot/toggle_shot_star/delete_shot do, plus a log line since a
            # silently-dropped Generate is the confusing case (the siblings stay silent).
            self._log("generate ignored: shot no longer exists")
            return
        # Persist the project (flushing every open shot-tab editor via
        # _commit_open_shot_tabs) BEFORE validating, computing the cost estimate, showing the
        # gate, and freezing the snapshot — otherwise an uncommitted open tab would mutate this
        # Shot in place between the gate and _queue_take, so the gate could confirm one
        # model/cost while the take snapshot records another (rule #1 + rule #3, card H3). Also
        # handles untitled -> Save As prompt; aborting the save aborts the generation. Every
        # validation below then reads committed state, so the confirmed content is what renders.
        if not self.save_project():
            self._log("generation cancelled (project not saved)")
            return
        shot = self.project.get_shot(shot_id)
        if not shot:
            self._log("generate ignored: shot deleted during save")
            return
        model = library.get_model(shot.model_id)
        if not model:
            QMessageBox.warning(self, "Generate", f"Unknown model: {shot.model_id}")
            return
        aspect = (shot.crop or {}).get("aspect")
        if aspect and aspect not in library.aspect_ratios(shot.model_id):
            QMessageBox.warning(self, "Generate",
                                f"'{aspect}' isn't a valid aspect ratio for "
                                f"{model['display_name']}. Open the shot and pick one from "
                                "the Aspect list.")
            return
        if not shot.start_frame:
            start, _ = QFileDialog.getOpenFileName(
                self, "Pick a start keyframe", str(paths.ASSETS_DIR),
                "Images (*.png *.jpg *.jpeg *.webp)")
            if not start:
                return
            self.project.update_shot(shot.id, start_frame=str(self.project.import_asset(start)))
            # Persist the picked keyframe DIRECTLY (project.save(), not save_project()): the
            # project is titled by now (the save above succeeded), and save_project()'s
            # _commit_open_shot_tabs() would re-commit this shot's open tab — whose editor
            # still has no start frame — reverting the keyframe we just picked before the
            # snapshot freezes it.
            self.project.save()
            shot = self.project.get_shot(shot_id)
            if not shot:
                self._log("generate ignored: shot deleted while picking a keyframe")
                return
            # The import+save already changed the project on disk, so reflect the new start
            # keyframe on the shot card + title NOW — before the cost gate. Otherwise cancelling
            # the gate below leaves the UI showing the pre-import (no-start-frame) state while
            # disk holds the assigned keyframe (L11).
            self.reload()

        settings = {**model.get("default_params", {}), **shot.settings}
        est = library.estimate_cost(shot.model_id, settings)
        item = {"name": shot.name, "model_display": model["display_name"],
                "backend": model["backend"], "est_cost": est, "params": settings}
        if not confirm_launch(self, [item]):
            self._log("launch cancelled")
            return

        self._queue_take(shot, model, settings, est)
        self._refresh_shot(shot.id)
        self._refresh_cancel_action()
        self.queue_tab.refresh()   # a freshly-queued take emits no signal until it starts

    def _queue_take(self, shot, model, settings, est) -> str:
        """Build the immutable snapshot, create a PENDING take, and enqueue its runner.

        Shared by single-shot Generate and the overnight batch. A 'random' shot rolls a
        fresh concrete seed *here*, per call, so N takes of the same shot vary and each
        take's snapshot records the seed actually used. Caller handles confirm + save +
        refresh. Returns the new take id.
        """
        # Copy per take so each take's snapshot owns an independent settings dict (N takes
        # of one shot share a source dict otherwise) and reroll the random seed per take.
        settings = dict(settings)
        if settings.get("seed") == library.SEED_RANDOM:
            settings["seed"] = library.resolve_seed(library.SEED_RANDOM)
        # Deep-copy crop/settings so the frozen snapshot is detached from the live shot the
        # moment it's created (rule #3): an in-place mutation of shot.crop (or a nested
        # settings dict) can't reach back and corrupt the take's immutable record. Mirrors the
        # render-side detach in _shot_from_snapshot (card #53); small dicts, cheap.
        snapshot = {
            "model_id": shot.model_id, "backend": model["backend"],
            "replicate_model_id": model.get("replicate_model_id"),
            "workflow_template": model.get("workflow_template"),
            "start_frame": shot.start_frame, "end_frame": shot.end_frame,
            "prompt": shot.prompt, "negative_prompt": shot.negative_prompt,
            "settings": copy.deepcopy(settings),
            "canvas": [shot.canvas_w, shot.canvas_h], "crop": copy.deepcopy(shot.crop),
        }
        take = self.project.add_take(shot.id, status=STATUS_PENDING,
                                     seed=settings.get("seed"), cost_estimate=est,
                                     settings_snapshot=snapshot)
        # Render from the take's frozen snapshot, not the live Shot. The serialized local
        # worker (or a later batch round) can dequeue long after the shot was edited+saved;
        # feeding _make_runner a snapshot-derived synth Shot keeps the render matching the
        # take's immutable settings_snapshot (rule #3). Same helper the restart path uses.
        synth = self._shot_from_snapshot(shot.id, snapshot)
        self.jobs.enqueue(take.id, model["backend"],
                          self._make_runner(model, synth, settings, take.id))
        self._log(f"queued {take.id[:8]} ({shot.name})")
        return take.id

    # ---- overnight batch -----------------------------------------------
    def start_batch(self) -> None:
        """Queue every eligible shot x N takes after one up-front cost-confirm.

        Honors the cost gate with a single confirmation for the whole night (the gate
        already takes a list). Saves once so no take references an unsaved shot. Records a
        BatchRun so _on_status_changed can detect drain and run the chosen power action.
        """
        if self._batch is not None:
            QMessageBox.information(self, "Generate batch",
                                    "A batch is already running. Wait for it to finish "
                                    "(or Cancel pending) before starting another.")
            return
        dlg = BatchDialog(self, view_count=len(self.cards))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        scope, n, power = dlg.scope(), dlg.takes_per_shot(), dlg.power_action()
        view_ids = list(self.cards)   # snapshot the view before save() rebuilds self.cards

        # Persist the project (flushing every open shot-tab editor via _commit_open_shot_tabs)
        # BEFORE planning, showing the gate, and freezing snapshots — otherwise an uncommitted
        # open tab would mutate a Shot in place between the gate and _queue_take, so the gate
        # could confirm one plan/cost while the take snapshots record another (rule #1 + rule
        # #3, card H3). Also handles untitled -> Save As; aborting the save aborts the batch.
        if not self.save_project():
            self._log("batch cancelled (project not saved)")
            return

        if scope == SCOPE_VIEW:
            shots = [s for s in (self.project.get_shot(sid) for sid in view_ids) if s]
        else:
            shots = self.project.list_shots()
        plan = batch.plan_batch(
            shots, takes_per_shot=n,
            model_of=lambda s: library.get_model(s.model_id),
            aspects_of=library.aspect_ratios,
            est_of=library.estimate_cost)

        if not plan.eligible:
            QMessageBox.warning(self, "Generate batch",
                                "No eligible shots to generate.\n\n"
                                + _skipped_text(plan.skipped))
            return
        if plan.skipped:
            if QMessageBox.question(
                    self, "Generate batch",
                    f"{len(plan.eligible)} shot(s) will generate "
                    f"({plan.take_count} take(s) total).\n"
                    f"{len(plan.skipped)} shot(s) will be skipped:\n\n"
                    + _skipped_text(plan.skipped) + "\n\nContinue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return

        if not confirm_launch(self, plan.items):
            self._log("batch cancelled")
            return

        take_ids: set[str] = set()
        for shot, model, settings, est in batch.queue_order(plan.eligible, plan.takes_per_shot):
            take_ids.add(self._queue_take(shot, model, settings, est))
        self._batch = batch.BatchRun(
            take_ids=take_ids, power_action=power,
            started=datetime.now().isoformat(timespec="seconds"))
        self._log(f"batch started: {len(take_ids)} take(s), when-done={power}")
        self.reload()
        self._refresh_cancel_action()
        self._refresh_pause_action()
        self.queue_tab.refresh()

    def _finalize_batch(self) -> None:
        """Every take in the batch has reached a terminal status: write the report, then run
        the chosen power action. Called from _on_status_changed once BatchRun.complete."""
        b, self._batch = self._batch, None
        self._refresh_pause_action()
        if b is None:
            return
        rows = []
        for tid in b.take_ids:
            t = self.project.get_take(tid)
            if t is None:
                continue
            s = self.project.get_shot(t.shot_id)
            name = s.name if s else tid[:8]
            rows.append({"name": name, "status": t.status, "cost_actual": t.cost_actual})
        report = batch.build_batch_report(
            rows, started=b.started,
            finished=datetime.now().isoformat(timespec="seconds"),
            power_action=b.power_action)
        try:
            paths.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # %f: unique per call
            report_path = paths.EXPORTS_DIR / f"overnight_{stamp}.txt"
            report_path.write_text(report, encoding="utf-8")
            self._log(f"batch finished - report: {report_path}")
        except Exception as e:  # noqa: BLE001 - a failed report must not abort the power action
            self._log(f"batch finished - report write failed: {e}")
        if b.power_action != batch.POWER_NONE:
            self._perform_power_action(b.power_action)

    def _perform_power_action(self, action: str) -> None:
        """Stop ComfyUI (always) and optionally sleep the PC, on a daemon thread so the GUI
        isn't blocked (stop_server can take ~10s). Best-effort: every step is guarded.

        Runs off the GUI thread, so it must NOT touch Qt (no self._log) - failures go to
        stdout (the launch log) instead. The GUI-thread announcement is logged below."""
        def work():
            try:
                comfy_client.stop_server()
            except Exception as e:  # noqa: BLE001 - server may already be down
                print(f"batch: stop_server failed: {e}")
            if action == batch.POWER_SLEEP:
                cmd = batch.sleep_command()
                if cmd:
                    try:
                        subprocess.Popen(cmd)
                    except Exception as e:  # noqa: BLE001
                        print(f"batch: sleep command failed: {e}")
        self._log(f"batch power action: {action}")
        threading.Thread(target=work, daemon=True).start()

    def cancel_pending(self) -> None:
        # Abort an active batch first (before the cancellations fire status_changed), so a
        # deliberate Cancel doesn't drain the batch into its when-done power action - the
        # user is clearly present and wouldn't want the PC put to sleep.
        if self._batch is not None:
            self._batch = None
            self._log("batch aborted (cancel pending) - no power action will run")
        self._stop_paused_local = False   # cancel_pending clears jobs._local_paused too
        n = self.jobs.cancel_pending()
        self._log(f"cancelled {n} pending generation(s)" if n
                  else "no pending generations to cancel")
        self._refresh_cancel_action()
        self._refresh_pause_action()

    def _refresh_cancel_action(self) -> None:
        self.cancel_act.setEnabled(self.jobs.pending_count() > 0)
        self._refresh_restart_action()

    def _refresh_restart_action(self) -> None:
        self.restart_act.setEnabled(self._interrupted_take_count() > 0)

    def _interrupted_take_count(self) -> int:
        return sum(1 for t in self.project.list_takes(include_deleted=False)
                   if t.interrupted and t.status in (STATUS_CANCELLED, STATUS_FAILED))

    # ---- remove cancelled takes (Edit menu) ----------------------------
    def _cancelled_take_count(self) -> int:
        # include_deleted: a binned cancelled take is still cancelled and should be purgeable.
        return sum(1 for t in self.project.list_takes(include_deleted=True)
                   if t.status == STATUS_CANCELLED)

    def _refresh_purge_cancelled_action(self) -> None:
        self.purge_cancelled_act.setEnabled(self._cancelled_take_count() > 0)

    def _close_take_tabs(self, take_ids) -> None:
        """Close any open take-viewer tabs for the given takes, so a tab can't dangle on a take
        that's about to be removed. Mirrors _on_tab_close's disposal (stop playback, drop tab)."""
        for tid in take_ids:
            tab = self.take_tabs.pop(tid, None)
            if tab is None:
                continue
            idx = self.tabs.indexOf(tab)
            if idx >= 0:
                self.tabs.removeTab(idx)
            tab.close_player()
            tab.deleteLater()

    def remove_cancelled_takes(self) -> None:
        """Edit menu: permanently remove every cancelled take in the project, across all shots
        (ignores the view filters, like Cancel pending / Restart interrupted). Drops their records
        and best-effort unlinks any managed media (a stopped-mid-render or binned take may carry
        some; external refs are left in place). Irreversible, so confirm first - defaults to No."""
        cancelled = [t for t in self.project.list_takes(include_deleted=True)
                     if t.status == STATUS_CANCELLED]
        if not cancelled:
            QMessageBox.information(self, "Remove cancelled takes",
                                    "There are no cancelled takes to remove.")
            return
        n = len(cancelled)
        if QMessageBox.question(
                self, "Remove cancelled takes",
                f"Permanently remove {n} cancelled take{'' if n == 1 else 's'} from this "
                f"project, across all shots?\n\nThis can't be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        ids = [t.id for t in cancelled]
        self._close_take_tabs(ids)           # a purged take must not leave a dangling viewer tab
        removed = self.project.purge_takes(ids)
        self._log(f"removed {removed} cancelled take(s)")
        self.reload()             # rebuilds cards + refreshes the purge/restart action states
        self._sync_recovery_banner()   # the purge may have taken the banner's takes with it
        self.queue_tab.refresh()  # drop the purged takes from the Queue tab

    # ---- one-time crash-recovery banner --------------------------------
    def _refresh_recovery_banner(self) -> None:
        """Show the load-time banner if a prior session left interrupted takes, else keep it
        hidden. Called after orphan recovery settles (and once at startup for takes that were
        already interrupted on load). Not driven from reload(), so a Dismiss stays dismissed."""
        text = recovery_banner_text(self._interrupted_take_count())
        if text:
            self.recovery_banner.show_message(text)
        else:
            self.recovery_banner.hide()

    def _sync_recovery_banner(self) -> None:
        """Keep an already-shown banner honest after take mutations (bin / purge / restart):
        retire it when no interrupted takes remain, refresh its count otherwise. A hidden
        banner stays hidden - unlike _refresh_recovery_banner this never re-arms a dismissed
        one, so it's safe to call from any take-churn path."""
        if self.recovery_banner.isHidden():
            return
        self._refresh_recovery_banner()

    def _restart_from_banner(self) -> None:
        """The banner's action: run the normal restart (its own cost gate). The banner is not
        pre-hidden - _restart_takes retires it only once the interrupted takes are actually
        consumed, so cancelling the cost gate leaves the notice up to try again."""
        self.restart_cancelled_takes()

    # ---- restart interrupted takes -------------------------------------
    def restart_cancelled_takes(self) -> None:
        """Re-run every INTERRUPTED take in the project (ignoring the view filters, like Cancel
        pending). These are takes a crash / ComfyUI-or-app death cut short - cancelled before they
        ran, or failed because their in-flight render was lost to the restart. Ones the user
        deliberately cancelled, and genuine render failures, are left alone. Exact-restartable
        takes replay in place from their snapshot; the rest are marked failed with a reason."""
        interrupted = [t for t in self.project.list_takes(include_deleted=False)
                       if t.interrupted and t.status in (STATUS_CANCELLED, STATUS_FAILED)]
        self._restart_takes(interrupted)

    def _restart_takes_by_ids(self, ids: list) -> None:
        """Restart just the given takes (the takes-grid context-menu entry). Restarts any cancelled
        take (an explicit user override) plus a crash-interrupted FAILED take (its in-flight render
        was lost to an app/ComfyUI death); a deliberately-failed (non-interrupted) take is ignored,
        mirroring the menu gate in takes_view._build_context_menu."""
        takes = [t for t in (self.project.get_take(i) for i in ids)
                 if t and (t.status == STATUS_CANCELLED
                           or (t.status == STATUS_FAILED and t.interrupted))]
        self._restart_takes(takes)

    def _restart_takes(self, takes: list) -> None:
        if not takes:
            QMessageBox.information(self, "Restart", "No interrupted takes to restart.")
            return
        plan = restart.plan_restart(
            takes, model_of_id=library.get_model, est_of=library.estimate_cost,
            path_exists=lambda p: bool(p) and Path(p).exists(), name_of=self._take_label)
        detail = "\n".join(f"  • {self._take_label(t)}: {r}" for t, r in plan.unrestartable)
        if plan.restartable:
            if not confirm_launch(self, plan.items):
                self._log("restart cancelled")
                return
            # Persist first so a restarted take never references an unsaved shot (untitled ->
            # Save As prompt). Aborting the save aborts the whole restart - fail nothing.
            if not self.save_project():
                self._log("restart cancelled (project not saved)")
                return
            for take in plan.restartable:
                self._restart_in_place(take)
        elif plan.unrestartable:
            # Nothing to re-fire (no spend, so no cost gate), but there are takes we can't replay
            # exactly. Confirm before flipping CANCELLED->FAILED so the click stays backable-out.
            if QMessageBox.question(
                    self, "Restart",
                    f"None of the {len(plan.unrestartable)} interrupted take(s) can be restarted "
                    f"exactly:\n\n{detail}\n\nMark them failed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        for take, reason in plan.unrestartable:
            self.project.update_take(take.id, status=STATUS_FAILED,
                                     error=f"cannot restart: {reason}", interrupted=False)
            self._refresh_shot_for_take(take.id)
        self._log(f"restart: {len(plan.restartable)} re-queued, "
                  f"{len(plan.unrestartable)} marked failed")
        if plan.restartable and plan.unrestartable:
            QMessageBox.information(
                self, "Restart",
                f"Re-queued {len(plan.restartable)} take(s).\n\n"
                f"{len(plan.unrestartable)} take(s) couldn't be restarted exactly and were "
                f"marked failed:\n\n{detail}")
        self.reload()
        self._refresh_cancel_action()
        self._sync_recovery_banner()   # interrupted takes consumed -> retire/re-count the notice
        self.queue_tab.refresh()

    def _restart_in_place(self, take) -> None:
        """Flip one cancelled take back to PENDING and re-enqueue a runner built straight from
        its immutable snapshot (same seed, same framing). The snapshot is never mutated - only
        the take's status + output/timing fields reset."""
        snap = take.settings_snapshot or {}
        model = library.get_model(snap.get("model_id"))
        settings = snap.get("settings") or {}
        synth = self._shot_from_snapshot(take.shot_id, snap)
        self.project.update_take(
            take.id, status=STATUS_PENDING, error=None, started=None, completed=None,
            video_path=None, thumbnail=None, preview_gif=None, cost_actual=None,
            interrupted=False)   # re-queued fresh; no longer an interruption
        self.jobs.restart_take(take.id, model["backend"],
                               self._make_runner(model, synth, settings, take.id))
        self._log(f"restarting {take.id[:8]} ({self._take_label(take)})")

    def _shot_from_snapshot(self, shot_id: str, snap: dict) -> Shot:
        """A throwaway Shot carrying the snapshot's frozen framing, fed to _make_runner /
        framing.render_keyposes so the take reproduces exactly (not the shot's
        possibly-since-edited state). Only the fields those readers touch are populated.
        crop/settings are deep-copied so a synth-Shot reader can never mutate the take's
        immutable settings_snapshot through the shared dict (rule #3)."""
        canvas = snap.get("canvas") or [None, None]   # always [w, h] when present (_queue_take)
        existing = self.project.get_shot(shot_id)
        return Shot(
            id=shot_id, name=(existing.name if existing else "(restart)"),
            start_frame=snap.get("start_frame"), end_frame=snap.get("end_frame"),
            canvas_w=canvas[0], canvas_h=canvas[1],
            crop=copy.deepcopy(snap.get("crop") or {}), prompt=snap.get("prompt", ""),
            negative_prompt=snap.get("negative_prompt", ""),
            model_id=snap.get("model_id", ""), settings=copy.deepcopy(snap.get("settings") or {}))

    def _take_label(self, take) -> str:
        s = self.project.get_shot(take.shot_id)
        return s.name if s else take.id[:8]

    def _refresh_pause_action(self) -> None:
        """Pause/Resume is only meaningful while a batch is in flight. Reset its label to
        'Pause batch' whenever no batch is active (so a finished/aborted batch doesn't leave
        a stale 'Resume batch')."""
        b = self._batch
        self.pause_act.setEnabled(b is not None)
        self.pause_act.setText("Resume batch" if (b is not None and b.paused) else "Pause batch")

    def toggle_pause_batch(self) -> None:
        b = self._batch
        if b is None:
            return
        self._resume_batch() if b.paused else self._pause_batch()

    def _pause_batch(self) -> None:
        """Ask whether to let the current take finish or halt+re-queue it, then hold the rest
        of the batch's local queue. Held takes stay PENDING (the batch can't finalize while
        paused), so its when-done power action won't fire until the user resumes and it drains."""
        b = self._batch
        if b is None or b.paused:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Pause batch")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Pause the batch — what about the take currently rendering?")
        box.setInformativeText(
            "Pause after current: let it finish, then hold the rest.\n"
            "Halt current & re-add: stop it now and put it back on the queue to re-run on resume.")
        after_btn = box.addButton("Pause after current", QMessageBox.ButtonRole.AcceptRole)
        halt_btn = box.addButton("Halt current && re-add", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(after_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (after_btn, halt_btn):
            return
        requeue = clicked is halt_btn
        held = self.jobs.pause_local(requeue_current=requeue)
        b.paused = True
        b.held = held
        tail = "current halted & re-queued" if requeue else "current take will finish"
        self._log(f"batch paused — holding {len(held)} local take(s); {tail}. Resume batch to continue.")
        self._refresh_pause_action()
        self.reload()
        self._refresh_cancel_action()
        self.queue_tab.refresh()

    def _resume_batch(self) -> None:
        b = self._batch
        if b is None or not b.paused:
            return
        n = self.jobs.resume_local(b.held)
        b.paused = False
        b.held = []
        self._log(f"batch resumed — re-enqueued {n} local take(s)")
        self._refresh_pause_action()
        self.reload()
        self._refresh_cancel_action()
        self.queue_tab.refresh()

    def _local_work_in_flight(self) -> bool:
        """Whether any local (ComfyUI) take is still GENERATING or queued PENDING. Used to
        decide whether a manual ComfyUI stop has anything to pause, and (in _on_status_changed)
        when a non-batch stop-pause has drained so the transient pause can be lifted."""
        return any(
            t.status in (STATUS_GENERATING, STATUS_PENDING)
            and (t.settings_snapshot or {}).get("backend") == "comfyui"
            for t in self.project.list_takes(include_deleted=False))

    def _pause_local_on_stop_intent(self) -> None:
        """A deliberate ComfyUI stop (Stop working / Shut down) was requested from the ComfyUI
        Status tab. Pause the local queue so the resulting render failure is treated as the
        intended stop, not a crash to auto-restart (rule #12 should_abort -> is_local_paused).

        Batch case (rule #16): pause it, holding the queued local takes PENDING for Resume batch.

        Non-batch case (card #42): there's no Resume affordance, so cancel the queued local
        takes and mark a transient pause that _on_status_changed auto-clears once the in-flight
        take drains. Without this, a manual Shut down outside a batch is fought by crash-recovery
        (server down read as a crash -> relaunch + retry). In both cases the GENERATING take is
        left to fail; those buttons already warn the running render is lost."""
        b = self._batch
        if b is not None:
            if b.paused:
                return
            held = self.jobs.pause_local()
            b.paused = True
            b.held = held
            self._log(f"batch paused (ComfyUI stopped by user) — holding {len(held)} local "
                      "take(s). Resume batch to continue.")
            self._refresh_pause_action()
            self.reload()
            self._refresh_cancel_action()
            self.queue_tab.refresh()
            return

        if self._stop_paused_local or not self._local_work_in_flight():
            return
        rendering = any(
            t.status == STATUS_GENERATING
            and (t.settings_snapshot or {}).get("backend") == "comfyui"
            for t in self.project.list_takes(include_deleted=False))
        held = self.jobs.pause_local()   # sets the pause flag, clears the local pool
        self._stop_paused_local = True
        for tid in held:                 # no Resume UI here: cancel the queued local takes.
            self.jobs.cancel_take(tid)   # a take a worker already dequeued bails on the _cancelled set
        self._log(f"local queue paused (ComfyUI stopped by user) — "
                  f"{'the current render will stop; ' if rendering else ''}"
                  f"cancelled {len(held)} queued local take(s).")
        if not self._local_work_in_flight():
            # The stop hit a purely-queued local set (nothing GENERATING): there's no in-flight
            # render whose failure needs covering, and no terminal status_changed will arrive to
            # trigger the auto-clear in _on_status_changed, so lift the transient pause now.
            self._stop_paused_local = False
            self.jobs.clear_local_pause()
        self.reload()
        self._refresh_cancel_action()
        self.queue_tab.refresh()

    def _on_queue_abandoned(self, reason: str) -> None:
        """The local (ComfyUI) queue was paused after a take crashed repeatedly. Log it,
        refresh the Cancel action (its pending takes are now cancelled), and warn the user.

        If a batch is running, drop its power action: a broken GPU means the user must
        intervene, so we must not sleep the PC (and would bury this warning). The batch
        still finalizes + writes its report when the remaining takes drain. This fires
        before the crashing take's own terminal signal (abandon_local emits queue_abandoned
        before crash_recovery re-raises), so the neutralized action is in place in time."""
        if self._batch is not None:
            self._batch.power_action = batch.POWER_NONE
        self._log(f"⚠ local queue paused: {reason}")
        self._refresh_cancel_action()
        self.queue_tab.refresh()
        QMessageBox.warning(self, "ComfyUI queue paused", reason)

    def _make_runner(self, model, shot, settings, take_id):
        # Keyposes are framed from the shot's assets at generation time (on the worker
        # thread) into a temp dir, rather than baked at save time. See framing.render_keyposes.
        out_path = self.project.takes_dir / f"{take_id}.mp4"
        if model["backend"] == "replicate":
            rid = model["replicate_model_id"]
            data_uri = model.get("requires_data_uri", False)
            extra = {k: v for k, v in settings.items() if k not in _EXPLICIT_SETTINGS}

            def runner(progress):
                def on_submit(pid):
                    # Record the prediction id NOW so a delete/stop mid-render can cancel it.
                    self.project.update_take(take_id, backend_job_id=pid)
                    # Close the create-POST window: if a stop was requested before the id
                    # existed, request_stop's cancel was skipped - self-cancel here (best-effort)
                    # so a prediction that would otherwise succeed and orphan its .mp4 halts spend.
                    if self.jobs.is_stop_requested(take_id):
                        progress("stop requested during submit - cancelling prediction")
                        try:
                            replicate_client.cancel_prediction(pid)
                        except Exception:  # noqa: BLE001 - best-effort, mirrors request_stop
                            pass

                start_kp, end_kp = framing.render_keyposes(shot, tempfile.mkdtemp(prefix="animgen_kp_"))
                return replicate_client.generate(
                    rid, start=start_kp, end=end_kp, prompt=shot.prompt,
                    negative=shot.negative_prompt, duration=settings.get("duration"),
                    resolution=settings.get("resolution"), seed=settings.get("seed"),
                    extra=extra, data_uri=data_uri, out_path=out_path, progress_cb=progress,
                    on_submit=on_submit)
            return runner

        tpl = paths.resolve_template(model.get("workflow_template") or "")
        roles = model.get("comfy_nodes")
        # Drive the workflow's output size from the shot's canvas (aspect -> w x h at the
        # local pixel budget) so a wide/tall aspect produces a wide/tall local video.
        size_sets = {}
        size_node = (roles or {}).get("size_node")
        if size_node and shot.canvas_w and shot.canvas_h:
            size_sets = {f"{size_node}.width": shot.canvas_w, f"{size_node}.height": shot.canvas_h}
        length = shot.settings.get("length")   # Output-tab duration -> Wan frame count (4n+1)
        if size_node and length:
            size_sets[f"{size_node}.length"] = int(length)

        def on_submit(pid):   # persist the comfy prompt id so an app restart mid-render
            self.project.update_take(take_id, backend_job_id=pid)  # can reconcile this take

        def runner(progress):
            # Cold-start ComfyUI if it isn't running, before the render. The local pool is
            # serialized, so the first queued take starts the server and the rest find it up.
            # Done here (not via crash recovery) so a not-yet-started server is an honest
            # "starting ComfyUI" step rather than a failure misread as a crash; a genuine
            # start failure raises out of the runner and fails just this take, cleanly.
            comfy_client.ensure_server(progress_cb=progress)

            # One render attempt; wrapped below in crash recovery. A ComfyUI process crash
            # (GPU watchdog/TDR) restarts the server and retries this take in place, while the
            # rest of the local queue waits behind it on the serialized worker. After 3 crashes
            # the whole local queue is abandoned (see backends/crash_recovery.py).
            def attempt():
                start_kp, end_kp = framing.render_keyposes(
                    shot, tempfile.mkdtemp(prefix="animgen_kp_"))
                return comfy_client.generate(
                    tpl, out_path, start=start_kp, end=end_kp,
                    prompt=shot.prompt or None, negative=shot.negative_prompt or None,
                    seed=settings.get("seed"), node_roles=roles, sets=size_sets,
                    progress_cb=progress, on_submit=on_submit,
                    text_encoder_cpu=True)  # keep the ~6GB encoder off the 12GB card (see comfy_client)

            return crash_recovery.run_with_crash_recovery(
                render=attempt,
                server_running=lambda: comfy_client.server_status()["running"],
                restart_server=lambda: comfy_client.restart_server(progress_cb=progress),
                note=progress,
                on_abandon=self.jobs.abandon_local,
                should_abort=self.jobs.is_local_paused,
                clock=time.time)
        return runner

    def _make_monitor_runner(self, take_id: str, prompt_id: str):
        """A runner that re-attaches to an in-flight ComfyUI prompt (orphan recovery)."""
        out_path = self.project.takes_dir / f"{take_id}.mp4"

        def runner(progress):
            return comfy_client.monitor(prompt_id, out_path, progress_cb=progress)
        return runner

    # ---- orphan recovery -----------------------------------------------
    def _recover_orphans(self) -> None:
        """Reconcile takes a prior session left mid-render on the local backend.

        On load there are no live workers, so any take still at generating/pending is orphaned.
        Local (comfyui) takes reconcile against the surviving ComfyUI server (/history + /queue);
        hosted (replicate) takes reconcile against each prediction on Replicate (get_prediction -
        an idempotent GET, no spend). Both fetch off-thread and apply on the GUI thread."""
        proj = self.project                       # guard against a project switch mid-fetch
        if recovery.comfy_orphans(proj):
            self._reconciler = _OrphanReconciler()    # kept on self so it isn't GC'd
            self._reconciler.ready.connect(lambda h, q: self._apply_recovery(proj, h, q))
            self._reconciler.start()
        hosted = recovery.replicate_orphans(proj)
        if hosted:
            self._replicate_reconciler = _ReplicateReconciler(hosted)   # kept on self (not GC'd)
            self._replicate_reconciler.ready.connect(
                lambda statuses: self._apply_replicate_recovery(proj, statuses))
            self._replicate_reconciler.start()

    def _apply_recovery(self, proj, history, queue) -> None:
        if proj is not self.project:              # user switched projects before the fetch returned
            return
        orphans = recovery.comfy_orphans(proj)
        if not orphans:
            return
        if history is None:                       # ComfyUI unreachable - nothing can be verified
            counts: Counter = Counter()           # and no worker is live, so clear every orphan
            for p in recovery.plan_offline_recovery(orphans):
                if p.action == recovery.CANCEL:
                    proj.update_take(p.take_id, status=STATUS_CANCELLED, error=p.reason,
                                     interrupted=True)   # crash/app-death, not a user cancel
                else:                             # FAIL - generating take lost to the restart
                    proj.update_take(p.take_id, status=STATUS_FAILED, error=p.reason,
                                     interrupted=True)   # lost render, restartable from snapshot
                counts[p.action] += 1
                self._refresh_shot(p.shot_id)
            parts = []
            if counts[recovery.FAIL]:
                parts.append(f"failed {counts[recovery.FAIL]} unrecoverable (ComfyUI unreachable)")
            if counts[recovery.CANCEL]:
                parts.append(f"cancelled {counts[recovery.CANCEL]} un-submitted")
            if parts:
                self._log("orphan recovery: " + "; ".join(parts))
            self._refresh_cancel_action()
            self._refresh_recovery_banner()   # recovery just produced interrupted takes
            return
        self._execute_plans(recovery.plan_comfy_recovery(
            orphans, history, queue, recovery.ambiguous_seeds(proj)))

    def _execute_plans(self, plans) -> None:
        counts: Counter = Counter()
        for p in plans:
            if p.action == recovery.RECLAIM:
                try:
                    # A binned orphan (deleted mid-flight, H2) is still reclaimed - the render
                    # is already paid for - but its media belongs in the bin with the rest of a
                    # deleted take's files, not the live takes/ dir. It stays deleted throughout
                    # (recovery never clears `deleted`), so restore-from-bin finds it in place.
                    take = self.project.get_take(p.take_id)
                    dest_dir = (self.project.bin_dir / p.take_id if take and take.deleted
                                else self.project.takes_dir)
                    dst = dest_dir / f"{p.take_id}.mp4"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p.output_path, dst)
                    # Stamp fps/frame_count off the reclaimed video too - this path sets
                    # video_path outside GenerationJob, so it'd otherwise leave them None.
                    fps, frame_count = extract.probe_media_fields(str(dst))
                    self.project.update_take(
                        p.take_id, status=STATUS_DONE, video_path=str(dst),
                        fps=fps, frame_count=frame_count, backend_job_id=p.prompt_id,
                        completed=datetime.now().isoformat(timespec="seconds"))
                except Exception as e:  # noqa: BLE001 - a failed copy must not abort the rest
                    self.project.update_take(p.take_id, status=STATUS_FAILED,
                                             error=f"recovery copy failed: {e}")
                    self._log(f"orphan {p.take_id[:8]}: reclaim FAILED: {e}")
                    counts["fail"] += 1
                    self._refresh_shot(p.shot_id)
                    continue
            elif p.action == recovery.REATTACH:
                self.project.update_take(p.take_id, status=STATUS_GENERATING,
                                         backend_job_id=p.prompt_id)
                self.jobs.enqueue(p.take_id, "comfyui",
                                  self._make_monitor_runner(p.take_id, p.prompt_id))
            elif p.action == recovery.FAIL:
                self.project.update_take(p.take_id, status=STATUS_FAILED, error=p.reason,
                                         interrupted=True)   # render lost to app restart, restartable
            elif p.action == recovery.CANCEL:
                self.project.update_take(p.take_id, status=STATUS_CANCELLED, error=p.reason,
                                         interrupted=True)   # crash/app-death, not a user cancel
            counts[p.action] += 1
            self._log(f"orphan {p.take_id[:8]}: {p.reason}")
            self._refresh_shot(p.shot_id)
        if counts:
            self._log("orphan recovery: " + ", ".join(f"{n} {a}" for a, n in counts.items()))
        self._refresh_cancel_action()
        self._refresh_recovery_banner()   # recovery may have produced interrupted takes

    def _apply_replicate_recovery(self, proj, statuses) -> None:
        if proj is not self.project:              # user switched projects before the poll returned
            return
        orphans = recovery.replicate_orphans(proj)
        if not orphans:
            return
        self._execute_replicate_plans(recovery.plan_replicate_recovery(orphans, statuses or {}))

    def _execute_replicate_plans(self, plans) -> None:
        """Apply hosted reconciliation plans. RECLAIM downloads the already-paid-for output
        (idempotent GET, no spend) and stamps it DONE; FAIL/CANCEL clear the frozen take.
        Mirrors _execute_plans (local) incl. fps/frame_count stamping and the bin destination
        for a take deleted mid-flight (H2)."""
        counts: Counter = Counter()
        for p in plans:
            if p.action == recovery.RECLAIM:
                try:
                    take = self.project.get_take(p.take_id)
                    dest_dir = (self.project.bin_dir / p.take_id if take and take.deleted
                                else self.project.takes_dir)
                    dst = dest_dir / f"{p.take_id}.mp4"
                    replicate_client.download_output(p.prediction, dst)
                    fps, frame_count = extract.probe_media_fields(str(dst))
                    self.project.update_take(
                        p.take_id, status=STATUS_DONE, video_path=str(dst),
                        fps=fps, frame_count=frame_count, backend_job_id=p.prompt_id,
                        completed=datetime.now().isoformat(timespec="seconds"))
                except Exception as e:  # noqa: BLE001 - a failed download must not abort the rest
                    self.project.update_take(p.take_id, status=STATUS_FAILED,
                                             error=f"hosted recovery download failed: {e}",
                                             interrupted=True)   # unclaimed render, restartable
                    self._log(f"orphan {p.take_id[:8]}: hosted reclaim FAILED: {e}")
                    counts["fail"] += 1
                    self._refresh_shot(p.shot_id)
                    continue
            elif p.action == recovery.FAIL:
                # A genuine `failed` prediction is a real render error (not interrupted); every
                # other FAIL here (still-running / unverifiable / no job id) is crash/lost and
                # restartable. plan_replicate_recovery only emits CANCEL for a server-side cancel.
                genuine = (p.prediction is not None
                           and p.prediction.get("status") == "failed")
                self.project.update_take(p.take_id, status=STATUS_FAILED, error=p.reason,
                                         interrupted=not genuine)
            elif p.action == recovery.CANCEL:
                self.project.update_take(p.take_id, status=STATUS_CANCELLED, error=p.reason,
                                         interrupted=True)   # stopped on the server, restartable
            counts[p.action] += 1
            self._log(f"hosted orphan {p.take_id[:8]}: {p.reason}")
            self._refresh_shot(p.shot_id)
        if counts:
            self._log("hosted orphan recovery: "
                      + ", ".join(f"{n} {a}" for a, n in counts.items()))
        self._refresh_cancel_action()
        self._refresh_recovery_banner()   # hosted recovery may have produced interrupted takes

    # ---- export ---------------------------------------------------------
    def export_takes(self, take_ids: list, label: Optional[str] = None) -> None:
        if not take_ids:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        if label is None:
            shot_ids = {t.shot_id for t in (self.project.get_take(i) for i in take_ids) if t}
            if len(shot_ids) == 1:
                shot = self.project.get_shot(next(iter(shot_ids)))
                label = shot.name if shot else "selection"
            else:
                label = "selection"
        try:
            res = export.export_takes(self.project, take_ids, label=label)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Export", f"Export failed:\n{e}")
            return
        self._report_export(res)

    def export_current_view(self) -> None:
        ids = []
        for card in self.cards.values():
            ids.extend(card._row_export_ids())
        self.export_takes(ids, label="view")

    def export_starred_takes(self) -> None:
        """Export every starred take across the shots currently shown (obeys the view
        filters, like Export view). Iterates the displayed cards so the model/starred-shot
        filters apply, and delegates to the shared export path."""
        ids = []
        for shot_id in self.cards:
            ids.extend(t.id for t in self.project.list_takes(shot_id, starred_only=True))
        if not ids:
            QMessageBox.information(self, "Export starred takes",
                                    "No starred takes in the current view.")
            return
        self.export_takes(ids, label="starred")

    def _report_export(self, res: dict) -> None:
        parent = res.get("parent")
        if not parent:
            QMessageBox.information(self, "Export", "No takes had a video file to export.")
            return
        n_folders = len(res["exported"])
        total_frames = sum(n for _, n in res["exported"])
        skipped = len(res.get("skipped", []))
        msg = f"Exported {n_folders} animation(s), {total_frames} frames total\n\n{parent}"
        if skipped:
            msg += f"\n\n({skipped} skipped - no video file, or take/shot no longer exists)"
        self._log(f"exported {n_folders} animation(s) -> {parent}")
        box = QMessageBox(self)
        box.setWindowTitle("Export complete")
        box.setText(msg)
        open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_btn:
            import os
            try:
                os.startfile(str(parent))  # type: ignore[attr-defined]  # Windows
            except Exception:  # noqa: BLE001
                pass

    def _on_tab_changed(self, index: int) -> None:
        # The ComfyUI monitor only polls while its tab is visible: a down localhost port
        # costs a full socket timeout per probe on this machine, so there's no point
        # paying that in the background. This keeps the poll scoped to the live tab.
        if self.tabs.widget(index) is self.comfy_tab:
            self.comfy_tab.start_monitoring()
        else:
            self.comfy_tab.stop_monitoring()

    def _show_fixed_tab(self, widget: QWidget, title: str) -> None:
        """Reopen (if closed) and focus one of the fixed tabs from the View menu."""
        if self.tabs.indexOf(widget) < 0:
            self.tabs.addTab(widget, title)
        self.tabs.setCurrentWidget(widget)

    # ---- tab-layout persistence (stored per project in the .animproj) ----
    def _capture_tab_state(self) -> None:
        """Snapshot the live tab bar into project.ui_state so a save records the layout."""
        self.project.ui_state = self._compute_tab_state()

    def _compute_tab_state(self) -> dict:
        """Build the {tabs, active} descriptor for the live tab bar (no side effect).
        Window metadata, not authoring data - written on save, never sets dirty. Pristine
        unsaved blank shot tabs (no id yet, not in shot_tabs) are skipped; commit them
        first via Save. `active` is the index into the captured descriptor list (not a raw
        tab position) so it survives a later tab being skipped on restore. When the active
        tab is itself a skipped blank shot tab (it maps to no descriptor), `active` is
        re-pointed at the previously-recorded active descriptor rather than left None - else
        focusing such a tab at save/close would silently wipe the remembered active (#65)."""
        fixed_titles = {w: title for w, title in self._fixed_tabs}
        shot_id_by_tab = {t: sid for sid, t in self.shot_tabs.items()}
        active_widget = self.tabs.currentWidget()
        entries: list[dict] = []
        active: Optional[int] = None
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            entry: Optional[dict] = None
            if w in fixed_titles:
                entry = {"kind": "fixed", "key": fixed_titles[w]}
            elif isinstance(w, ShotTab):
                sid = shot_id_by_tab.get(w)
                if sid:
                    entry = {"kind": "shot", "id": sid}
            elif isinstance(w, TakePlayerTab):
                entry = {"kind": "take", "id": w.take_id}
            if entry is None:
                continue
            if w is active_widget:
                active = len(entries)
            entries.append(entry)
        if active is None and active_widget is not None:
            active = self._prior_active_index(entries)
        return {"tabs": entries, "active": active}

    def _prior_active_index(self, entries: list[dict]) -> Optional[int]:
        """Resolve the previously-recorded active descriptor to its position in `entries`.
        Used when the live active tab can't be represented (a pristine unsaved blank shot
        tab) so re-capturing the layout preserves the remembered active instead of nulling
        it. Returns None when that descriptor is no longer open - then active falls back to
        the Shots tab on restore, the same as having no remembered active. `prior` is the
        last *persisted* active (ui_state is only re-captured on save/close), not the live
        focus, so closing the recorded-active tab before this fires also yields the fallback."""
        prior = self.project.ui_state or self._default_tab_state()
        tabs = prior.get("tabs") or []
        idx = prior.get("active")
        if isinstance(idx, int) and 0 <= idx < len(tabs) and tabs[idx] in entries:
            return entries.index(tabs[idx])
        return None

    def _default_tab_state(self) -> dict:
        """The layout an empty ui_state restores to: every fixed tab in order, Shots active
        (index 0, matching _restore_tab_state's setCurrentIndex(0) fallback). Used as the
        effective on-disk state so a no-change close of a default-layout project (older/
        seeded files carry no ui_state) compares equal and writes nothing."""
        return {"tabs": [{"kind": "fixed", "key": t} for _, t in self._fixed_tabs],
                "active": 0}

    def _persist_layout_on_close(self) -> None:
        """Record a tab-layout change at window close even when the project is otherwise
        clean (a tab rearrange doesn't set dirty, so _maybe_save_changes wouldn't write it).
        Gated on the layout actually differing from what's on disk, so an unchanged session
        never touches the .animproj mtime. The active-tab index is part of the layout (it's
        serialized into ui_state and Save captures it too), so switching to a different tab
        and closing IS a change worth persisting - the restored project reopens on it. Only
        called on the genuinely-clean close path (titled project, no unsaved authoring
        edits) - see closeEvent."""
        current = self._compute_tab_state()
        on_disk = self.project.ui_state or self._default_tab_state()
        if current == on_disk:
            return
        self.project.ui_state = current
        self.project.persist_ui_state()

    def _restore_tab_state(self) -> None:
        """Rebuild the tab bar from the project's saved layout (or the default full set of
        fixed tabs when there's none). Detaching first keeps the fixed-tab widgets alive
        (removeTab doesn't delete them); shot/take tabs are reopened by id, skipping any
        that no longer exist. Signals are blocked during the rebuild so _on_tab_changed
        (which toggles comfy polling) fires once at the end against the settled tab bar,
        not on every intermediate add/remove."""
        state = self.project.ui_state or {}
        desc = state.get("tabs")
        self.tabs.blockSignals(True)
        try:
            while self.tabs.count():
                self.tabs.removeTab(0)
            built: list = []
            if isinstance(desc, list):
                built = self._apply_tab_descriptors(desc)
            else:
                for widget, title in self._fixed_tabs:   # default: all fixed tabs, original order
                    self.tabs.addTab(widget, title)
            if self.tabs.count() == 0:                    # never leave an empty tab bar: Shots always wins
                w, t = self._fixed_tabs[0]
                self.tabs.addTab(w, t)
            active = state.get("active")
            target = built[active] if isinstance(active, int) and 0 <= active < len(built) else None
            if target is not None and self.tabs.indexOf(target) >= 0:
                self.tabs.setCurrentWidget(target)
            else:
                self.tabs.setCurrentIndex(0)
        finally:
            self.tabs.blockSignals(False)
        self._on_tab_changed(self.tabs.currentIndex())   # one settled sync (comfy polling etc.)

    def _apply_tab_descriptors(self, desc: list) -> list:
        """Re-add tabs in saved order; return the widget built for each descriptor (None
        where it was skipped) so the caller can map the saved active index back to a widget."""
        fixed_by_key = {title: w for w, title in self._fixed_tabs}
        built: list = []
        for entry in desc:
            w = None
            if isinstance(entry, dict):
                kind = entry.get("kind")
                if kind == "fixed":
                    fw = fixed_by_key.get(entry.get("key"))
                    if fw is not None and self.tabs.indexOf(fw) < 0:
                        self.tabs.addTab(fw, entry.get("key"))
                        w = fw
                elif kind == "shot":
                    sid = entry.get("id")
                    self.open_shot(sid)              # no-op on a missing/already-open shot
                    w = self.shot_tabs.get(sid)
                elif kind == "take":
                    tid = entry.get("id")
                    self.open_take(tid)              # no-op on a missing/already-open take
                    w = self.take_tabs.get(tid)
            built.append(w)
        return built

    def _maybe_close_shot_tab(self, tab: ShotTab) -> bool:
        """Confirm before closing a shot tab that has uncommitted editor edits. Returns
        False to keep the tab (and its edits) open — Cancel. Save commits the edits into
        the project buffer (same as the tab's own Save button; the title's * persists
        until File > Save writes to disk); Discard drops them."""
        if not tab.is_dirty():
            return True
        name = (tab.shot.name if tab.shot else tab.name.text().strip()) or "this shot"
        choice = QMessageBox.question(
            self, "Unsaved changes",
            f"Save changes to '{name}' before closing this tab?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            tab.commit()            # flush the edit into the project buffer (no disk write)
            self.reload()           # the new/updated shot's card now reflects it
        return True

    def _on_tab_close(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, TakePlayerTab):
            widget.close_player()              # stop its playback timer before disposal
            self.take_tabs.pop(widget.take_id, None)
            self.tabs.removeTab(index)
            widget.deleteLater()
            return
        if isinstance(widget, ShotTab):
            if not self._maybe_close_shot_tab(widget):
                return              # Cancel - keep the tab open
            for sid, t in list(self.shot_tabs.items()):
                if t is widget:
                    del self.shot_tabs[sid]
            self.tabs.removeTab(index)
            widget.deleteLater()
            return
        if widget is self.comfy_tab:  # fixed tab: detach but keep the widget for reopening
            self.comfy_tab.stop_monitoring()
        self.tabs.removeTab(index)

    def _refresh_shot(self, shot_id: str) -> None:
        """Refresh both the list card and the open detail tab (if any) for a shot."""
        if shot_id in self.cards:
            self.cards[shot_id].refresh_takes()
        if shot_id in self.shot_tabs:
            self.shot_tabs[shot_id].refresh_takes()

    def _refresh_shot_for_take(self, take_id: str) -> None:
        """A take's status signal (status_changed / finished) - update just that take's tile in
        the list card and the open detail tab, in place. Unlike _refresh_shot (used for discrete
        actions like queueing/restart/delete, where a full reload is right), this avoids the
        O(takes-in-shot) model rebuild + every-thumbnail/strip re-decode on every transition
        (card #75). A newly-visible / removed take falls back to a full load inside update_take."""
        take = self.project.get_take(take_id)
        if not take:
            return
        if take.shot_id in self.cards:
            self.cards[take.shot_id].update_take(take_id)
        if take.shot_id in self.shot_tabs:
            self.shot_tabs[take.shot_id].update_take(take_id)
        # A take viewer opened while its take was still generating must catch up when the take
        # finishes / fails / is cancelled — the card/tab fan-out above never reaches take_tabs (L6).
        viewer = self.take_tabs.get(take_id)
        if viewer is not None:
            viewer.refresh_status()

    def _monitor_context(self) -> str:
        """Short app-state string for the heartbeat log (project + queue snapshot)."""
        proj = (self.project.name or "untitled") + ("*" if self.project.dirty else "")
        try:
            pending = self.jobs.pending_count()
        except Exception:  # noqa: BLE001
            pending = "?"
        generating = sum(1 for t in self.project.list_takes() if t.status == STATUS_GENERATING)
        return (f"project={proj} jobs_pending={pending} generating={generating} "
                f"shot_tabs={len(self.shot_tabs)} visible={self.isVisible()} "
                f"minimized={self.isMinimized()}")

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        # A "vanish" can be a hide/minimize (process still alive) rather than a close/crash.
        # Logged so heartbeats continuing after this line tell you it was only hidden.
        applog.logger.info("window hidden (spontaneous=%s, minimized=%s) - process still running",
                           event.spontaneous(), self.isMinimized())
        super().hideEvent(event)

    def _confirm_close_with_inflight_renders(self) -> bool:
        """If any take is still GENERATING (either backend), warn before closing - a hosted
        render keeps BILLING on Replicate after the app exits and would be left a frozen
        "running" row until next-launch reconciliation. Returns True to proceed with the close,
        False to abort it. Offers to STOP the in-flight renders first (best-effort
        jobs.request_stop - stops spend/GPU, NEVER creates any). No takes generating -> True."""
        takes = generating_takes(self.project)
        text = close_warning_text(takes)
        if text is None:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Renders still in progress")
        box.setText(text)
        close_btn = box.addButton("Close anyway", QMessageBox.ButtonRole.DestructiveRole)
        stop_btn = box.addButton("Stop renders && close", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is stop_btn:
            for t in takes:                      # best-effort: stops spend/GPU, never creates it
                try:
                    self.jobs.request_stop(t.id)
                except Exception as e:  # noqa: BLE001 - a failed stop must not block the close
                    applog.logger.info("request_stop failed for %s at close: %s", t.id[:8], e)
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # spontaneous() = the window system initiated it (you clicked X / the OS asked it
        # to close) vs a programmatic close. Logged so a vanished window is explainable.
        spontaneous = event.spontaneous()
        if not self._confirm_close_with_inflight_renders():
            applog.logger.info("close aborted at in-flight-render prompt (spontaneous=%s)",
                               spontaneous)
            event.ignore()
            return
        had_edits = self._has_unsaved_edits()
        if not self._maybe_save_changes():
            applog.logger.info("close aborted at unsaved-changes prompt (spontaneous=%s)", spontaneous)
            event.ignore()
            return
        applog.logger.info("window closing (spontaneous=%s) - %s", spontaneous,
                           "user/OS closed the window" if spontaneous else "programmatic close")
        # A tab rearrange on an otherwise-clean project doesn't set dirty, so _maybe_save_changes
        # didn't persist it. Record it now - but only on the genuinely-clean path: if there WERE
        # edits, the user either Saved (save_project's _capture_tab_state already wrote ui_state)
        # or Discarded (must not write the discarded shots back to disk). Skipping on had_edits is
        # safe precisely because the Save branch captures the layout itself. Untitled has nowhere
        # to write without a Save-As prompt.
        if not had_edits and not self.project.is_untitled:
            self._persist_layout_on_close()
        if self._remote is not None:
            self._remote.stop()
        self.comfy_tab.stop_monitoring()
        self.library_tab.stop_fetch()   # stop the off-thread schema fetch before teardown
        super().closeEvent(event)

    # ---- job signal handlers -------------------------------------------
    def _log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _card_for_take(self, take_id: str):
        t = self.project.get_take(take_id)
        return self.cards.get(t.shot_id) if t else None

    def _on_progress(self, take_id: str, line: str) -> None:
        self._log(f"  {take_id[:8]}: {line}")

    def _on_status_changed(self, take_id: str, status: str) -> None:
        self._log(f"[{status}] {take_id[:8]}")
        self._refresh_shot_for_take(take_id)
        self._refresh_cancel_action()
        # A take flipping to/from CANCELLED changes whether 'Remove cancelled takes' has
        # anything to do; refresh its enablement here (not only on reload()) so it's live
        # right after Cancel pending, without waiting on an unrelated refresh (L12).
        self._refresh_purge_cancelled_action()
        if self._batch is not None:
            self._batch.mark(take_id, status)
            if self._batch.complete:
                self._finalize_batch()
        elif self._stop_paused_local and not self._local_work_in_flight():
            # The deliberately-stopped non-batch local queue has drained: lift the transient
            # pause so a later render recovers from a genuine crash normally (card #42).
            self._stop_paused_local = False
            self.jobs.clear_local_pause()
            self._log("local queue idle — ComfyUI stop complete, crash-recovery re-armed")

    def _after_job(self, take_id: str, msg: str) -> None:
        self._log(msg)
        self._refresh_shot_for_take(take_id)
        self._refresh_cancel_action()
        self._refresh_purge_cancelled_action()   # keep 'Remove cancelled takes' live (L12)
