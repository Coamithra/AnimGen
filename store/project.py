"""File-based project document for AnimGen (replaces the old SQLite store).

A Project is a classic openable/saveable document:

    Fighter.animproj          JSON: {format, version, name, shots:[...]}  (authoring)
    Fighter.assets/           sidecar folder beside the file:
        takes.json            JSON: {version, takes:[...]}  (generated-take metadata)
        shot_stars.json       JSON: {version, starred:[shot_id,...]}  (write-through shot stars)
        keyposes/<shot_id>/   baked start.png / end.png
        takes/<take_id>.mp4   generated takes
        thumbs/<take_id>.png  take thumbnails
        .bin/<take_id>/       soft-deleted take media

Persistence is HYBRID (per the project design):
- Shot edits (authoring) buffer in memory and set `dirty`; they hit disk only on
  save()/save_as(). The window shows the dirty marker and prompts before discarding.
- Take changes WRITE THROUGH immediately to assets/takes.json, so a finished render is
  never lost - and because takes live in a separate file, persisting one never flushes
  buffered shot edits.
- Shot STARS are the one authoring field that also writes through (to assets/shot_stars.json),
  so a star/unstar persists instantly - same timing as a take star - without flushing the
  rest of the buffered shot edits or marking the project dirty.

Managed media (under assets_dir) serialize as paths RELATIVE to assets_dir so the
project is portable as a (file + .assets) pair; external references (e.g. a seeded
Fighter take in ../Fighter/out, or a browsed source keypose) stay ABSOLUTE and are
never copied - the tool is purely additive (gotcha #2). An untitled project keeps its
assets in a scratch dir until the first Save As, which relocates them.

Thread-safe like the old Store: one RLock guards mutation + persistence, so JobManager
worker threads can update takes off the GUI thread.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import paths
from store.models import STATUS_CANCELLED, STATUS_FAILED, Job, Shot, Take

FORMAT = "animgen-project"
VERSION = 1

# Dataclass path fields that may point into assets_dir (and so get relativized on save).
_SHOT_PATHS = ("start_frame", "end_frame")
_TAKE_PATHS = ("video_path", "preview_gif", "thumbnail")

# Reason-text markers (lowercase) that identify a take cancelled/failed by a crash or
# ComfyUI/app death (orphan recovery or the 3-strike abandon) rather than by the user. Used
# ONLY to backfill the `interrupted` flag on legacy takes written before the flag existed;
# new takes set it explicitly at the cancel/fail site (see backends/recovery.py, jobs.py).
_INTERRUPTED_REASON_MARKERS = (
    "before restart",          # orphan recovery CANCEL (pending take never submitted)
    "unreachable at restart",  # offline recovery FAIL (in-flight render lost, ComfyUI down)
    "lost to app restart",     # online recovery FAIL (in-flight render not found on server)
    "pausing the local queue", # 3-strike crash abandon
)

# Keyframe assets are image files kept flat in the .assets/ root.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _safe_name(name: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (name or "")).strip("_")
    return s or "asset"


def _under(path: Path, base: Path) -> bool:
    try:
        return base.resolve() in path.resolve().parents
    except OSError:
        return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{new_id()}.tmp")  # unique tmp: no concurrent clobber
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(5):                              # Windows: AV/indexer can hold a brief lock
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


class Project:
    """In-memory project document. Use Project.new() / Project.load()."""

    def __init__(self, path: Optional[Path], name: str, assets_dir: Path):
        self.path: Optional[Path] = path        # the .animproj file (None = untitled)
        self.name = name
        self._assets_dir = assets_dir
        self._lock = threading.RLock()
        self._shots: dict[str, Shot] = {}
        self._takes: dict[str, Take] = {}
        self._jobs: dict[str, Job] = {}          # in-memory only, never persisted
        self.dirty = False                       # unsaved *authoring* edits
        self.ui_state: dict = {}                 # per-project window layout (open tabs); UI-owned

    # ---- construction ---------------------------------------------------
    @classmethod
    def new(cls, name: str = "Untitled") -> "Project":
        scratch = paths.SCRATCH_DIR / new_id()
        return cls(path=None, name=name, assets_dir=scratch)

    @classmethod
    def load(cls, path: Path | str) -> "Project":
        path = Path(path)
        assets = cls._assets_for(path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        proj = cls(path=path, name=doc.get("name") or path.stem, assets_dir=assets)
        proj.ui_state = doc.get("ui_state") or {}   # restored open-tab layout (may be absent)
        for sd in doc.get("shots", []):
            shot = proj._shot_from_dict(sd)
            proj._shots[shot.id] = shot
        takes_file = assets / "takes.json"
        if takes_file.exists():
            tdoc = json.loads(takes_file.read_text(encoding="utf-8"))
            for td in tdoc.get("takes", []):
                take = proj._take_from_dict(td)
                if take.shot_id in proj._shots:      # drop orphans defensively
                    proj._takes[take.id] = take
        proj._load_shot_stars()
        proj.dirty = False
        proj._migrate_flatten_keyposes()
        return proj

    @staticmethod
    def _assets_for(path: Path) -> Path:
        return path.with_name(path.stem + ".assets")

    # ---- identity / paths ----------------------------------------------
    @property
    def is_untitled(self) -> bool:
        return self.path is None

    @property
    def assets_dir(self) -> Path:
        return self._assets_dir

    def _mediadir(self, *parts: str) -> Path:
        d = self._assets_dir.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def takes_dir(self) -> Path:
        return self._mediadir("takes")

    @property
    def thumbs_dir(self) -> Path:
        return self._mediadir("thumbs")

    @property
    def bin_dir(self) -> Path:
        return self._mediadir(".bin")

    # ---- assets (keyframe images, flat in the .assets/ root) ------------
    def list_assets(self) -> list[Path]:
        if not self._assets_dir.exists():
            return []
        return sorted(p for p in self._assets_dir.iterdir()
                      if p.is_file() and p.suffix.lower() in _IMAGE_EXTS)

    def import_asset(self, src: Path | str) -> Path:
        """Copy an image into the project's .assets/ root (leaving the original) and
        return the new path. Names are made filesystem-safe and collision-free."""
        src = Path(src)
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        stem, ext = _safe_name(src.stem), (src.suffix.lower() or ".png")
        dest = self._assets_dir / f"{stem}{ext}"
        n = 1
        while dest.exists():
            dest = self._assets_dir / f"{stem}_{n}{ext}"
            n += 1
        shutil.copy2(src, dest)
        return dest

    def remove_asset(self, path: Path | str) -> None:
        p = Path(path)
        if _under(p, self._assets_dir) and p.is_file():
            p.unlink(missing_ok=True)

    def _migrate_flatten_keyposes(self) -> None:
        """Old projects baked per-shot keyposes into .assets/keyposes/<shot_id>/. Re-point
        such shots to an imported copy of their original source (framing params already
        match the source, so gen-time framing stays correct), persist the re-point, then drop
        the keyposes tree. Best-effort + idempotent: shots already flat, or whose source is
        gone, are skipped.

        The re-point is persisted to disk BEFORE the sources are deleted. Marking dirty alone
        is unsafe: the destructive deletion would then happen while the only record of the
        re-point lives in memory, so a Discard at the next save-prompt would revert the
        re-point yet leave the sources already gone -- permanently stranding the shot's
        keyframes. Writing now makes the re-point durable and leaves the freshly-loaded
        project clean (no phantom '*' priming a misleading save-prompt). A persist failure
        can orphan the just-imported copies (a later reload re-imports) -- a harmless leak,
        never data loss, since the keypose sources are kept until a persist succeeds."""
        kp = self._assets_dir / "keyposes"
        if not kp.exists():
            return
        changed = False
        for shot in self._shots.values():
            crop = shot.crop or {}
            for field, src_key in (("start_frame", "source_start"), ("end_frame", "source_end")):
                val = getattr(shot, field)
                if not val or not _under(Path(val), kp):
                    continue
                source = crop.get(src_key)
                if source and Path(source).exists():
                    setattr(shot, field, str(self.import_asset(source)))
                    changed = True
        if not changed:
            return
        try:
            self._write_project_file()            # make the re-point durable before deleting
        except OSError:
            self.dirty = True                     # couldn't persist; keep sources, let Save retry
            return
        referenced = {Path(getattr(s, f)).resolve()
                      for s in self._shots.values() for f in _SHOT_PATHS if getattr(s, f)}
        for sub in list(kp.iterdir()):
            if sub.is_dir() and not any(r == sub or _under(r, sub) for r in referenced):
                shutil.rmtree(sub, ignore_errors=True)
        if not any(kp.iterdir()):
            kp.rmdir()

    # ---- save -----------------------------------------------------------
    def save(self) -> None:
        if self.path is None:
            raise ValueError("save() needs a path; call save_as() on an untitled project")
        self._write_project_file()
        self._write_takes_file()
        with self._lock:
            self.dirty = False

    def save_as(self, path: Path | str) -> None:
        path = Path(path)
        if path.suffix != ".animproj":
            path = path.with_suffix(".animproj")
        new_assets = self._assets_for(path)
        with self._lock:
            old_assets, old_path, old_name = self._assets_dir, self.path, self.name
            new_path_existed = path.exists()
            moved = copied = remapped = False
            displaced = None     # a pre-existing target sidecar, moved aside (not destroyed)
            # The identity swap + asset move must NOT outlive a failed document write: if
            # save() raises (e.g. _atomic_write_json exhausts its AV/indexer retries on
            # Windows), the in-memory project would otherwise claim the new identity with no
            # file on disk and, for an untitled project, its scratch already moved away
            # (gone, not copied) -> unrecoverable. So do everything inside a try and fully
            # roll back on any failure, leaving both this project and any clobbered neighbour
            # exactly as they were.
            try:
                if old_assets.exists() and old_assets.resolve() != new_assets.resolve():
                    if new_assets.exists():
                        # move the target's existing sidecar aside so a rollback can restore
                        # it; this also leaves a fresh dest for move/copytree below.
                        displaced = new_assets.with_name(f"{new_assets.name}.{new_id()}.bak")
                        shutil.move(str(new_assets), str(displaced))
                    # untitled (scratch) -> move; already-saved -> copy (keep the original)
                    if self.is_untitled:
                        new_assets.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_assets), str(new_assets))
                        moved = True
                    else:
                        shutil.copytree(old_assets, new_assets)
                        copied = True
                self._remap_paths(old_assets, new_assets)
                remapped = True
                self._assets_dir = new_assets
                self.path = path
                self.name = path.stem
                self.save()
            except Exception:
                self.path, self.name, self._assets_dir = old_path, old_name, old_assets
                if remapped:
                    self._remap_paths(new_assets, old_assets)        # reverse the path remap
                if moved:
                    shutil.move(str(new_assets), str(old_assets))    # restore the scratch
                elif copied:
                    shutil.rmtree(new_assets, ignore_errors=True)    # drop the copy
                if displaced is not None:
                    shutil.rmtree(new_assets, ignore_errors=True)    # ensure dest is clear
                    shutil.move(str(displaced), str(new_assets))     # restore the clobbered sidecar
                if not new_path_existed:
                    path.unlink(missing_ok=True)                     # drop a partial .animproj
                raise
            if displaced is not None:
                shutil.rmtree(displaced, ignore_errors=True)         # committed: drop the old sidecar

    def _remap_paths(self, old_assets: Path, new_assets: Path) -> None:
        """Rewrite in-memory absolute paths that lived under old_assets to new_assets."""
        def remap(val: Optional[str]) -> Optional[str]:
            if not val:
                return val
            try:
                rel = Path(val).relative_to(old_assets)
            except ValueError:
                return val                      # external path - leave it
            return str(new_assets / rel)
        for shot in self._shots.values():
            for f in _SHOT_PATHS:
                setattr(shot, f, remap(getattr(shot, f)))
        for take in self._takes.values():
            for f in _TAKE_PATHS:
                setattr(take, f, remap(getattr(take, f)))

    def persist_ui_state(self) -> None:
        """Write just the .animproj to record the current ui_state, without flushing
        buffered shot edits or touching takes.json. UI-owned window metadata, so it does
        not clear `dirty`. Callers must only invoke this when there are no uncommitted
        authoring edits (it serializes current in-memory shots, which equal disk only then).
        No-op on an untitled project (nowhere to write)."""
        if self.path is None:
            return
        self._write_project_file()

    def _write_project_file(self) -> None:
        assert self.path is not None
        # Hold the lock across build+write so concurrent take updates can't race os.replace.
        with self._lock:
            doc = {"format": FORMAT, "version": VERSION, "name": self.name,
                   "shots": [self._shot_to_dict(s) for s in self._ordered(self._shots)]}
            if self.ui_state:                       # additive: omit when empty (older files stay clean)
                doc["ui_state"] = self.ui_state
            _atomic_write_json(self.path, doc)

    def _write_takes_file(self) -> None:
        with self._lock:
            doc = {"version": VERSION,
                   "takes": [self._take_to_dict(t) for t in self._ordered(self._takes)]}
            _atomic_write_json(self._assets_dir / "takes.json", doc)

    @staticmethod
    def _ordered(d: dict) -> list:
        return sorted(d.values(), key=lambda o: o.created or "")

    # ---- (de)serialization ---------------------------------------------
    def _rel(self, val: Optional[str]) -> Optional[str]:
        if not val:
            return val
        try:
            return Path(val).relative_to(self._assets_dir).as_posix()
        except ValueError:
            return val                          # external - keep absolute

    def _abs(self, val: Optional[str]) -> Optional[str]:
        if not val:
            return val
        p = Path(val)
        return val if p.is_absolute() else str(self._assets_dir / p)

    def _shot_to_dict(self, s: Shot) -> dict:
        d = vars(s).copy()
        d.pop("starred", None)   # shot stars live in the write-through shot_stars.json sidecar
        for f in _SHOT_PATHS:
            d[f] = self._rel(d[f])
        return d

    def _shot_from_dict(self, d: dict) -> Shot:
        d = dict(d)
        for f in _SHOT_PATHS:
            d[f] = self._abs(d.get(f))
        return Shot(**d)

    def _take_to_dict(self, t: Take) -> dict:
        d = vars(t).copy()
        for f in _TAKE_PATHS:
            d[f] = self._rel(d[f])
        return d

    def _take_from_dict(self, d: dict) -> Take:
        d = dict(d)
        for f in _TAKE_PATHS:
            d[f] = self._abs(d.get(f))
        # Migration: pre-2026-06-18 cancelled/failed takes carry no `interrupted` flag. Backfill it
        # from the orphan-recovery reason markers in `error` so an existing crash-interrupted batch is
        # recognised (not user-cancelled / not a genuine failure) by the bulk Restart. Matches the
        # exact recovery phrases rather than a bare "restart" substring, so a "cannot restart: ..."
        # unrestartable mark is NOT misread as interrupted. New takes always serialize the field, so
        # this only runs for legacy files (once, then the value is persisted).
        if "interrupted" not in d and d.get("status") in (STATUS_CANCELLED, STATUS_FAILED):
            err = (d.get("error") or "").lower()
            d["interrupted"] = any(m in err for m in _INTERRUPTED_REASON_MARKERS)
        return Take(**d)

    # ---- shots (authoring; set dirty, no immediate persist) -------------
    def add_shot(self, name: str, **kw) -> Shot:
        shot = Shot(id=new_id(), name=name, created=_now(), updated=_now(), **kw)
        with self._lock:
            self._shots[shot.id] = shot
            self.dirty = True
        return shot

    def get_shot(self, shot_id: str) -> Optional[Shot]:
        return self._shots.get(shot_id)

    def list_shots(self) -> list[Shot]:
        return self._ordered(self._shots)

    def update_shot(self, shot_id: str, **fields) -> None:
        with self._lock:
            shot = self._shots.get(shot_id)
            if not shot:
                return
            for k, v in fields.items():
                setattr(shot, k, v)
            shot.updated = _now()
            self.dirty = True

    def duplicate_shot(self, shot_id: str) -> Optional[Shot]:
        """Copy a shot's authoring spec into a new shot (fresh id, name '<name> (copy)').
        Takes are NOT copied - a duplicate starts empty. Mutable dicts (crop/settings) are
        deep-copied so the copy is independent; asset paths are shared (we never duplicate
        asset files). Buffers like add_shot (sets dirty)."""
        with self._lock:
            src = self._shots.get(shot_id)
            if not src:
                return None
            dup = Shot(
                id=new_id(), name=f"{src.name} (copy)",
                start_frame=src.start_frame, end_frame=src.end_frame,
                canvas_w=src.canvas_w, canvas_h=src.canvas_h,
                crop=copy.deepcopy(src.crop), prompt=src.prompt,
                negative_prompt=src.negative_prompt, model_id=src.model_id,
                settings=copy.deepcopy(src.settings), created=_now(), updated=_now())
            self._shots[dup.id] = dup
            self.dirty = True
        return dup

    def delete_shot(self, shot_id: str) -> None:
        with self._lock:
            self._shots.pop(shot_id, None)
            gone = [tid for tid, t in self._takes.items() if t.shot_id == shot_id]
            for tid in gone:
                self._takes.pop(tid, None)
            self.dirty = True
        if gone:
            self._write_takes_file()

    def set_shot_starred(self, shot_id: str, starred: bool) -> None:
        """Star/unstar a shot. WRITE-THROUGH to the shot_stars.json sidecar (like a take's
        star, unlike other shot edits) so it persists instantly without flushing buffered
        authoring edits and without marking the project dirty."""
        with self._lock:
            shot = self._shots.get(shot_id)
            if not shot or bool(shot.starred) == bool(starred):
                return
            shot.starred = starred
        self._write_shot_stars_file()

    def _write_shot_stars_file(self) -> None:
        with self._lock:
            ids = [s.id for s in self._ordered(self._shots) if s.starred]
            doc = {"version": VERSION, "starred": ids}
            _atomic_write_json(self._assets_dir / "shot_stars.json", doc)

    def _load_shot_stars(self) -> None:
        """Apply shot stars from the write-through sidecar (authoritative when present).
        When it's absent - or present but unreadable - migrate any legacy `starred` flags
        carried in the .animproj into the sidecar (when there are any to save) so it becomes
        the source of truth going forward. The unreadable case must migrate too, not bail:
        _shot_to_dict strips `starred` from the .animproj, so leaving a corrupt sidecar in
        place would let the next ordinary Save silently drop the legacy stars for good (the
        in-memory flags are their only copy)."""
        sidecar = self._assets_dir / "shot_stars.json"
        starred = None
        if sidecar.exists():
            try:
                doc = json.loads(sidecar.read_text(encoding="utf-8"))
                starred = set(doc.get("starred", []))
            except (OSError, ValueError):
                starred = None                      # present but unreadable -> fall through to
                                                    # the migration branch below
        if starred is not None:
            for shot in self._shots.values():
                shot.starred = shot.id in starred
        elif any(s.starred for s in self._shots.values()):
            # Legacy .animproj stars OR an unreadable sidecar -> (re)materialize the sidecar
            # from the in-memory flags. Best-effort: a write hiccup here must never stop the
            # project from opening, so on failure the stars just stay live in memory for now.
            try:
                self._write_shot_stars_file()
            except OSError:
                pass

    def used_model_ids(self) -> list[str]:
        """Distinct model_ids across shots - powers the 'filter by model' dropdown."""
        seen = {s.model_id for s in self._shots.values() if s.model_id}
        return sorted(seen)

    # ---- takes (write-through to takes.json; do NOT set dirty) ----------
    def add_take(self, shot_id: str, **kw) -> Take:
        take = Take(id=new_id(), shot_id=shot_id, created=_now(), **kw)
        with self._lock:
            self._takes[take.id] = take
        self._write_takes_file()
        return take

    def get_take(self, take_id: str) -> Optional[Take]:
        return self._takes.get(take_id)

    def list_takes(self, shot_id: Optional[str] = None, *, include_deleted: bool = False,
                   starred_only: bool = False) -> list[Take]:
        out = []
        for t in self._ordered(self._takes):
            if shot_id is not None and t.shot_id != shot_id:
                continue
            if not include_deleted and t.deleted:
                continue
            if starred_only and not t.starred:
                continue
            out.append(t)
        return out

    def update_take(self, take_id: str, **fields) -> None:
        with self._lock:
            take = self._takes.get(take_id)
            if not take:
                return
            for k, v in fields.items():
                setattr(take, k, v)
        self._write_takes_file()

    def set_starred(self, take_id: str, starred: bool) -> None:
        self.update_take(take_id, starred=starred)

    def soft_delete_take(self, take_id: str) -> None:
        self.update_take(take_id, deleted=True)

    def restore_take(self, take_id: str) -> None:
        self.update_take(take_id, deleted=False)

    def purge_takes(self, take_ids) -> int:
        """Permanently remove takes - drop them from takes.json entirely (no bin, no restore).
        Best-effort deletes each take's MANAGED media (files under the assets dir); an external
        ref (e.g. a seeded ../Fighter/out take) is left exactly where it is - this stays purely
        additive toward anything the project doesn't own (gotcha #2). Returns the count removed.
        Unlike soft_delete_take this is irreversible. One takes.json write for the whole batch."""
        removed = 0
        with self._lock:
            for tid in list(take_ids):
                take = self._takes.pop(tid, None)
                if take is None:
                    continue
                removed += 1
                for field in _TAKE_PATHS:
                    val = getattr(take, field, None)
                    if not val:
                        continue
                    p = Path(val)
                    try:
                        if p.exists() and _under(p, self._assets_dir):
                            p.unlink()
                    except OSError:
                        pass    # best-effort; a locked managed file just lingers as orphan media
        if removed:
            self._write_takes_file()
        return removed

    # ---- jobs (in-memory only) -----------------------------------------
    def add_job(self, take_id: str, **kw) -> Job:
        job = Job(id=new_id(), take_id=take_id, created=_now(), updated=_now(), **kw)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def update_job(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated = _now()

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)
