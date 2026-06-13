"""SQLite-backed store for animgen (configs / results / jobs).

Thread-safe enough for the Qt app: one connection opened with
check_same_thread=False, every write guarded by an RLock, WAL mode for concurrent
reads. Structured fields live in JSON-blob columns (crop_json, settings_json,
settings_snapshot_json); rows convert to/from the dataclasses in store.models.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from store.models import Config, Job, Result


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS configs (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    start_frame     TEXT,
    end_frame       TEXT,
    canvas_w        INTEGER,
    canvas_h        INTEGER,
    crop_json       TEXT,
    prompt          TEXT,
    negative_prompt TEXT,
    model_id        TEXT,
    settings_json   TEXT,
    created         TEXT,
    updated         TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id                      TEXT PRIMARY KEY,
    config_id               TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending',
    video_path              TEXT,
    preview_gif             TEXT,
    thumbnail               TEXT,
    settings_snapshot_json  TEXT,
    seed                    INTEGER,
    cost_estimate           REAL,
    cost_actual             REAL,
    fps                     REAL,
    frame_count             INTEGER,
    starred                 INTEGER NOT NULL DEFAULT 0,
    deleted                 INTEGER NOT NULL DEFAULT 0,
    error                   TEXT,
    created                 TEXT,
    completed               TEXT,
    FOREIGN KEY (config_id) REFERENCES configs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    result_id   TEXT NOT NULL,
    backend     TEXT,
    state       TEXT,
    log         TEXT,
    ext_id      TEXT,
    created     TEXT,
    updated     TEXT,
    FOREIGN KEY (result_id) REFERENCES results(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_results_config ON results(config_id);
CREATE INDEX IF NOT EXISTS idx_jobs_result ON jobs(result_id);
"""


def _row_to_config(r: sqlite3.Row) -> Config:
    return Config(
        id=r["id"], name=r["name"], start_frame=r["start_frame"],
        end_frame=r["end_frame"], canvas_w=r["canvas_w"], canvas_h=r["canvas_h"],
        crop=json.loads(r["crop_json"] or "{}"), prompt=r["prompt"] or "",
        negative_prompt=r["negative_prompt"] or "", model_id=r["model_id"] or "",
        settings=json.loads(r["settings_json"] or "{}"),
        created=r["created"] or "", updated=r["updated"] or "",
    )


def _row_to_result(r: sqlite3.Row) -> Result:
    return Result(
        id=r["id"], config_id=r["config_id"], status=r["status"],
        video_path=r["video_path"], preview_gif=r["preview_gif"],
        thumbnail=r["thumbnail"],
        settings_snapshot=json.loads(r["settings_snapshot_json"] or "{}"),
        seed=r["seed"], cost_estimate=r["cost_estimate"],
        cost_actual=r["cost_actual"], fps=r["fps"], frame_count=r["frame_count"],
        starred=bool(r["starred"]), deleted=bool(r["deleted"]), error=r["error"],
        created=r["created"] or "", completed=r["completed"],
    )


def _row_to_job(r: sqlite3.Row) -> Job:
    return Job(
        id=r["id"], result_id=r["result_id"], backend=r["backend"] or "",
        state=r["state"] or "", log=r["log"] or "", ext_id=r["ext_id"],
        created=r["created"] or "", updated=r["updated"] or "",
    )


# Dataclass field name -> (column name, serializer) for the JSON / bool columns.
_CONFIG_MAP = {"crop": ("crop_json", json.dumps), "settings": ("settings_json", json.dumps)}
_RESULT_MAP = {
    "settings_snapshot": ("settings_snapshot_json", json.dumps),
    "starred": ("starred", int), "deleted": ("deleted", int),
}


