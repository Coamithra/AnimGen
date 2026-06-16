"""Phase 6 smoke test (offscreen, no spend).

Seeds the real shipped-move manifest into a temp project (idempotent), and builds the
model library window. Does NOT touch the live Fighter.animproj or any project assets.

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase6.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import paths  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/

from store.project import Project  # noqa: E402


def test_seed() -> None:
    from scripts.seed_configs import seed

    project = Project.new()
    out_dir = paths.FIGHTER_OUT
    previews = out_dir / "retime_previews"
    added = seed(project, paths.GAME_SPRITES_MANIFEST, out_dir, previews)
    assert added >= 25, f"expected ~31 moves, seeded {added}"
    shots = project.list_shots()
    assert len(shots) == added
    # every seeded shot has exactly one starred 'done' take, snapshot tied to the move
    for s in shots:
        ts = project.list_takes(s.id)
        assert len(ts) == 1 and ts[0].starred and ts[0].status == "done"
        assert ts[0].settings_snapshot.get("move") == s.name
    # models were derived across backends
    models = {s.model_id for s in shots}
    assert "seedance-2.0-std" in models
    # keyposes resolved from the manifest frame sequence
    with_keyposes = sum(1 for s in shots if s.start_frame and s.end_frame)
    assert with_keyposes == added, f"{with_keyposes}/{added} shots have both keyposes"
    # keyframes were IMPORTED into the project's .assets (flat, no hash folders)
    assert len(project.list_assets()) >= added, "keyframes imported as assets"
    assert not (project.assets_dir / "keyposes").exists()
    for s in shots:
        for f in (s.start_frame, s.end_frame):
            if f:
                assert Path(f).parent == project.assets_dir, f"{f} not flat in .assets"
    # idempotent: a second seed adds nothing (no new shots, no new asset copies)
    n_assets = len(project.list_assets())
    again = seed(project, paths.GAME_SPRITES_MANIFEST, out_dir, previews)
    assert again == 0 and len(project.list_shots()) == added
    assert len(project.list_assets()) == n_assets, "re-seed must not duplicate assets"
    # at least some takes/previews resolved to real files
    with_video = sum(1 for s in shots for t in project.list_takes(s.id) if t.video_path)
    with_thumb = sum(1 for s in shots for t in project.list_takes(s.id) if t.thumbnail)

    # save -> load round-trip preserves shots + takes
    proj_path = Path(tempfile.mkdtemp()) / "Seeded.animproj"
    project.save_as(proj_path)
    reloaded = Project.load(proj_path)
    assert len(reloaded.list_shots()) == added and len(reloaded.list_takes()) == added

    print(f"seed OK: {added} shots, all starred/done, {with_keyposes} with keyposes; "
          f"{with_video} with video, {with_thumb} with preview thumb; idempotent + round-trip")


def test_schema_cache() -> None:
    """Round-trip the live-schema cache the Model Library populates / the shot editor reads."""
    from store import schema_cache

    paths.SCHEMA_CACHE = Path(tempfile.mkdtemp()) / "schema_cache.json"
    assert schema_cache.get("nobody/none") is None              # missing file -> empty
    props = {"resolution": {"enum": ["480p", "720p"]}, "seed": {"type": "integer"}}
    rec = schema_cache.put("acme/model", props)
    assert rec["fields"] == 2 and rec["fetched"] > 0
    assert schema_cache.get("acme/model") == props              # persisted + reread
    assert schema_cache.entry("acme/model")["fields"] == 2
    assert schema_cache.get(None) is None                       # local models have no rid
    print("schema_cache OK: put/get/entry round-trip, missing-key tolerant")


def test_prompt_library() -> None:
    """Round-trip the app-global prompt-template store (seed + save/get/delete)."""
    from store import prompt_library

    paths.PROMPT_TEMPLATES = Path(tempfile.mkdtemp()) / "prompt_templates.json"
    seeds = prompt_library.all_templates()                        # missing file -> seeds
    names = [t["name"] for t in seeds]
    assert "Camera-locked action" in names, f"expected seeded templates, got {names}"
    # Curated set pre-filled from Fighter PROMPTS.txt sections 1-3: a hosted move, a local
    # FLF transition, and a PD-unique move should all be present.
    for expected in ("Punch combo (light + heavy)", "Stand -> Crouch", "Dagger slash"):
        assert expected in names, f"missing curated seed {expected!r}; got {names}"
    assert len(seeds) == 31, f"expected the 31-template curated seed set, got {len(seeds)}"
    assert len(names) == len(set(names)), f"duplicate seed names: {names}"
    # Local templates carry the shared local style tail; hosted ones do not.
    crouch = prompt_library.get("Stand -> Crouch")
    assert crouch and crouch["positive"].endswith(prompt_library._LOCAL_TAIL), crouch
    assert crouch["negative"] and "still image" in crouch["negative"], crouch
    prompt_library.save("My move", "spinning kick", "blurry")
    got = prompt_library.get("My move")
    assert got and got["positive"] == "spinning kick" and got["negative"] == "blurry"
    prompt_library.save("My move", "updated", "")                 # save is upsert by name
    assert prompt_library.get("My move")["positive"] == "updated"
    assert sum(t["name"] == "My move" for t in prompt_library.all_templates()) == 1
    assert prompt_library.delete("My move") is True
    assert prompt_library.get("My move") is None
    assert prompt_library.delete("My move") is False             # already gone
    print("prompt_library OK: seeds, upsert-by-name, get/delete round-trip")


def test_library_window() -> None:
    from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget

    import library
    from ui.model_library_window import _COLUMNS, ModelLibraryWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    win = ModelLibraryWindow()
    table = win.findChild(QTableWidget)
    assert table is not None and table.rowCount() == len(library.models())
    assert table.columnCount() == len(_COLUMNS) and "Schema" in _COLUMNS
    assert "Capabilities" in _COLUMNS
    # the refresh control exists; Schema column reflects per-backend state (no fetch yet)
    assert any(isinstance(b, QPushButton) and b.text() == "Refresh from Replicate"
               for b in win.findChildren(QPushButton))
    schema_col = _COLUMNS.index("Schema")
    cells = {table.item(r, schema_col).text() for r in range(table.rowCount())}
    assert cells <= {"n/a", "not fetched"}, f"unexpected Schema cells: {cells}"
    # Capabilities column renders the synced flags (authored values pre-seeded in the roster):
    # veo-3.1-fast supports negative, seedance-1.0-pro supports fixed camera.
    caps_col = _COLUMNS.index("Capabilities")
    caps_by_model = {library.models()[r]["id"]: table.item(r, caps_col).text()
                     for r in range(table.rowCount())}
    assert "negative" in caps_by_model["veo-3.1-fast"], caps_by_model["veo-3.1-fast"]
    assert "camera-fixed" in caps_by_model["seedance-1.0-pro"], caps_by_model["seedance-1.0-pro"]
    print(f"ModelLibraryWindow OK: {table.rowCount()} model rows, Schema + Capabilities columns")


if __name__ == "__main__":
    test_seed()
    test_schema_cache()
    test_prompt_library()
    test_library_window()
    print("PHASE 6 SMOKE: PASS")
