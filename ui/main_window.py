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

import json
import shutil
import tempfile
import threading
from collections import Counter
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QFileDialog, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

import library
import paths
from backends import comfy_client, recovery, replicate_client
from backends.jobs import JobManager
from pipeline import export, framing
from store import app_settings
from store.project import Project
from store.models import (
    STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_GENERATING, STATUS_PENDING,
)
from ui.assets_view import AssetsView
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
        self.ready.emit(hist, queue)


class MainWindow(QMainWindow):
    def __init__(self, project: Project):
        super().__init__()
        self.project = project
        self.cards: dict[str, ShotCard] = {}
        self.shot_tabs: dict[str, ShotTab] = {}   # shot_id -> its open detail/edit tab
        self.take_tabs: dict[str, TakePlayerTab] = {}  # take_id -> its open viewer tab

        self.jobs = JobManager(project)
        self.jobs.progress.connect(self._on_progress)
        self.jobs.status_changed.connect(self._on_status_changed)
        self.jobs.finished.connect(lambda tid: self._after_job(tid, f"✓ done {tid[:8]}"))
        self.jobs.failed.connect(
            lambda tid, err: self._after_job(tid, f"✗ FAILED {tid[:8]}: {err}"))

        self.resize(1180, 820)
        self._build_body()
        self._build_menu()
        self.reload()
        self._recover_orphans()   # reclaim/clear takes a prior session left mid-render
        self._maybe_refresh_schemas_on_startup()

    # ---- construction ---------------------------------------------------
    def _build_controls(self) -> QToolBar:
        """The Shots-tab control strip (filters + view actions). Lives inside the Shots
        tab, not above the tabs, since every control here acts only on that view."""
        tb = QToolBar("Shot controls")
        tb.setMovable(False)
        tb.addWidget(QLabel(" Model: "))
        self.model_filter = QComboBox()
        self.model_filter.currentIndexChanged.connect(self.reload)
        tb.addWidget(self.model_filter)
        self.starred_filter = QCheckBox("Starred only")
        self.starred_filter.stateChanged.connect(self.reload)
        tb.addWidget(self.starred_filter)
        exp_view = QAction("Export view", self)
        exp_view.triggered.connect(self.export_current_view)
        tb.addAction(exp_view)
        self.cancel_act = QAction("Cancel pending", self)
        self.cancel_act.setToolTip("Cancel all queued generations that haven't started yet")
        self.cancel_act.triggered.connect(self.cancel_pending)
        self.cancel_act.setEnabled(False)
        tb.addAction(self.cancel_act)

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

    def _build_body(self) -> None:
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
        shots_layout.addWidget(self._build_controls())
        shots_layout.addWidget(scroll, 1)

        # Model Library and ComfyUI Status used to be separate top-level windows; they're
        # now tabs alongside the shots view. The monitor only polls while its tab is on
        # screen (see _on_tab_changed) to avoid hammering a down port in the background.
        self.queue_tab = QueueView(self.project, self.jobs)
        self.assets_tab = AssetsView(self.project)
        self.library_tab = ModelLibraryWindow(self)
        self.comfy_tab = ComfyMonitorWindow(self)

        self.shots_tab = shots_tab
        # Fixed tabs are closable (the x) and reopen from the View menu; shot tabs are
        # dynamic (reopen by opening the shot again).
        self._fixed_tabs = [(shots_tab, "Shots"), (self.queue_tab, "Queue"),
                            (self.assets_tab, "Assets"),
                            (self.library_tab, "Model Library"),
                            (self.comfy_tab, "ComfyUI Status")]

        self.tabs = QTabWidget()
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
        self._recover_orphans()   # reclaim/clear takes a prior session left mid-render

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
            if starred_only and not self.project.list_takes(shot.id, starred_only=True):
                continue
            card = ShotCard(self.project, shot)
            card.generate_requested.connect(self.generate_shot)
            card.open_requested.connect(self.open_shot)
            card.duplicate_requested.connect(self.duplicate_shot)
            card.delete_requested.connect(self.delete_shot)
            card.export_takes_requested.connect(self.export_takes)
            card.open_take_requested.connect(self.open_take)
            if shot.id in expanded:
                card.expand_btn.setChecked(True)
            self.cards_layout.addWidget(card)
            self.cards[shot.id] = card
            shown += 1

        self.cards_layout.addWidget(self._make_add_shot_card())
        self.statusBar().showMessage(f"{shown} shots shown · {len(shots)} total")
        self._refresh_total_price(shots)
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
        tab = ShotTab(self.project)
        self._wire_shot_tab(tab)
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, tab.title()))

    def open_shot(self, shot_id: str) -> None:
        if shot_id in self.shot_tabs:
            self.tabs.setCurrentWidget(self.shot_tabs[shot_id])
            return
        shot = self.project.get_shot(shot_id)
        if not shot:
            return
        tab = ShotTab(self.project, shot=shot)
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
        self.take_tabs[take_id] = tab
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, title))

    def duplicate_shot(self, shot_id: str) -> None:
        dup = self.project.duplicate_shot(shot_id)
        if not dup:
            return
        self.reload()
        self._log(f"duplicated shot -> {dup.name}")

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
        for t in inflight:
            self.jobs.request_stop(t.id)
        self.project.delete_shot(shot_id)
        self.reload()
        self._refresh_cancel_action()
        self.queue_tab.refresh()
        note = ""
        if cancelled or inflight:
            bits = []
            if cancelled:
                bits.append(f"cancelled {cancelled} queued")
            if inflight:
                bits.append(f"stopped {len(inflight)} rendering")
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

    def _on_shot_saved(self, shot_id: str, tab: ShotTab) -> None:
        # A blank tab just became a real shot (or an existing shot was re-saved): register
        # it, refresh the tab label, and rebuild the list so the card appears/updates.
        self.shot_tabs[shot_id] = tab
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, tab.title())
        self.reload()

    def generate_shot(self, shot_id: str) -> None:
        shot = self.project.get_shot(shot_id)
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
            shot = self.project.get_shot(shot_id)

        settings = {**model.get("default_params", {}), **shot.settings}
        est = library.estimate_cost(shot.model_id, settings)
        item = {"name": shot.name, "model_display": model["display_name"],
                "backend": model["backend"], "est_cost": est, "params": settings}
        if not confirm_launch(self, [item]):
            self._log("launch cancelled")
            return

        # Persist the project so the take never references an unsaved shot (untitled ->
        # Save As prompt). Aborting the save aborts the generation.
        if not self.save_project():
            self._log("generation cancelled (project not saved)")
            return

        # A 'random' shot rolls a fresh concrete seed per take (so a batch varies); record
        # it on the take/snapshot for reproducibility. A fixed seed passes through unchanged.
        if settings.get("seed") == library.SEED_RANDOM:
            settings = {**settings, "seed": library.resolve_seed(library.SEED_RANDOM)}

        snapshot = {
            "model_id": shot.model_id, "backend": model["backend"],
            "replicate_model_id": model.get("replicate_model_id"),
            "workflow_template": model.get("workflow_template"),
            "start_frame": shot.start_frame, "end_frame": shot.end_frame,
            "prompt": shot.prompt, "negative_prompt": shot.negative_prompt, "settings": settings,
        }
        take = self.project.add_take(shot.id, status=STATUS_PENDING,
                                     seed=settings.get("seed"), cost_estimate=est,
                                     settings_snapshot=snapshot)
        self.jobs.enqueue(take.id, model["backend"],
                          self._make_runner(model, shot, settings, take.id))
        self._log(f"queued {take.id[:8]} ({shot.name})")
        self._refresh_shot(shot.id)
        self._refresh_cancel_action()
        self.queue_tab.refresh()   # a freshly-queued take emits no signal until it starts

    def cancel_pending(self) -> None:
        n = self.jobs.cancel_pending()
        self._log(f"cancelled {n} pending generation(s)" if n
                  else "no pending generations to cancel")
        self._refresh_cancel_action()

    def _refresh_cancel_action(self) -> None:
        self.cancel_act.setEnabled(self.jobs.pending_count() > 0)

    def _make_runner(self, model, shot, settings, take_id):
        # Keyposes are framed from the shot's assets at generation time (on the worker
        # thread) into a temp dir, rather than baked at save time. See framing.render_keyposes.
        out_path = self.project.takes_dir / f"{take_id}.mp4"
        if model["backend"] == "replicate":
            rid = model["replicate_model_id"]
            data_uri = model.get("requires_data_uri", False)
            extra = {k: v for k, v in settings.items() if k not in _EXPLICIT_SETTINGS}

            def on_submit(pid):   # persist the prediction id so a delete-while-rendering
                self.project.update_take(take_id, backend_job_id=pid)  # can cancel it

            def runner(progress):
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
            start_kp, end_kp = framing.render_keyposes(shot, tempfile.mkdtemp(prefix="animgen_kp_"))
            return comfy_client.generate(
                tpl, out_path, start=start_kp, end=end_kp,
                prompt=shot.prompt or None, negative=shot.negative_prompt or None,
                seed=settings.get("seed"), node_roles=roles, sets=size_sets,
                progress_cb=progress, on_submit=on_submit,
                text_encoder_cpu=True)   # keep the ~6GB encoder off the 12GB card (see comfy_client)
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

        On load there are no live workers, so any comfyui take still at generating/pending
        is orphaned. Fetch ComfyUI state off-thread (the server outlives the app), then
        reclaim finished renders, re-attach running ones, and clear the dead ones."""
        if not recovery.comfy_orphans(self.project):
            return
        proj = self.project                       # guard against a project switch mid-fetch
        self._reconciler = _OrphanReconciler()    # kept on self so it isn't GC'd
        self._reconciler.ready.connect(lambda h, q: self._apply_recovery(proj, h, q))
        self._reconciler.start()

    def _apply_recovery(self, proj, history, queue) -> None:
        if proj is not self.project:              # user switched projects before the fetch returned
            return
        orphans = recovery.comfy_orphans(proj)
        if not orphans:
            return
        if history is None:                       # ComfyUI unreachable - can't tell finished
            cancelled = 0                         # from lost; only clear never-submitted takes
            for t in orphans:
                if t.status == STATUS_PENDING:
                    proj.update_take(t.id, status=STATUS_CANCELLED,
                                     error="not submitted before restart; re-Generate to run it")
                    self._refresh_shot(t.shot_id)
                    cancelled += 1
            left = len(orphans) - cancelled
            parts = []
            if cancelled:
                parts.append(f"cancelled {cancelled} un-submitted")
            if left:
                parts.append(f"left {left} unfinished (ComfyUI unreachable)")
            if parts:
                self._log("orphan recovery: " + "; ".join(parts))
            self._refresh_cancel_action()
            return
        self._execute_plans(recovery.plan_comfy_recovery(orphans, history, queue))

    def _execute_plans(self, plans) -> None:
        counts: Counter = Counter()
        for p in plans:
            if p.action == recovery.RECLAIM:
                try:
                    dst = self.project.takes_dir / f"{p.take_id}.mp4"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p.output_path, dst)
                    self.project.update_take(
                        p.take_id, status=STATUS_DONE, video_path=str(dst),
                        backend_job_id=p.prompt_id,
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
                self.project.update_take(p.take_id, status=STATUS_FAILED, error=p.reason)
            elif p.action == recovery.CANCEL:
                self.project.update_take(p.take_id, status=STATUS_CANCELLED, error=p.reason)
            counts[p.action] += 1
            self._log(f"orphan {p.take_id[:8]}: {p.reason}")
            self._refresh_shot(p.shot_id)
        if counts:
            self._log("orphan recovery: " + ", ".join(f"{n} {a}" for a, n in counts.items()))
        self._refresh_cancel_action()

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
            msg += f"\n\n({skipped} skipped - no video file)"
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
        take = self.project.get_take(take_id)
        if take:
            self._refresh_shot(take.shot_id)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._maybe_save_changes():
            event.ignore()
            return
        self.comfy_tab.stop_monitoring()
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

    def _after_job(self, take_id: str, msg: str) -> None:
        self._log(msg)
        self._refresh_shot_for_take(take_id)
        self._refresh_cancel_action()
