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
from store.models import STATUS_CANCELLED, STATUS_DONE  # noqa: E402


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

    # Corrupt sidecar must NOT silently lose legacy stars (card #55): a legacy starred
    # .animproj beside an UNREADABLE shot_stars.json must keep the in-memory star AND
    # re-materialize the sidecar from it, so a later ordinary Save (which strips `starred`
    # from the .animproj) + reload still reports the shot as starred.
    corrupt_path = Path(tempfile.mkdtemp()) / "corrupt.animproj"
    corrupt_doc = {"format": "animgen-project", "version": 1, "name": "corrupt",
                   "shots": [{"id": "c1", "name": "old", "starred": True, "crop": {},
                              "settings": {}, "created": "", "updated": ""}]}
    corrupt_path.write_text(json.dumps(corrupt_doc), encoding="utf-8")
    corrupt_assets = corrupt_path.with_name(corrupt_path.stem + ".assets")
    corrupt_assets.mkdir(parents=True, exist_ok=True)
    (corrupt_assets / "shot_stars.json").write_text("{ not valid json", encoding="utf-8")
    c = Project.load(corrupt_path)
    assert c.get_shot("c1").starred, "legacy star must survive an unreadable sidecar on load"
    rebuilt = json.loads((corrupt_assets / "shot_stars.json").read_text(encoding="utf-8"))
    assert rebuilt["starred"] == ["c1"], "unreadable sidecar must be rebuilt from the legacy flag"
    c.update_shot("c1", prompt="edited")      # a normal authoring edit -> dirty -> next Save strips `starred`
    c.save()
    doc2 = json.loads(corrupt_path.read_text(encoding="utf-8"))
    assert all("starred" not in sd for sd in doc2["shots"]), "Save still strips starred from the .animproj"
    reloaded = Project.load(corrupt_path)
    assert reloaded.get_shot("c1").starred, "star must survive corrupt-sidecar + Save + reload"
    print("shot star write-through OK: sidecar persist, reload, unstar, legacy migration, corrupt-sidecar rescue")


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


def test_output_url_parsing() -> None:
    """run_prediction's output->url resolution must never AttributeError on an unexpected
    output shape: a list whose first element is neither str nor dict ([None], a nested
    list, a number, ...) falls through to a clean ReplicateError quoting the raw output,
    while well-formed shapes still resolve to the URL."""
    from backends.replicate_client import ReplicateError, _output_video_url

    # Well-formed shapes still resolve to the video URL.
    assert _output_video_url("https://x/v.mp4") == "https://x/v.mp4"
    assert _output_video_url(["https://x/a.mp4", "https://x/b.mp4"]) == "https://x/a.mp4"
    assert _output_video_url([{"url": "https://x/c.mp4"}]) == "https://x/c.mp4"
    assert _output_video_url({"url": "https://x/d.mp4"}) == "https://x/d.mp4"
    assert _output_video_url({"video": "https://x/e.mp4"}) == "https://x/e.mp4"

    # Bad shapes raise ReplicateError (NEVER AttributeError/TypeError) and quote the raw
    # output. The nested list [[...]] has a list (not str/dict) first element, and the
    # nested-value dicts have a non-str url/video, so all fall through to no usable URL.
    bad_outputs = ([None], [[{"url": "https://x/n.mp4"}]], [42], [True], [], {}, None, "",
                   {"foo": "bar"}, {"url": {"nested": "x"}}, {"video": ["https://x/v.mp4"]})
    for bad in bad_outputs:
        try:
            _output_video_url(bad)
        except ReplicateError as e:
            assert "No video URL in output" in str(e), str(e)
        except Exception as e:  # AttributeError (the bug) or anything else
            raise AssertionError(
                f"expected ReplicateError for {bad!r}, got {type(e).__name__}: {e}")
        else:
            raise AssertionError(f"expected ReplicateError for {bad!r}, got no exception")
    print("output url parsing OK: bad shapes -> ReplicateError (no AttributeError), good resolve")


