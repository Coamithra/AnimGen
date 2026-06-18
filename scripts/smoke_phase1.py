"""Phase 1 smoke test: library load, project round-trip, offscreen GUI build.

Run headless with the animgen venv:
    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        animgen/.venv/Scripts/python.exe animgen/scripts/smoke_phase1.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # animgen/

import library  # noqa: E402
import paths  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/

from store.project import Project  # noqa: E402
from store.models import STATUS_DONE  # noqa: E402


def test_library() -> None:
    lib = library.load_library()
    assert lib["models"], "no models in library"
    seed = library.get_model("seedance-2.0-std")
    assert seed and seed["replicate_model_id"] == "bytedance/seedance-2.0"
    cost = library.estimate_cost("seedance-2.0-std", {"duration": 4})
    assert cost is not None and abs(cost - 0.72) < 1e-6, cost   # default res (720p)
    # per-resolution pricing: 480p is cheaper than 720p for the same duration
    c720 = library.estimate_cost("seedance-2.0-std", {"duration": 4, "resolution": "720p"})
    c480 = library.estimate_cost("seedance-2.0-std", {"duration": 4, "resolution": "480p"})
    assert c720 is not None and c480 is not None and c480 < c720, (c480, c720)
    # Wan prices by resolution, Kling by mode (cost_by)
    w720 = library.estimate_cost("wan-2.7-i2v", {"duration": 4, "resolution": "720p"})
    w1080 = library.estimate_cost("wan-2.7-i2v", {"duration": 4, "resolution": "1080p"})
    assert w720 is not None and w1080 is not None and w720 < w1080, (w720, w1080)
    kstd = library.estimate_cost("kling-3.0", {"duration": 5, "mode": "standard"})
    kpro = library.estimate_cost("kling-3.0", {"duration": 5, "mode": "pro"})
    assert kstd is not None and kpro is not None and kstd < kpro, (kstd, kpro)
    assert library.estimate_cost("local-flf-wan14b", {}) == 0.0
    assert library.default_negative_prompt().startswith("camera pan")
    print(f"library OK: {len(lib['models'])} models; seedance 4s est ${cost:.2f}")


def test_model_options() -> None:
    """Authored option lists match Replicate's live schema (verified 2026-06-16) and stay
    internally consistent (default in options, range ordered, default aspect allowed)."""
    # Vidu now exposes all three resolution tiers; 1080p must cost more than 540p.
    vidu = library.get_model("vidu-q3-pro")
    assert vidu["resolution_options"] == ["540p", "720p", "1080p"], vidu["resolution_options"]
    v540 = library.estimate_cost("vidu-q3-pro", {"duration": 4, "resolution": "540p"})
    v1080 = library.estimate_cost("vidu-q3-pro", {"duration": 4, "resolution": "1080p"})
    assert v540 is not None and v1080 is not None and v540 < v1080, (v540, v1080)

    # Kling takes no resolution param; mode is the quality axis and now includes 4k.
    kling = library.get_model("kling-3.0")
    assert "resolution_options" not in kling, "Kling has no resolution param"
    assert kling["mode_options"] == ["standard", "pro", "4k"], kling["mode_options"]
    k4k = library.estimate_cost("kling-3.0", {"duration": 5, "mode": "4k"})
    kpro = library.estimate_cost("kling-3.0", {"duration": 5, "mode": "pro"})
    assert k4k is not None and kpro is not None and kpro < k4k, (kpro, k4k)

    # Every Seedance model exposes Replicate's full aspect set (sans 'adaptive', no canvas).
    for mid in ("seedance-2.0-std", "seedance-2.0-fast", "seedance-1.0-pro", "seedance-1.0-lite"):
        sa = library.aspect_ratios(mid)
        assert set(sa) == {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "9:21"}, (mid, sa)
        assert "adaptive" not in sa

    # Duration maxima were widened to Replicate's live limits.
    for mid in ("seedance-2.0-std", "seedance-2.0-fast", "wan-2.7-i2v"):
        assert library.get_model(mid)["duration_range"][1] == 15, mid

    # Every model: defaults must be valid against their own option lists / ranges.
    for m in library.models():
        dp = m.get("default_params", {})
        ro = m.get("resolution_options")
        if ro and dp.get("resolution"):
            assert dp["resolution"] in ro, (m["id"], dp["resolution"], ro)
        mo = m.get("mode_options")
        if mo and dp.get("mode"):
            assert dp["mode"] in mo, (m["id"], dp["mode"], mo)
        dr = m.get("duration_range")
        if dr:
            lo, hi = dr
            assert lo <= hi, (m["id"], dr)
            if dp.get("duration") is not None:
                assert lo <= dp["duration"] <= hi, (m["id"], dp["duration"], dr)
        da = dp.get("aspect_ratio")
        if da:
            assert da in library.aspect_ratios(m["id"]), (m["id"], da)
    print("model options OK: vidu res tiers, kling modes (incl 4k), seedance aspects, defaults valid")


def test_project() -> None:
    p = Project.new()
    assert p.is_untitled and not p.dirty
    shot = p.add_shot(
        "kick_heavy", model_id="seedance-2.0-std", start_frame="assets/a.png",
        settings={"seed": 7, "duration": 4}, crop={"x": 1, "y": 2, "w": 3, "h": 4},
    )
    assert p.dirty, "add_shot should mark the project dirty"
    got = p.get_shot(shot.id)
    assert got.name == "kick_heavy" and got.settings["seed"] == 7 and got.crop["w"] == 3
    p.update_shot(shot.id, prompt="fierce kick", settings={"seed": 9, "duration": 5})
    got = p.get_shot(shot.id)
    assert got.prompt == "fierce kick" and got.settings["seed"] == 9
    assert not got.starred, "shots default to unstarred"
    p.dirty = False
    p.set_shot_starred(shot.id, True)
    assert p.get_shot(shot.id).starred, "shot is starred"
    assert not p.dirty, "starring a shot is write-through, must NOT mark the project dirty"
    assert (p.assets_dir / "shot_stars.json").exists(), "shot star must persist immediately"

    take = p.add_take(
        shot.id, status=STATUS_DONE, seed=7,
        settings_snapshot={"model_id": "seedance-2.0-std", "seed": 7, "prompt": "fierce kick"},
    )
    assert p.get_take(take.id).settings_snapshot["seed"] == 7
    p.set_starred(take.id, True)
    assert p.list_takes(shot.id, starred_only=True)[0].id == take.id
    p.soft_delete_take(take.id)
    assert p.list_takes(shot.id) == []
    assert len(p.list_takes(shot.id, include_deleted=True)) == 1
    p.restore_take(take.id)
    assert len(p.list_takes(shot.id)) == 1
    assert p.used_model_ids() == ["seedance-2.0-std"]

    job = p.add_job(take.id, backend="replicate", state="queued")
    p.update_job(job.id, state="running", ext_id="pred_abc")
    assert p.get_job(job.id).ext_id == "pred_abc"

    # save -> load round-trip
    proj_path = Path(tempfile.mkdtemp()) / "round.animproj"
    p.save_as(proj_path)
    assert not p.dirty and proj_path.exists()
    q = Project.load(proj_path)
    assert len(q.list_shots()) == 1 and q.list_shots()[0].prompt == "fierce kick"
    assert q.list_shots()[0].starred, "shot star must survive save/load"
    assert len(q.list_takes()) == 1 and q.list_takes()[0].starred
    print("project OK: shot+take+job round-trip, snapshot, star/delete/restore, save/load")


def test_shot_context_ops() -> None:
    """Duplicate copies the spec independently (no takes); the ShotCard right-click menu
    exposes Edit/Generate/Duplicate/Delete and fires the matching signals."""
    p = Project.new()
    src = p.add_shot("kick", model_id="seedance-2.0-std", prompt="hi",
                     settings={"seed": 7}, crop={"aspect": "16:9", "start": {"scale": 1.0}})
    p.add_take(src.id, status=STATUS_DONE)
    p.dirty = False

    dup = p.duplicate_shot(src.id)
    assert dup is not None and dup.id != src.id
    assert dup.name == "kick (copy)" and p.dirty
    assert dup.prompt == "hi" and dup.settings["seed"] == 7
    assert p.list_takes(dup.id) == [], "duplicate must start with no takes"
    dup.settings["seed"] = 99
    dup.crop["start"]["scale"] = 2.0
    assert p.get_shot(src.id).settings["seed"] == 7, "settings must be deep-copied"
    assert p.get_shot(src.id).crop["start"]["scale"] == 1.0, "crop must be deep-copied"
    assert p.duplicate_shot("nope") is None

    from PySide6.QtWidgets import QApplication

    from ui.shot_card import ShotCard

    app = QApplication.instance() or QApplication([])  # noqa: F841
    card = ShotCard(p, src)
    menu = card._build_context_menu()
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels == ["Edit", "Generate", "Duplicate", "Star shot", "Delete"], labels
    fired: dict = {}
    card.duplicate_requested.connect(lambda sid: fired.__setitem__("dup", sid))
    card.delete_requested.connect(lambda sid: fired.__setitem__("del", sid))
    card.open_requested.connect(lambda sid: fired.__setitem__("edit", sid))
    card.generate_requested.connect(lambda sid: fired.__setitem__("gen", sid))
    card.star_toggled.connect(lambda sid: fired.__setitem__("star", sid))
    by_text = {a.text(): a for a in menu.actions()}
    for label in ("Duplicate", "Delete", "Edit", "Generate", "Star shot"):
        by_text[label].trigger()
    assert fired == {"dup": src.id, "del": src.id, "edit": src.id,
                     "gen": src.id, "star": src.id}, fired

    # The header star button toggles glyph + emits, and the menu label flips once starred.
    assert card.star_btn.text() == "☆"
    p.set_shot_starred(src.id, True)
    card.shot = p.get_shot(src.id)
    card._refresh_star_btn()
    assert card.star_btn.text() == "★" and card.star_btn.isChecked()
    assert "Unstar shot" in [a.text() for a in card._build_context_menu().actions()]
    print("shot context ops OK: duplicate is independent + no takes; menu signals fire; star toggles")


def test_hybrid_persistence() -> None:
    """Shot edits buffer (dirty, not on disk); a finished take writes through at once."""
    proj_path = Path(tempfile.mkdtemp()) / "h.animproj"
    p = Project.new()
    s = p.add_shot("walk", model_id="local-flf-wan14b")
    p.save_as(proj_path)
    assert not p.dirty

    p.update_shot(s.id, name="walk_fwd")          # buffered authoring edit
    assert p.dirty
    p.add_take(s.id, status=STATUS_DONE)           # write-through to takes.json

    on_disk = json.loads(proj_path.read_text(encoding="utf-8"))
    assert on_disk["shots"][0]["name"] == "walk", "buffered shot edit must not be on disk"
    takes_doc = json.loads((p.assets_dir / "takes.json").read_text(encoding="utf-8"))
    assert len(takes_doc["takes"]) == 1, "finished take must auto-persist"
    print("hybrid persistence OK: shot edit buffered, take auto-persisted")


def test_shot_star_write_through() -> None:
    """Shot stars persist instantly to the shot_stars.json sidecar (not the .animproj), and
    a legacy .animproj that still carries `starred` migrates into the sidecar on load."""
    proj_path = Path(tempfile.mkdtemp()) / "stars.animproj"
    p = Project.new()
    a = p.add_shot("a", model_id="local-flf-wan14b")
    b = p.add_shot("b", model_id="local-flf-wan14b")
    p.save_as(proj_path)

    # Star is write-through (no dirty) and lands in the sidecar, NOT the .animproj doc.
    p.set_shot_starred(a.id, True)
    assert not p.dirty
    doc = json.loads(proj_path.read_text(encoding="utf-8"))
    assert all("starred" not in sd for sd in doc["shots"]), "stars must not be in the .animproj"
    side = json.loads((p.assets_dir / "shot_stars.json").read_text(encoding="utf-8"))
    assert side["starred"] == [a.id]

    # Reload reflects the sidecar; unstar removes it write-through.
    q = Project.load(proj_path)
    assert q.get_shot(a.id).starred and not q.get_shot(b.id).starred
    q.set_shot_starred(a.id, False)
    side = json.loads((q.assets_dir / "shot_stars.json").read_text(encoding="utf-8"))
    assert side["starred"] == []

    # Legacy migration: an .animproj with `starred:true` and NO sidecar seeds the sidecar.
    legacy_path = Path(tempfile.mkdtemp()) / "legacy.animproj"
    legacy_doc = {"format": "animgen-project", "version": 1, "name": "legacy",
                  "shots": [{"id": "s1", "name": "old", "starred": True, "crop": {},
                             "settings": {}, "created": "", "updated": ""}]}
    legacy_path.write_text(json.dumps(legacy_doc), encoding="utf-8")
    m = Project.load(legacy_path)
    assert m.get_shot("s1").starred, "legacy .animproj star must be read on load"
    migrated = json.loads((m.assets_dir / "shot_stars.json").read_text(encoding="utf-8"))
    assert migrated["starred"] == ["s1"], "legacy star must migrate into the sidecar"
    print("shot star write-through OK: sidecar persist, reload, unstar, legacy migration")


def test_keypose_migration_persist() -> None:
    """A legacy keypose-baked project re-points its shots to imported copies on load and
    PERSISTS that re-point immediately -- BEFORE deleting the keyposes source tree -- so a
    Discard at the next save-prompt (i.e. a no-save reload) can't strand a half-applied,
    source-deleted migration (card #56). The freshly-loaded project also stays clean (no
    phantom '*')."""
    tmp = Path(tempfile.mkdtemp())
    proj_path = tmp / "legacy.animproj"
    assets = tmp / "legacy.assets"
    # The baked keypose the legacy shot points at, plus the external original it was framed
    # from (kept outside the project, as a real seeded source would be).
    kp_dir = assets / "keyposes" / "s1"
    kp_dir.mkdir(parents=True)
    (kp_dir / "start.png").write_bytes(b"baked-keypose")
    source = tmp / "orig_start.png"
    source.write_bytes(b"original-source")

    legacy_doc = {"format": "animgen-project", "version": 1, "name": "legacy",
                  "shots": [{"id": "s1", "name": "kick", "model_id": "local-flf-wan14b",
                             "start_frame": "keyposes/s1/start.png", "end_frame": None,
                             "crop": {"source_start": str(source)},
                             "settings": {}, "created": "", "updated": ""}]}
    proj_path.write_text(json.dumps(legacy_doc), encoding="utf-8")

    p = Project.load(proj_path)
    s = p.get_shot("s1")
    # Re-pointed off the keyposes tree to an imported copy in the .assets/ root.
    assert s.start_frame and "keyposes" not in Path(s.start_frame).parts, s.start_frame
    assert Path(s.start_frame).exists(), "imported copy must exist"
    # Persisted immediately: the re-point is on disk, and the project is clean (no phantom *).
    assert not p.dirty, "a persisted migration must NOT leave the project dirty"
    on_disk = json.loads(proj_path.read_text(encoding="utf-8"))
    assert "keyposes" not in (on_disk["shots"][0]["start_frame"] or ""), \
        "the re-point must be written to the .animproj on load, not just held in memory"
    # The stale keyposes source tree is removed -- only after the durable persist.
    assert not (assets / "keyposes" / "s1").exists(), "stale keypose source should be removed"

    # Discard-equivalent: reload with no explicit save. The re-pointed frame must survive.
    q = Project.load(proj_path)
    qs = q.get_shot("s1")
    assert qs.start_frame and "keyposes" not in Path(qs.start_frame).parts
    assert Path(qs.start_frame).exists(), "no data loss after a discard-equivalent reload"
    print("keypose migration OK: re-point persisted before source deletion, clean reload")


def test_keypose_migration_persist_failure() -> None:
    """When persisting the re-point fails, the migration must NOT delete the keyposes sources
    -- it keeps them and falls back to dirty=True so a later Save (or reload) retries (card
    #56). This is the headline safety property: no source is deleted before the re-point is
    durable on disk."""
    tmp = Path(tempfile.mkdtemp())
    proj_path = tmp / "legacy.animproj"
    assets = tmp / "legacy.assets"
    kp_dir = assets / "keyposes" / "s1"
    kp_dir.mkdir(parents=True)
    (kp_dir / "start.png").write_bytes(b"baked-keypose")
    source = tmp / "orig_start.png"
    source.write_bytes(b"original-source")
    legacy_doc = {"format": "animgen-project", "version": 1, "name": "legacy",
                  "shots": [{"id": "s1", "name": "kick", "model_id": "local-flf-wan14b",
                             "start_frame": "keyposes/s1/start.png", "end_frame": None,
                             "crop": {"source_start": str(source)},
                             "settings": {}, "created": "", "updated": ""}]}
    proj_path.write_text(json.dumps(legacy_doc), encoding="utf-8")

    orig = Project._write_project_file
    Project._write_project_file = lambda self: (_ for _ in ()).throw(OSError("disk full"))
    try:
        p = Project.load(proj_path)
    finally:
        Project._write_project_file = orig

    assert p.dirty, "a failed persist must fall back to dirty=True"
    assert (assets / "keyposes" / "s1" / "start.png").exists(), \
        "the keypose source must NOT be deleted when the persist failed"
    on_disk = json.loads(proj_path.read_text(encoding="utf-8"))
    assert "keyposes" in (on_disk["shots"][0]["start_frame"] or ""), \
        "nothing should have been persisted on a failed migration write"

    # Recovery: with persist working again, a reload completes the migration with no loss.
    r = Project.load(proj_path)
    rs = r.get_shot("s1")
    assert rs.start_frame and "keyposes" not in Path(rs.start_frame).parts
    assert Path(rs.start_frame).exists() and not r.dirty
    print("keypose migration failure OK: sources kept + dirty on failed persist, reload recovers")


def test_gui_build() -> None:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    win = MainWindow(Project.new("Untitled"))
    win.show()
    app.processEvents()
    assert win.windowTitle().endswith("Animation Generator")
    # An untitled (never-saved) project carries the unsaved-changes marker.
    assert win._has_unsaved_changes() and win.windowTitle().startswith("Untitled*")
    print("GUI OK: MainWindow built + shown offscreen, untitled shows *")


if __name__ == "__main__":
    test_library()
    test_model_options()
    test_project()
    test_shot_context_ops()
    test_hybrid_persistence()
    test_shot_star_write_through()
    test_keypose_migration_persist()
    test_keypose_migration_persist_failure()
    test_gui_build()
    print("PHASE 1 SMOKE: PASS")
