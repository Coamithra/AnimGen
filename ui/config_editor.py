"""Config editor dialog - create or edit an animation configuration.

Pick a start frame (and optional end frame), frame them with the crop tool, write
the prompt + negative, choose a model, and fill its settings. The settings form is
auto-generated: fields are typed from the model's library metadata
(resolution_options, duration_range, default_params) and can be refined against the
live Replicate input schema via the Fetch-schema button. On save, the framed start
(and end) keyposes are baked to data/keyposes/<config_id>/ and stored as the config's
start_frame/end_frame; the framing parameters are kept under crop for re-editing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

import library
import paths
from backends import replicate_client
from pipeline import framing
from store.db import Store
from ui.crop_widget import CropWidget

_PARAM_ORDER = ["duration", "resolution", "seed", "aspect_ratio", "camera_fixed",
                "mode", "length"]


class ConfigEditor(QDialog):
    def __init__(self, store: Store, config=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.config = config           # existing Config or None
        self._schema: Optional[dict] = None
        self._param_getters: dict[str, Callable] = {}
        self.setWindowTitle("Edit config" if config else "New config")
        self.resize(1040, 760)
        self._build()
        if config:
            self._load(config)

    # ---- construction ---------------------------------------------------
    def _build(self) -> None:
        self.name = QLineEdit()
        self.model_combo = QComboBox()
        for m in library.models():
            self.model_combo.addItem(m["display_name"], m["id"])
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)

        # start / end frame source pickers
        self.start_src = QLineEdit(); self.start_src.setReadOnly(True)
        start_btn = QPushButton("Browse…"); start_btn.clicked.connect(self._pick_start)
        self.end_src = QLineEdit(); self.end_src.setReadOnly(True)
        end_btn = QPushButton("Browse…"); end_btn.clicked.connect(self._pick_end)
        clear_end = QPushButton("Clear"); clear_end.clicked.connect(lambda: self.end_src.clear())

        top = QFormLayout()
        top.addRow("Name", self.name)
        top.addRow("Model", self.model_combo)
        srow = QHBoxLayout(); srow.addWidget(self.start_src); srow.addWidget(start_btn)
        sw = QWidget(); sw.setLayout(srow); top.addRow("Start frame", sw)
        erow = QHBoxLayout(); erow.addWidget(self.end_src); erow.addWidget(end_btn); erow.addWidget(clear_end)
        ew = QWidget(); ew.setLayout(erow); top.addRow("End frame (opt)", ew)

        # crop tool (frames the START image)
        self.crop = CropWidget()

        # prompts
        self.prompt = QPlainTextEdit(); self.prompt.setPlaceholderText("Prompt…")
        self.negative = QPlainTextEdit(); self.negative.setPlaceholderText("Negative prompt…")
        self.negative.setPlainText(library.default_negative_prompt())
        prompt_box = QGroupBox("Prompt")
        pv = QVBoxLayout(prompt_box)
        pv.addWidget(QLabel("Positive")); pv.addWidget(self.prompt)
        pv.addWidget(QLabel("Negative")); pv.addWidget(self.negative)

        # model settings form
        self.params_box = QGroupBox("Model settings")
        self.params_form = QFormLayout(self.params_box)
        self.fetch_btn = QPushButton("Fetch live schema")
        self.fetch_btn.clicked.connect(self._fetch_schema)
        self.schema_status = QLabel("")

        # tabs: Framing | Prompt & settings
        tabs = QTabWidget()
        tabs.addTab(self.crop, "Framing")
        settings_tab = QWidget()
        sv = QVBoxLayout(settings_tab)
        sv.addWidget(prompt_box)
        sv.addWidget(self.params_box)
        frow = QHBoxLayout(); frow.addWidget(self.fetch_btn); frow.addWidget(self.schema_status); frow.addStretch(1)
        sv.addLayout(frow)
        tabs.addTab(settings_tab, "Prompt & settings")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(tabs, 1)
        lay.addWidget(bb)

        self._rebuild_params()

    # ---- model + params -------------------------------------------------
    def _current_model(self) -> dict:
        return library.get_model(self.model_combo.currentData())

    def _on_model_changed(self) -> None:
        self._schema = None
        self.schema_status.setText("")
        self.negative.setPlainText(self.negative.toPlainText() or library.default_negative_prompt())
        self._rebuild_params()

    def _rebuild_params(self, values: Optional[dict] = None) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self._param_getters.clear()
        model = self._current_model()
        if not model:
            return
        merged = dict(model.get("default_params", {}))
        if values:
            merged.update(values)
        ordered = [k for k in _PARAM_ORDER if k in merged]
        ordered += [k for k in merged if k not in ordered]
        for name in ordered:
            widget, getter = self._make_param_widget(name, merged[name], model)
            self.params_form.addRow(name, widget)
            self._param_getters[name] = getter
        self.fetch_btn.setEnabled(model["backend"] == "replicate")

    def _make_param_widget(self, name, value, model):
        schema_prop = (self._schema or {}).get(name, {})
        enum = schema_prop.get("enum")
        if name == "resolution" and (enum or model.get("resolution_options")):
            opts = enum or model["resolution_options"]
            w = QComboBox(); w.addItems([str(o) for o in opts])
            if str(value) in [str(o) for o in opts]:
                w.setCurrentText(str(value))
            return w, lambda: w.currentText()
        if enum:
            w = QComboBox(); w.addItems([str(o) for o in enum])
            w.setCurrentText(str(value))
            return w, lambda: w.currentText()
        if name == "aspect_ratio":
            opts = ["1:1", "16:9", "9:16", "auto"]
            if str(value) not in opts:
                opts.insert(0, str(value))
            w = QComboBox(); w.addItems(opts); w.setCurrentText(str(value))
            return w, lambda: w.currentText()
        if name == "mode":
            w = QComboBox(); w.addItems(["standard", "pro"]); w.setCurrentText(str(value))
            return w, lambda: w.currentText()
        if isinstance(value, bool):
            w = QCheckBox(); w.setChecked(value)
            return w, lambda: w.isChecked()
        if isinstance(value, int):
            w = QSpinBox(); w.setRange(-2147483648, 2147483647)
            if name == "duration" and model.get("duration_range"):
                lo, hi = model["duration_range"]; w.setRange(int(lo), int(hi))
            elif name == "seed":
                w.setRange(0, 2147483647)
            w.setValue(value)
            return w, lambda: w.value()
        if isinstance(value, float):
            w = QDoubleSpinBox(); w.setRange(-1e9, 1e9); w.setDecimals(3); w.setValue(value)
            return w, lambda: w.value()
        w = QLineEdit(str(value))
        return w, lambda: w.text()

    def _params(self) -> dict:
        return {name: getter() for name, getter in self._param_getters.items()}

    def _fetch_schema(self) -> None:
        model = self._current_model()
        rid = model.get("replicate_model_id")
        if not rid:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            props, _ = replicate_client.get_input_schema(replicate_client.load_token(), rid)
            self._schema = props
            self.schema_status.setText(f"schema: {len(props)} fields")
            self._rebuild_params(self._params())
        except Exception as e:  # noqa: BLE001
            self.schema_status.setText(f"fetch failed: {e}")
        finally:
            QGuiApplication.restoreOverrideCursor()

    # ---- source pickers -------------------------------------------------
    def _pick_start(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Start frame", str(paths.ASSETS_DIR),
            "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.start_src.setText(path)
            self.crop.set_source(path)

    def _pick_end(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "End frame", str(paths.ASSETS_DIR),
            "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.end_src.setText(path)

    # ---- load / save ----------------------------------------------------
    def _load(self, cfg) -> None:
        self.name.setText(cfg.name)
        idx = self.model_combo.findData(cfg.model_id)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.prompt.setPlainText(cfg.prompt)
        self.negative.setPlainText(cfg.negative_prompt)
        self._rebuild_params(cfg.settings)
        framing_meta = cfg.crop or {}
        start_source = framing_meta.get("source_start")
        if start_source and Path(start_source).exists():
            self.start_src.setText(start_source)
            self.crop.set_source(start_source)
            self._restore_framing(framing_meta)
        if framing_meta.get("source_end"):
            self.end_src.setText(framing_meta["source_end"])

    def _restore_framing(self, meta: dict) -> None:
        c = meta.get("crop")
        canvas = meta.get("canvas") or [1254, 1254]
        if c:
            self.crop._syncing = True
            self.crop.sx.setValue(c[0]); self.crop.sy.setValue(c[1])
            self.crop.sw.setValue(c[2]); self.crop.sh.setValue(c[3])
            self.crop._syncing = False
            self.crop._fields_to_rect()
        self.crop.cw.setValue(canvas[0]); self.crop.ch.setValue(canvas[1])
        if meta.get("char_height_frac"):
            self.crop.char_frac.setValue(meta["char_height_frac"])
        if meta.get("ground_y") is not None:
            self.crop.ground.setValue(meta["ground_y"])
        if meta.get("char_x") is not None:
            self.crop.char_x.setValue(meta["char_x"])

    def _save(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Save", "Name is required.")
            return
        model = self._current_model()
        settings = self._params()
        prompt = self.prompt.toPlainText().strip()
        negative = self.negative.toPlainText().strip()

        # create/update to get a stable id for baking keyposes
        if self.config:
            cid = self.config.id
            self.store.update_config(cid, name=name, model_id=model["id"],
                                     prompt=prompt, negative_prompt=negative,
                                     settings=settings)
        else:
            cid = self.store.add_config(name, model_id=model["id"], prompt=prompt,
                                        negative_prompt=negative, settings=settings).id

        framing_meta = self.crop.get_framing() if self.start_src.text() else {}
        start_path = end_path = None
        canvas = framing_meta.get("canvas") or [1254, 1254]
        try:
            if self.start_src.text():
                out = paths.DATA_DIR / "keyposes" / cid / "start.png"
                self.crop.bake(str(out))
                start_path = str(out)
                framing_meta["source_start"] = self.start_src.text()
            if self.end_src.text():
                out = paths.DATA_DIR / "keyposes" / cid / "end.png"
                framing.normalize_keypose(
                    self.end_src.text(), canvas=tuple(canvas),
                    char_height_frac=framing_meta.get("char_height_frac", 0.65),
                    ground_y=framing_meta.get("ground_y"),
                    char_x=framing_meta.get("char_x", 0.5), out_path=str(out))
                end_path = str(out)
                framing_meta["source_end"] = self.end_src.text()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Framing", f"Could not bake keypose:\n{e}")
            return

        self.store.update_config(
            cid, start_frame=start_path, end_frame=end_path,
            canvas_w=canvas[0], canvas_h=canvas[1], crop=framing_meta)
        self.saved_config_id = cid
        self.accept()