def test_http_error_handling() -> None:
    """api_request humanizes known HTTP statuses and retries transient 5xx/429: a 504 on the
    create POST is no longer a bare 'HTTP 504' and recovers on retry; a 402 fails fast with a
    'out of credit' note. No real network (urlopen + sleep are stubbed)."""
    import io
    import urllib.error

    from backends import replicate_client as rc

    # Pure formatter: a recognised code gets a plain-English note + endpoint + raw body.
    msg = rc._http_error_message(504, "https://api.replicate.com/v1/models/vidu/q3-pro", "error code: 504")
    assert "HTTP 504" in msg and "gateway timeout" in msg.lower() and "vidu/q3-pro" in msg, msg
    assert "out of credit" in rc._http_error_message(402, "u", "")          # billing, not a timeout
    assert rc._http_error_message(418, "u", "").startswith("HTTP 418")      # unknown code still clean
    assert rc._retry_wait(429, '{"retry_after": 9}', 0) == 12               # honours retry_after + 3
    assert rc._retry_wait(504, "error code: 504", 2) == 12                  # 5xx escalating backoff

    class _Resp:                                                            # context-manager stand-in for urlopen
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    calls = {"n": 0}
    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:                                                 # first attempt: transient 504
            raise urllib.error.HTTPError(req.full_url, 504, "Gateway Timeout", {},
                                         io.BytesIO(b"error code: 504"))
        return _Resp()                                                      # retry succeeds

    saved_open, saved_sleep = rc.urllib.request.urlopen, rc.time.sleep
    rc.urllib.request.urlopen, rc.time.sleep = fake_urlopen, (lambda *_: None)
    try:
        out = rc.api_request("tok", "https://api.replicate.com/v1/x")
        assert out == {"ok": True} and calls["n"] == 2, (out, calls)       # retried once, then ok

        calls["n"] = 0                                                      # 402 is NOT retried
        def fail_402(req, timeout=0):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, io.BytesIO(b"{}"))
        rc.urllib.request.urlopen = fail_402
        try:
            rc.api_request("tok", "https://api.replicate.com/v1/x")
            raise AssertionError("expected ReplicateError on 402")
        except rc.ReplicateError as e:
            assert "HTTP 402" in str(e) and "out of credit" in str(e), str(e)
            assert calls["n"] == 1, calls                                   # failed fast, no retry
    finally:
        rc.urllib.request.urlopen, rc.time.sleep = saved_open, saved_sleep
    print("replicate_client OK: humanized HTTP errors + transient 504/429 retry, 402 fails fast")


def test_data_uri_fit() -> None:
    """For requires_data_uri models (vidu/q3-pro) an oversized input is auto-shrunk under the
    ~256KB base64 cap instead of being refused; a small image passes through untouched; the
    fitter flattens transparency onto magenta. No network (the data-URI path never uploads)."""
    import base64 as b64mod
    import io as iomod
    import tempfile
    from pathlib import Path

    import numpy as np
    from PIL import Image

    from backends import replicate_client as rc

    cap = rc.DATA_URI_B64_CAP
    tmp = Path(tempfile.mkdtemp())

    # A hard-to-compress noise image starts well over the cap...
    arr = np.random.default_rng(7).integers(0, 256, size=(525, 700, 3), dtype=np.uint8)
    big = tmp / "big.png"
    Image.fromarray(arr, "RGB").save(big)
    assert rc._b64_len(big.read_bytes()) > cap, "test image must start over the cap"

    uri = rc.upload_file("tok", big, as_data_uri=True)          # data-URI path: no network
    assert uri.startswith("data:image/png;base64,"), uri[:40]
    data = uri.split(",", 1)[1]
    assert len(data) <= cap, f"shrunk data-URI still over cap: {len(data)} > {cap}"
    Image.open(iomod.BytesIO(b64mod.b64decode(data))).load()     # still a decodable image

    # A small image is returned byte-for-byte (no needless re-encode).
    small = tmp / "small.png"
    Image.new("RGB", (32, 32), (255, 0, 255)).save(small)
    uri2 = rc.upload_file("tok", small, as_data_uri=True)
    assert b64mod.b64decode(uri2.split(",", 1)[1]) == small.read_bytes(), "small image must pass through"

    # The fitter flattens a transparent image onto magenta -> no alpha survives, fits the cap.
    buf = iomod.BytesIO(); Image.new("RGBA", (300, 300), (0, 255, 0, 0)).save(buf, format="PNG")
    out, mime = rc.fit_data_uri(buf.getvalue())
    assert mime == "image/png" and rc._b64_len(out) <= cap
    assert Image.open(iomod.BytesIO(out)).mode in ("RGB", "P")   # opaque, no transparency band
    print("replicate_client OK: data-URI auto-shrink (oversized fits, small passes, alpha flattened)")


