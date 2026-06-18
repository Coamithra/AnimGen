"""Shot tab - the full editor + takes view for one shot, shown as a dedicated tab.

Choose start/end keyframes from the project's assets (left-click a slot to frame it,
double-click to pick), set the canvas aspect ratio (offered per the model), drag/scale
each keyframe within the aspect canvas, write the prompt, tune the output/model
settings, and see/generate this shot's takes - all inline.

Placement is stored per keyframe under shot.crop = {aspect, start:{...}, end:{...}};
the 1254-class (hosted) or pixel-budget (local) canvas is computed from the aspect, and
the keypose is framed at generation time (pipeline.framing.render_keyposes). New shots
open as a blank tab.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

import library
from backends import replicate_client
from pipeline import framing
from store import prompt_library, schema_cache
from store.project import Project
from ui.asset_picker import AssetPickerDialog
from ui.placement_widget import PlacementCanvas, pil_to_pixmap
from ui.takes_view import TakesView

_PARAM_ORDER = ["duration", "resolution", "seed", "camera_fixed", "mode", "length"]
_DEFAULT_PLACEMENT = {"scale": 0.65, "cx": 0.5, "cy": 0.6}
_WAN_FPS = 16                                          # local Wan renders at a fixed 16 fps
_OUTPUT_PARAMS = {"resolution", "duration", "length"}  # go in the output_form, not the Model settings group
_NEG_UNSUPPORTED_MSG = "This model does not support negative prompting."


class _KeyframeButton(QPushButton):
    """Thumbnail slot: left-click selects it for framing, double-click opens the picker."""
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt override
        self.doubleClicked.emit()


class ShotTab(QWidget):
    saved = Signal(str)              # shot_id (after a successful save)
    generate_requested = Signal(str)  # shot_id
    export_requested = Signal(list)   # take ids
    open_take_requested = Signal(str)  # take id -> open in the frame-by-frame viewer tab
    restart_requested = Signal(list)   # cancelled / crash-interrupted-failed take ids -> re-run them
    dirty_changed = Signal()          # this tab's unsaved-edits state flipped

    def __init__(self, project: Project, shot=None, parent=None, jobs=None):
        super().__init__(parent)
        self.project = project
        self.shot = shot
        self.jobs = jobs
        self._schema: Optional[dict] = None
        self._param_getters: dict[str, Callable] = {}
        self._takes_view: Optional[TakesView] = None
        self._assets: dict[str, Optional[str]] = {"start": None, "end": None}
        self._frames: dict[str, dict] = {"start": dict(_DEFAULT_PLACEMENT),
                                          "end": dict(_DEFAULT_PLACEMENT)}
        self._keyed_cache: dict = {}   # asset path -> keyed PIL sprite (thumb reuse)
        self._active = "start"
        self._dirty = False
        self._neg_stash: Optional[str] = None  # real negative text, held while masked (unsupported model)
        self._suppress = True          # block dirty-marking while building/loading widgets
        self._build()
        if shot:
            self._load(shot)
        else:
            self._select("start")
        self._update_action_state()
        self._suppress = False         # subsequent widget changes are real user edits

    def title(self) -> str:
        base = self.shot.name if self.shot else "New shot"
        return f"{base}*" if self._dirty else base

    # ---- dirty tracking -------------------------------------------------
    def is_dirty(self) -> bool:
        return self._dirty

    def _mark_dirty(self) -> None:
        if self._suppress or self._dirty:
            return
        self._dirty = True
        self.dirty_changed.emit()

    def _clear_dirty(self) -> None:
        if self._dirty:
            self._dirty = False
            self.dirty_changed.emit()

    def _wire_dirty(self, widget: QWidget) -> None:
        """Mark this tab dirty whenever a built editor widget changes. Param/output rows
        are sometimes a container host (today only the length spin + hint), so fall back to
        its concrete child editors. QLineEdit is intentionally absent from the descend set:
        no container hosts a free-text field, and including it would also match the private
        line edit inside a QSpinBox. Top-level QLineEdit rows are still handled by _wire_one."""
        if not self._wire_one(widget):
            for cls in (QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox):
                for child in widget.findChildren(cls):
                    self._wire_one(child)

    def _wire_one(self, w: QWidget) -> bool:
        if isinstance(w, QCheckBox):
            w.toggled.connect(self._mark_dirty)
        elif isinstance(w, QComboBox):
            w.currentIndexChanged.connect(self._mark_dirty)
        elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
            w.valueChanged.connect(self._mark_dirty)
        elif isinstance(w, QLineEdit):
            w.textChanged.connect(self._mark_dirty)
        else:
            return False
        return True

    # ---- construction ---------------------------------------------------
    def _build(self) -> None:
        self.name = QLineEdit()
        self.name.textChanged.connect(self._mark_dirty)
        self.model_combo = QComboBox()
        for m in library.models():
            self.model_combo.addItem(m["display_name"], m["id"])
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.model_combo.currentIndexChanged.connect(self._mark_dirty)

        self.aspect_combo = QComboBox()
        self.aspect_combo.currentIndexChanged.connect(self._on_aspect_changed)
        self.aspect_combo.currentIndexChanged.connect(self._mark_dirty)
        self.canvas_lbl = QLabel("")
        self.canvas_lbl.setStyleSheet("color: gray;")

        top = QFormLayout()
        top.addRow("Name", self.name)
        top.addRow("Model", self.model_combo)
        arow = QHBoxLayout(); arow.addWidget(self.aspect_combo); arow.addWidget(self.canvas_lbl, 1)
        aw = QWidget(); aw.setLayout(arow); top.addRow("Aspect", aw)
        top.addRow(self._build_keyframes())

        self.canvas = PlacementCanvas()
        self.canvas.changed.connect(self._on_placement_changed)
        self.canvas.changed.connect(self._mark_dirty)

        self.prompt = QPlainTextEdit(); self.prompt.setPlaceholderText("Prompt…")
        self.prompt.textChanged.connect(self._mark_dirty)
        self.negative = QPlainTextEdit(); self.negative.setPlaceholderText("Negative prompt…")
        self.negative.textChanged.connect(self._mark_dirty)
        self.negative.setPlainText(library.default_negative_prompt())
        self.negative_label = QLabel("Negative")
        prompt_box = QGroupBox("Prompt")
        pv = QVBoxLayout(prompt_box)
        pv.addLayout(self._build_template_row())
        pv.addWidget(QLabel("Positive")); pv.addWidget(self.prompt)
        pv.addWidget(self.negative_label); pv.addWidget(self.negative)

        self.params_form = QFormLayout()

        # Settings tab: resolution + duration (model-aware), then the remaining model params,
        # then read-only fps and est. price. The whole tab is the model's settings, so the
        # two form sections flow together with no inner group-box label.
        self.output_form = QFormLayout()
        self.fps_value = QLabel("—"); self.fps_value.setStyleSheet("color: gray;")
        self.price_value = QLabel("—"); self.price_value.setStyleSheet("font-weight: bold;")
        output_tab = QWidget()
        ov = QVBoxLayout(output_tab)
        ov.addLayout(self.output_form)
        ov.addLayout(self.params_form)
        fps_line = QHBoxLayout()
        fps_line.addWidget(QLabel("Output FPS")); fps_line.addWidget(self.fps_value, 1)
        ov.addLayout(fps_line)
        price_line = QHBoxLayout()
        price_line.addWidget(QLabel("Est. price")); price_line.addWidget(self.price_value, 1)
        ov.addLayout(price_line); ov.addStretch(1)

        tabs = QTabWidget()
        tabs.addTab(self.canvas, "Framing")
        tabs.addTab(output_tab, "Settings")
        prompt_tab = QWidget()
        sv = QVBoxLayout(prompt_tab)
        sv.addWidget(prompt_box)
        tabs.addTab(prompt_tab, "Prompt")

        self._takes_host = QWidget()
        self._takes_layout = QVBoxLayout(self._takes_host)
        # The Export-takes (whole-shot) action lives here, in the Takes subtab, beside the
        # takes grid - not in the always-visible bottom row.
        self.export_btn = QPushButton("Export takes"); self.export_btn.clicked.connect(self._export)
        takes_head = QHBoxLayout(); takes_head.addStretch(1); takes_head.addWidget(self.export_btn)
        self._takes_layout.addLayout(takes_head)
        self._takes_placeholder = QLabel("Save the shot, then Generate to create takes.")
        self._takes_placeholder.setStyleSheet("color: gray;")
        self._takes_layout.addWidget(self._takes_placeholder); self._takes_layout.addStretch(1)
        tabs.addTab(self._takes_host, "Takes")

        self.save_btn = QPushButton("Save"); self.save_btn.clicked.connect(self._save)
        self.gen_btn = QPushButton("Generate"); self.gen_btn.clicked.connect(self._generate)
        self.status_lbl = QLabel(""); self.status_lbl.setStyleSheet("color: gray;")
        btn_row = QHBoxLayout()
        for b in (self.save_btn, self.gen_btn):
            btn_row.addWidget(b)
        btn_row.addWidget(self.status_lbl); btn_row.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(tabs, 1)
        lay.addLayout(btn_row)

        self._rebuild_params()
        self._populate_aspects()

    def _make_kf_button(self, which: str) -> _KeyframeButton:
        btn = _KeyframeButton()
        btn.setIconSize(QSize(168, 116))     # framed-preview thumbnail
        btn.setMinimumHeight(128)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.clicked.connect(lambda: self._select(which))
        btn.doubleClicked.connect(lambda: self._pick(which))
        return btn

    def _build_keyframes(self) -> QWidget:
        """Start + End keyframe slots, side by side. Each tile shows the framed preview
        (keyed sprite placed on the aspect canvas), a filename line, and a Clear button."""
        self.start_btn = self._make_kf_button("start")
        self.end_btn = self._make_kf_button("end")
        clr_start = QPushButton("Clear"); clr_start.clicked.connect(lambda: self._clear("start"))
        clr_end = QPushButton("Clear"); clr_end.clicked.connect(lambda: self._clear("end"))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._kf_card("start", "Start keyframe", self.start_btn, clr_start), 1)
        row.addWidget(self._kf_card("end", "End keyframe (optional)", self.end_btn, clr_end), 1)
        self.copy_se_btn = QPushButton("Copy Start → End")
        self.copy_se_btn.setToolTip("Use the start keyframe (image + framing) as the end keyframe")
        self.copy_se_btn.clicked.connect(self._copy_start_to_end)
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0)
        crow.addStretch(1); crow.addWidget(self.copy_se_btn)
        box = QVBoxLayout(); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(4)
        box.addLayout(row); box.addLayout(crow)
        host = QWidget(); host.setLayout(box)
        self._refresh_copy_btn()
        return host

    def _kf_card(self, which: str, caption: str, btn: QPushButton, clr: QPushButton) -> QWidget:
        cap = QLabel(caption); cap.setStyleSheet("color: gray;")
        name = QLabel("— none —"); name.setStyleSheet("color: #888;")
        if which == "start":
            self.start_name = name
        else:
            self.end_name = name
        foot = QHBoxLayout(); foot.setContentsMargins(0, 0, 0, 0)
        foot.addWidget(name, 1); foot.addWidget(clr)
        box = QVBoxLayout(); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(3)
        box.addWidget(cap); box.addWidget(btn, 1); box.addLayout(foot)
        card = QWidget(); card.setLayout(box)
        return card

    # ---- prompt templates ----------------------------------------------
    def _build_template_row(self) -> QHBoxLayout:
        """Load/save buttons for the app-global prompt prefab library."""
        load_btn = QPushButton("Load template"); load_btn.clicked.connect(self._load_template)
        load_btn.setToolTip("Replace the prompt with a saved prefab (shared across all projects)")
        save_btn = QPushButton("Save as template"); save_btn.clicked.connect(self._save_template)
        save_btn.setToolTip("Save the current prompt as a reusable prefab")
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Template"))
        row.addWidget(load_btn); row.addWidget(save_btn)
        row.addStretch(1)
        return row

    def _load_template(self) -> None:
        names = [t["name"] for t in prompt_library.all_templates()]
        if not names:
            QMessageBox.information(self, "Load template", "No saved templates yet.")
            return
        name, ok = QInputDialog.getItem(self, "Load prompt template",
                                        "Template:", names, 0, False)
        if ok and name:
            self._apply_template_by_name(name)

    def _apply_template_by_name(self, name: str) -> None:
        t = prompt_library.get(name)
        if not t:
            return
        self.prompt.setPlainText(t["positive"])      # fires textChanged -> marks dirty
        self._set_negative(t["negative"])             # stash-aware (kept hidden while masked)
        self._mark_dirty()

    def _save_template(self) -> None:
        name, ok = QInputDialog.getText(self, "Save prompt template", "Template name:")
        name = name.strip()
        if not (ok and name):
            return
        if prompt_library.get(name) is not None:
            if QMessageBox.question(self, "Overwrite template?",
                                    f"A template named “{name}” already exists. Overwrite it?"
                                    ) != QMessageBox.StandardButton.Yes:
                return
        prompt_library.save(name, self.prompt.toPlainText(), self._negative_value())

    def _update_action_state(self) -> None:
        self.export_btn.setEnabled(self.shot is not None)

    def _ensure_takes_view(self) -> None:
        if self._takes_view is None and self.shot is not None:
            self._takes_placeholder.hide()
            self._takes_view = TakesView(self.project, self.shot.id, jobs=self.jobs)
            self._takes_view.export_requested.connect(self.export_requested)
            self._takes_view.open_take_requested.connect(self.open_take_requested)
            self._takes_view.restart_requested.connect(self.restart_requested)
            self._takes_layout.insertWidget(1, self._takes_view, 1)

    def refresh_takes(self) -> None:
        if self._takes_view is not None:
            self._takes_view.load()
        self._refresh_fps_label()   # a new take's measured fps may now be available

    # ---- aspect / canvas ------------------------------------------------
    def _is_local(self) -> bool:
        m = self._current_model()
        return bool(m and m.get("backend") == "comfyui")

    def _effective_resolution(self) -> Optional[str]:
        """The hosted model's resolution tier driving the readout - the live param if the
        model exposes one, else its first resolution_options entry. None for local."""
        if self._is_local():
            return None
        res = self._params().get("resolution")
        if res:
            return str(res)
        opts = (self._current_model() or {}).get("resolution_options") or []
        return str(opts[0]) if opts else None

    def selected_aspect(self) -> str:
        return self.aspect_combo.currentText()

    def aspect_valid(self) -> bool:
        m = self._current_model()
        ok = bool(m) and self.selected_aspect() in library.aspect_ratios(m["id"])
        self.aspect_combo.setStyleSheet("" if ok else "QComboBox { border: 2px solid #d9534f; }")
        return ok

    def _populate_aspects(self, preferred: Optional[str] = None) -> None:
        m = self._current_model()
        allowed = library.aspect_ratios(m["id"]) if m else ["1:1"]
        keep = preferred or self.aspect_combo.currentText() or (allowed[0] if allowed else "1:1")
        self.aspect_combo.blockSignals(True)
        self.aspect_combo.clear()
        self.aspect_combo.addItems(allowed)
        if keep and keep not in allowed:        # keep an out-of-list choice visible (flagged red)
            self.aspect_combo.addItem(keep)
        self.aspect_combo.setCurrentText(keep)
        self.aspect_combo.blockSignals(False)
        self._on_aspect_changed()

    def _on_aspect_changed(self) -> None:
        self.aspect_valid()
        w, h = framing.display_size(self.selected_aspect(),
                                    resolution=self._effective_resolution(), local=self._is_local())
        self._canvas = (w, h)
        self.canvas_lbl.setText(f"Canvas: {w}×{h}")
        self.canvas.set_aspect(w, h)
        self._update_kf_thumb("start"); self._update_kf_thumb("end")   # aspect changed -> reframe tiles
        if hasattr(self, "price_value"):
            self._refresh_price()   # resolution drives the canvas AND the per-resolution price

    def _on_placement_changed(self) -> None:
        self._frames[self._active] = self.canvas.get_placement()
        self._update_kf_thumb(self._active)

    # ---- keyframe slots -------------------------------------------------
    def _select(self, which: str) -> None:
        if self.sender() is not None and self._active in ("start", "end"):
            self._frames[self._active] = self.canvas.get_placement()  # stash current
            self._update_kf_thumb(self._active)                        # reflect new framing on its tile
        self._active = which
        self.canvas.set_sprite(self._keyed_pixmap(self._assets[which]))
        self.canvas.set_placement(self._frames[which])
        self._refresh_kf_styles()

    def _pick(self, which: str) -> None:
        dlg = AssetPickerDialog(self.project, current=self._assets[which], parent=self)
        if dlg.exec() and dlg.selected():
            self._set_asset(which, dlg.selected())
            self._select(which)
            self._mark_dirty()

    def _clear(self, which: str) -> None:
        had_asset = self._assets[which] is not None
        self._set_asset(which, None)
        if self._active == which:
            self.canvas.set_sprite(None)
        if had_asset:                  # clearing an already-empty slot isn't an edit
            self._mark_dirty()

    def _copy_start_to_end(self) -> None:
        """Mirror the start keyframe (image + placement) onto the end slot."""
        if not self._assets["start"]:
            return
        if self._active == "start":
            self._frames["start"] = self.canvas.get_placement()  # flush live start edits
        self._set_asset("end", self._assets["start"])
        self._frames["end"] = dict(self._frames["start"])
        if self._active == "end":
            self.canvas.set_sprite(self._keyed_pixmap(self._assets["end"]))
            self.canvas.set_placement(self._frames["end"])
        self._update_kf_thumb("end")
        self._mark_dirty()

    def _refresh_copy_btn(self) -> None:
        self.copy_se_btn.setEnabled(bool(self._assets["start"]))

    def _set_asset(self, which: str, path: Optional[str]) -> None:
        self._assets[which] = path or None
        name_lbl = self.start_name if which == "start" else self.end_name
        name_lbl.setText(Path(path).name if (path and Path(path).exists()) else "— none —")
        self._update_kf_thumb(which)
        self._refresh_copy_btn()

    def _refresh_kf_styles(self) -> None:
        for which, btn in (("start", self.start_btn), ("end", self.end_btn)):
            active = which == self._active
            btn.setStyleSheet("padding: 2px; border: %s;" %
                              ("2px solid #5fa97a" if active else "1px solid #555"))

    def _keyed_pixmap(self, asset: Optional[str]):
        if not (asset and Path(asset).exists()):
            return None
        try:
            return pil_to_pixmap(framing.keyed_sprite(asset, crop_to_content=False))
        except Exception:  # noqa: BLE001 - unreadable image -> empty canvas
            return None

    # ---- framed-keyframe thumbnails -------------------------------------
    def _thumb_canvas(self, long: int = 256) -> tuple[int, int]:
        """A small canvas matching the current aspect, for rendering tile previews."""
        w, h = getattr(self, "_canvas", (1, 1))
        w, h = (w or 1), (h or 1)
        if w >= h:
            return long, max(1, round(long * h / w))
        return max(1, round(long * w / h)), long

    def _framed_pixmap(self, which: str):
        """Preview of a keyframe AS FRAMED: the keyed sprite placed on the aspect canvas
        at its {scale,cx,cy}. Reuses a cached keyed sprite so re-renders are cheap."""
        asset = self._assets[which]
        if not (asset and Path(asset).exists()):
            return None
        try:
            sprite = self._keyed_cache.get(asset)
            if sprite is None:
                sprite = framing.keyed_sprite(asset, crop_to_content=False)
                self._keyed_cache[asset] = sprite
            img = framing.render_placement(asset, self._frames[which], self._thumb_canvas(),
                                           sprite=sprite)
            return pil_to_pixmap(img)
        except Exception:  # noqa: BLE001 - unreadable image -> empty slot
            return None

    def _update_kf_thumb(self, which: str) -> None:
        btn = self.start_btn if which == "start" else self.end_btn
        pm = self._framed_pixmap(which)
        if pm is not None:
            btn.setIcon(QIcon(pm)); btn.setText("")
        else:
            btn.setIcon(QIcon()); btn.setText("Choose…")

    # ---- model + params -------------------------------------------------
    def _current_model(self) -> dict:
        return library.get_model(self.model_combo.currentData())

    def _on_model_changed(self) -> None:
        self._rebuild_params()        # re-reads the schema -> re-masks the negative box
        self._populate_aspects()      # offer this model's aspects; flag if current is invalid

    # ---- negative prompt (schema-gated) ---------------------------------
    def _negative_value(self) -> str:
        """The logical negative prompt, whether or not it's currently masked/hidden."""
        return self._neg_stash if self._neg_stash is not None else self.negative.toPlainText()

    def _set_negative_text(self, text: str) -> None:
        """Set the box text programmatically (no dirty mark)."""
        self.negative.blockSignals(True)
        self.negative.setPlainText(text)
        self.negative.blockSignals(False)

    def _set_negative(self, text: str) -> None:
        """Write the logical negative value, respecting the masked state (no dirty mark)."""
        if self._neg_stash is not None:
            self._neg_stash = text
        else:
            self._set_negative_text(text)

    def _negative_supported(self) -> Optional[bool]:
        """Does the current model accept a negative prompt? True/False, or None when the
        Replicate schema hasn't been fetched yet (so we can't tell — leave it editable).

        Authoritative source is the live input schema (mirrors replicate_client._pick_field
        over ALIASES["negative"]); local models declare it via comfy_nodes.negative."""
        model = self._current_model()
        if not model:
            return None
        if model.get("backend") == "comfyui":
            return bool((model.get("comfy_nodes") or {}).get("negative"))
        if self._schema is None:           # never fetched -> unknown
            return None
        return any(name in self._schema for name in replicate_client.ALIASES["negative"])

    def _refresh_negative_state(self) -> None:
        """Grey out + disable the Negative box for models that ignore it, replacing the text
        with an explanatory placeholder. The real text is stashed and restored when a model
        that does support negatives is selected again (so it's never silently lost)."""
        supported = self._negative_supported()
        enabled = supported is not False     # True or unknown -> editable
        if enabled:
            if self._neg_stash is not None:          # leaving masked state -> restore text
                self._set_negative_text(self._neg_stash)
                self._neg_stash = None
            self.negative.setPlaceholderText("Negative prompt…")
        else:
            if self._neg_stash is None:              # entering masked state -> stash + clear
                self._neg_stash = self.negative.toPlainText()
                self._set_negative_text("")
            self.negative.setPlaceholderText(_NEG_UNSUPPORTED_MSG)
        self.negative.setEnabled(enabled)
        self.negative_label.setStyleSheet("" if enabled else "color: gray;")
        self.negative.setToolTip("" if enabled else _NEG_UNSUPPORTED_MSG)

    def _rebuild_params(self, values: Optional[dict] = None) -> None:
        for form in (self.params_form, self.output_form):
            while form.rowCount():
                form.removeRow(0)
        self._param_getters.clear()
        self._refresh_fps_label()
        model = self._current_model()
        if not model:
            self._schema = None
            return
        # Per-param schema (enums/types) comes from the live-schema cache the Model Library
        # tab populates ("Fetch live schemas"); None for local/never-fetched models.
        self._schema = schema_cache.get(model.get("replicate_model_id"))
        merged = dict(model.get("default_params", {}))
        if values:
            merged.update(values)
        merged.pop("aspect_ratio", None)   # owned by the Aspect dropdown, not the form
        # Settings tab: resolution + duration/length lead (output_form); the rest follow.
        for name, label in (("resolution", "Resolution"), ("duration", "Duration"),
                            ("length", "Duration")):
            if name in merged:
                widget, getter = self._make_output_widget(name, merged[name], model)
                self.output_form.addRow(label, widget)
                self._param_getters[name] = getter
                self._wire_dirty(widget)
        ordered = [k for k in _PARAM_ORDER if k in merged and k not in _OUTPUT_PARAMS]
        ordered += [k for k in merged if k not in ordered and k not in _OUTPUT_PARAMS]
        for name in ordered:
            widget, getter = self._make_param_widget(name, merged[name], model)
            self.params_form.addRow(name, widget)
            self._param_getters[name] = getter
            self._wire_dirty(widget)
        self._refresh_price()
        self._refresh_negative_state()

    def _refresh_fps_label(self) -> None:
        if self._is_local():
            self.fps_value.setText(f"{_WAN_FPS} fps (fixed)")
            return
        fps = self._measured_fps()   # hosted models don't declare fps - measure it off takes
        self.fps_value.setText(f"~ {fps:g} fps (measured)" if fps else "set by model")

    def _measured_fps(self) -> Optional[float]:
        """Most recent non-null fps among this shot's takes (PyAV measures it per take)."""
        if not self.shot:
            return None
        for take in reversed(self.project.list_takes(self.shot.id)):
            fps = getattr(take, "fps", None)
            if fps:
                return float(fps)
        return None

    def _refresh_price(self) -> None:
        model = self._current_model()
        cost = library.estimate_cost(model["id"], self._params()) if model else None
        if cost is None:
            self.price_value.setText("—")
        elif cost <= 0:
            self.price_value.setText("Free (local)")
        else:
            self.price_value.setText(f"~ ${cost:.2f}")

    def _make_output_widget(self, name, value, model):
        """Resolution / duration / length widgets for the Settings tab. Hosted duration is
        seconds (enum dropdown if the model has fixed options, else a bounded spin); local
        'length' is a 4n+1 frame count with a seconds hint."""
        schema_prop = (self._schema or {}).get(name, {})
        enum = schema_prop.get("enum")
        if name == "resolution":
            opts = enum or model.get("resolution_options") or [value]
            w = QComboBox(); w.addItems([str(o) for o in opts])
            if str(value) in [str(o) for o in opts]:
                w.setCurrentText(str(value))
            w.currentTextChanged.connect(lambda _t: self._on_aspect_changed())  # readout tracks resolution
            return w, lambda: w.currentText()
        if name == "length":                       # local Wan: frame count locked to 4n+1
            spin = QSpinBox(); spin.setRange(1, 997); spin.setSingleStep(4)
            hint = QLabel(""); hint.setStyleSheet("color: gray;")

            def update_hint() -> None:
                hint.setText(f"frames (4n+1) · ≈ {spin.value() / _WAN_FPS:.1f} s @ {_WAN_FPS} fps")

            def snap() -> None:
                s = max(1, round((spin.value() - 1) / 4) * 4 + 1)
                if s != spin.value():
                    spin.setValue(s)
                update_hint()

            spin.valueChanged.connect(lambda _v: update_hint())
            spin.editingFinished.connect(snap)
            spin.setValue(max(1, round((int(value) - 1) / 4) * 4 + 1))
            update_hint()
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(spin); row.addWidget(hint, 1)
            host = QWidget(); host.setLayout(row)
            return host, lambda: spin.value()
        # hosted duration (seconds): enum dropdown if available, else bounded spin
        opts = enum or model.get("duration_options")
        if opts:
            w = QComboBox(); w.addItems([f"{o} s" for o in opts])
            vals = [str(o) for o in opts]
            if str(value) in vals:
                w.setCurrentIndex(vals.index(str(value)))
            w.currentTextChanged.connect(lambda _t: self._refresh_price())
            return w, lambda: int(w.currentText().split()[0])
        spin = QSpinBox(); spin.setRange(1, 600); spin.setSuffix(" s")
        if model.get("duration_range"):
            lo, hi = model["duration_range"]; spin.setRange(int(lo), int(hi))
        spin.setValue(int(value))
        spin.valueChanged.connect(lambda _v: self._refresh_price())
        return spin, lambda: spin.value()

    def _make_param_widget(self, name, value, model):
        schema_prop = (self._schema or {}).get(name, {})
        enum = schema_prop.get("enum")
        if name == "seed":
            return self._make_seed_widget(value)
        if name == "resolution" and (enum or model.get("resolution_options")):
            opts = enum or model["resolution_options"]
            w = QComboBox(); w.addItems([str(o) for o in opts])
            if str(value) in [str(o) for o in opts]:
                w.setCurrentText(str(value))
            w.currentTextChanged.connect(lambda _t: self._on_aspect_changed())  # readout tracks resolution
            return w, lambda: w.currentText()
        if name == "mode":   # before the generic enum branch: mode drives Kling price and
            opts = enum or model.get("mode_options") or ["standard", "pro"]   # needs the refresh wiring
            w = QComboBox(); w.addItems([str(o) for o in opts])
            if str(value) in [str(o) for o in opts]:
                w.setCurrentText(str(value))
            w.currentTextChanged.connect(lambda _t: self._refresh_price())
            return w, lambda: w.currentText()
        if enum:                                  # generic enum: preserve an out-of-schema stored
            items = [str(o) for o in enum]        # value (renamed/removed by a refresh) instead of
            w = QComboBox(); w.addItems(items)    # silently snapping to enum[0]; flag it red to re-pick
            sval = str(value)
            if sval not in items:                 # keep the stale choice visible (mirrors _populate_aspects)
                w.addItem(sval)
            w.setCurrentText(sval)

            def _flag_enum(_t=None, _w=w, _items=items) -> None:
                _w.setStyleSheet("" if _w.currentText() in _items
                                 else "QComboBox { border: 2px solid #d9534f; }")
            _flag_enum()
            w.currentTextChanged.connect(_flag_enum)
            return w, lambda: w.currentText()
        if isinstance(value, bool):
            w = QCheckBox(); w.setChecked(value)
            return w, lambda: w.isChecked()
        if isinstance(value, int):
            w = QSpinBox(); w.setRange(-2147483648, 2147483647)
            if name == "duration" and model.get("duration_range"):
                lo, hi = model["duration_range"]; w.setRange(int(lo), int(hi))
            w.setValue(value)
            return w, lambda: w.value()
        if isinstance(value, float):
            w = QDoubleSpinBox(); w.setRange(-1e9, 1e9); w.setDecimals(3); w.setValue(value)
            return w, lambda: w.value()
        w = QLineEdit(str(value))
        return w, lambda: w.text()

    def _make_seed_widget(self, value):
        """Seed entry with a 'Random' toggle. Checked -> the shot stores SEED_RANDOM and
        every generated take (including future batch members) gets a fresh random seed;
        unchecked -> the fixed spin value. The concrete per-take seed is resolved at launch
        (library.resolve_seed) and recorded on the take, so a good result stays reproducible."""
        random_on = value == library.SEED_RANDOM
        spin = QSpinBox(); spin.setRange(0, 2147483647)
        spin.setValue(0 if random_on else int(value))
        chk = QCheckBox("Random")
        chk.setToolTip("Use a new random seed for every generation (each take in a batch differs)")
        chk.setChecked(random_on)
        spin.setEnabled(not random_on)
        chk.toggled.connect(spin.setDisabled)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(spin, 1); row.addWidget(chk)
        host = QWidget(); host.setLayout(row)
        return host, lambda: library.SEED_RANDOM if chk.isChecked() else spin.value()

    def _params(self) -> dict:
        return {name: getter() for name, getter in self._param_getters.items()}

    # ---- load / save ----------------------------------------------------
    def _load(self, shot) -> None:
        self.name.setText(shot.name)
        idx = self.model_combo.findData(shot.model_id)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.prompt.setPlainText(shot.prompt)
        self._set_negative(shot.negative_prompt)   # stash-aware (the model's box may be masked)
        self._rebuild_params(shot.settings)
        crop = shot.crop or {}
        for which, field in (("start", "start_frame"), ("end", "end_frame")):
            self._set_asset(which, getattr(shot, field))
            self._frames[which] = dict(crop.get(which) or _DEFAULT_PLACEMENT)
        self._populate_aspects(crop.get("aspect"))
        self._active = "start"
        self._select("start")
        self._ensure_takes_view()

    def is_blank_new(self) -> bool:
        """A never-saved tab the user hasn't touched - nothing worth persisting on a
        bulk File > Save (avoids manufacturing empty 'Unnamed Shot' rows)."""
        return (self.shot is None and not self.name.text().strip()
                and not self._assets["start"] and not self._assets["end"]
                and not self.prompt.toPlainText().strip())

    def _auto_name(self) -> str:
        """The classic 'Untitled' trick: first free 'Unnamed Shot N' in the project."""
        existing = {s.name for s in self.project.list_shots()}
        n = 1
        while f"Unnamed Shot {n}" in existing:
            n += 1
        return f"Unnamed Shot {n}"

    def commit(self) -> Optional[str]:
        """Flush this editor's state into the project's shot buffer (no file write) and
        return the shot id. A blank name is auto-filled 'Unnamed Shot N'. Used by the
        Save button and by File > Save, which flushes every open tab before writing."""
        name = self.name.text().strip()
        if not name:
            name = self._auto_name()
            self.name.setText(name)
        self._frames[self._active] = self.canvas.get_placement()   # capture the live one
        model = self._current_model()
        settings = self._params()
        aspect = self.selected_aspect()
        if "aspect_ratio" in (model.get("default_params") or {}):
            settings["aspect_ratio"] = aspect    # hosted models that take the param
        crop = {"aspect": aspect, "start": self._frames["start"], "end": self._frames["end"]}
        w, h = framing.canvas_size(aspect, local=self._is_local())

        fields = dict(model_id=model["id"], prompt=self.prompt.toPlainText().strip(),
                      negative_prompt=self._negative_value().strip(), settings=settings,
                      start_frame=self._assets["start"], end_frame=self._assets["end"],
                      canvas_w=w, canvas_h=h, crop=crop)
        if self.shot:
            sid = self.shot.id
            self.project.update_shot(sid, name=name, **fields)
        else:
            sid = self.project.add_shot(name, **fields).id

        self.shot = self.project.get_shot(sid)
        self._ensure_takes_view()
        self._update_action_state()
        self._clear_dirty()
        return sid

    def _save(self) -> Optional[str]:
        sid = self.commit()
        if sid:
            self.status_lbl.setText("saved")
            self.saved.emit(sid)
        return sid

    def _generate(self) -> None:
        if not self.aspect_valid():
            QMessageBox.warning(self, "Generate",
                                f"'{self.selected_aspect()}' isn't a valid aspect ratio for "
                                f"{self._current_model()['display_name']}. Pick one from the list.")
            return
        sid = self._save()
        if sid:
            self.generate_requested.emit(sid)

    def _export(self) -> None:
        if self.shot:
            self.export_requested.emit([t.id for t in self.project.list_takes(self.shot.id)])