class Store:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- configs --------------------------------------------------------
    def add_config(self, name: str, **kw) -> Config:
        cfg = Config(id=new_id(), name=name, created=_now(), updated=_now(), **kw)
        with self._lock:
            self._conn.execute(
                "INSERT INTO configs(id,name,start_frame,end_frame,canvas_w,canvas_h,"
                "crop_json,prompt,negative_prompt,model_id,settings_json,created,updated)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cfg.id, cfg.name, cfg.start_frame, cfg.end_frame, cfg.canvas_w,
                 cfg.canvas_h, json.dumps(cfg.crop), cfg.prompt, cfg.negative_prompt,
                 cfg.model_id, json.dumps(cfg.settings), cfg.created, cfg.updated),
            )
            self._conn.commit()
        return cfg

    def get_config(self, config_id: str) -> Optional[Config]:
        r = self._conn.execute("SELECT * FROM configs WHERE id=?", (config_id,)).fetchone()
        return _row_to_config(r) if r else None

    def list_configs(self) -> list[Config]:
        rows = self._conn.execute("SELECT * FROM configs ORDER BY created").fetchall()
        return [_row_to_config(r) for r in rows]

    def update_config(self, config_id: str, **fields) -> None:
        cols, vals = self._build_update(fields, _CONFIG_MAP)
        cols.append("updated=?"); vals.append(_now())
        vals.append(config_id)
        with self._lock:
            self._conn.execute(f"UPDATE configs SET {','.join(cols)} WHERE id=?", vals)
            self._conn.commit()

    def delete_config(self, config_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM configs WHERE id=?", (config_id,))
            self._conn.commit()

    # ---- results --------------------------------------------------------
    def add_result(self, config_id: str, **kw) -> Result:
        res = Result(id=new_id(), config_id=config_id, created=_now(), **kw)
        with self._lock:
            self._conn.execute(
                "INSERT INTO results(id,config_id,status,video_path,preview_gif,"
                "thumbnail,settings_snapshot_json,seed,cost_estimate,cost_actual,fps,"
                "frame_count,starred,deleted,error,created,completed)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (res.id, res.config_id, res.status, res.video_path, res.preview_gif,
                 res.thumbnail, json.dumps(res.settings_snapshot), res.seed,
                 res.cost_estimate, res.cost_actual, res.fps, res.frame_count,
                 int(res.starred), int(res.deleted), res.error, res.created,
                 res.completed),
            )
            self._conn.commit()
        return res

    def get_result(self, result_id: str) -> Optional[Result]:
        r = self._conn.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
        return _row_to_result(r) if r else None

    def list_results(self, config_id: Optional[str] = None, *, include_deleted: bool = False,
                     starred_only: bool = False) -> list[Result]:
        where, vals = [], []
        if config_id is not None:
            where.append("config_id=?"); vals.append(config_id)
        if not include_deleted:
            where.append("deleted=0")
        if starred_only:
            where.append("starred=1")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._conn.execute(
            f"SELECT * FROM results{clause} ORDER BY created", vals).fetchall()
        return [_row_to_result(r) for r in rows]

    def update_result(self, result_id: str, **fields) -> None:
        cols, vals = self._build_update(fields, _RESULT_MAP)
        vals.append(result_id)
        with self._lock:
            self._conn.execute(f"UPDATE results SET {','.join(cols)} WHERE id=?", vals)
            self._conn.commit()

    def set_starred(self, result_id: str, starred: bool) -> None:
        self.update_result(result_id, starred=starred)

    def soft_delete_result(self, result_id: str) -> None:
        self.update_result(result_id, deleted=True)

    def restore_result(self, result_id: str) -> None:
        self.update_result(result_id, deleted=False)

    def used_model_ids(self) -> list[str]:
        """Distinct model_ids across configs - powers the 'filter by model' dropdown."""
        rows = self._conn.execute(
            "SELECT DISTINCT model_id FROM configs WHERE model_id IS NOT NULL "
            "AND model_id <> '' ORDER BY model_id").fetchall()
        return [r["model_id"] for r in rows]

    # ---- jobs -----------------------------------------------------------
    def add_job(self, result_id: str, **kw) -> Job:
        job = Job(id=new_id(), result_id=result_id, created=_now(), updated=_now(), **kw)
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(id,result_id,backend,state,log,ext_id,created,updated)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (job.id, job.result_id, job.backend, job.state, job.log, job.ext_id,
                 job.created, job.updated),
            )
            self._conn.commit()
        return job

    def update_job(self, job_id: str, **fields) -> None:
        cols, vals = self._build_update(fields, {})
        cols.append("updated=?"); vals.append(_now())
        vals.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {','.join(cols)} WHERE id=?", vals)
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[Job]:
        r = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(r) if r else None

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _build_update(fields: dict, field_map: dict) -> tuple[list, list]:
        cols, vals = [], []
        for k, v in fields.items():
            if k in field_map:
                col, conv = field_map[k]
                cols.append(f"{col}=?"); vals.append(conv(v))
            else:
                cols.append(f"{k}=?"); vals.append(v)
        return cols, vals