def test_purge_takes() -> None:
    """Hard-delete: purge_takes drops takes from takes.json entirely (no bin/restore), deletes
    each take's MANAGED media (files under .assets) but leaves an EXTERNAL ref in place (gotcha
    #2), counts only what it actually removed, and persists in one write. A binned (soft-deleted)
    cancelled take is still cancelled, so it's purged too."""
    proj_path = Path(tempfile.mkdtemp()) / "purge.animproj"
    p = Project.new()
    shot = p.add_shot("kick", model_id="local-flf-wan14b")
    p.save_as(proj_path)

    done = p.add_take(shot.id, status=STATUS_DONE)
    managed = p.takes_dir / "c1.mp4"           # take media the project OWNS (under .assets)
    managed.write_bytes(b"managed")
    canc_managed = p.add_take(shot.id, status=STATUS_CANCELLED, video_path=str(managed))
    external = Path(tempfile.mkdtemp()) / "ext.mp4"   # an external ref (seeded-style)
    external.write_bytes(b"external")
    p.add_take(shot.id, status=STATUS_CANCELLED, video_path=str(external))
    # A binned cancelled take whose media was MOVED into <assets>/.bin/<id>/ (still cancelled ->
    # still purged); purge must delete the binned file AND remove the emptied per-take bin dir.
    from pipeline.takes_io import move_to_bin
    binned_media = p.takes_dir / "c2.mp4"
    binned_media.write_bytes(b"binned")
    binned = p.add_take(shot.id, status=STATUS_CANCELLED, video_path=str(binned_media))
    move_to_bin(p.get_take(binned.id), p)
    bin_take_dir = p.bin_dir / binned.id
    assert any(bin_take_dir.iterdir()), "media should have moved into the per-take bin dir"

    cancelled = [t for t in p.list_takes(include_deleted=True) if t.status == STATUS_CANCELLED]
    assert len(cancelled) == 3, len(cancelled)
    assert p.purge_takes(t.id for t in cancelled) == 3

    # Gone from memory and disk; the done take survives.
    assert [t.id for t in p.list_takes(include_deleted=True)] == [done.id]
    assert p.get_take(canc_managed.id) is None
    on_disk = json.loads((p.assets_dir / "takes.json").read_text(encoding="utf-8"))
    assert [t["id"] for t in on_disk["takes"]] == [done.id], "only the done take remains on disk"

    # Managed media deleted (incl. the binned copy + its emptied bin dir); external ref untouched.
    assert not managed.exists(), "managed media under .assets must be deleted"
    assert not bin_take_dir.exists(), "emptied per-take bin dir must be removed"
    assert external.exists(), "external media ref must NOT be touched (gotcha #2)"

    # No-op safety: purging unknown / already-gone ids removes nothing and skips the write.
    assert p.purge_takes([canc_managed.id, "nope"]) == 0
    print("purge takes OK: hard-delete drops records + managed media, keeps external, persists")


def test_remove_cancelled_takes_action() -> None:
    """The Edit menu exposes 'Remove cancelled takes'; its enabled state tracks whether the
    project holds any cancelled take, and the purge clears them. (The handler's confirm dialog
    is modal, so we drive the purge directly here -- never call .exec() headless.)"""
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    p = Project.new("Untitled")
    shot = p.add_shot("kick", model_id="local-flf-wan14b")
    win = MainWindow(p)
    assert win.purge_cancelled_act.text() == "Remove cancelled takes"
    assert not win.purge_cancelled_act.isEnabled(), "disabled with no cancelled takes"

    p.add_take(shot.id, status=STATUS_CANCELLED)
    win.reload()
    assert win._cancelled_take_count() == 1
    assert win.purge_cancelled_act.isEnabled(), "enabled once a cancelled take exists"

    win.project.purge_takes(t.id for t in win.project.list_takes(include_deleted=True)
                            if t.status == STATUS_CANCELLED)
    win.reload()
    assert win._cancelled_take_count() == 0 and not win.purge_cancelled_act.isEnabled()
    print("Edit menu OK: Remove cancelled takes enables on cancelled takes + purges them")


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
    test_output_url_parsing()
    test_http_error_handling()
    test_data_uri_fit()
    test_purge_takes()
    test_remove_cancelled_takes_action()
    test_gui_build()
    print("PHASE 1 SMOKE: PASS")
