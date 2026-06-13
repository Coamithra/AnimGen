"""Local (ComfyUI) generation backend.

Wraps scripts/run_workflow.py's submit/poll loop as importable functions that RAISE
(ComfyError) and report progress. prepare_workflow() clones a template and sets the
start/end keypose (LoadImage), prompt/negative (CLIPTextEncode) and seed nodes -
using an explicit node-role map from the model library when available, else a
heuristic (ascending node-id ordering). Keyposes are copied into ComfyUI's input/
dir (LoadImage reads from there). dry_run prepares the workflow WITHOUT submitting.
"""
from __future__ import annotations

import copy
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from paths import COMFY_DIR, COMFY_INPUT_DIR

COMFY_URL = "http://127.0.0.1:8188"
COMFY_OUTPUT_DIR = COMFY_DIR / "output"

ProgressCb = Optional[Callable[[str], None]]


class ComfyError(RuntimeError):
    pass


def _log(cb: ProgressCb, msg: str) -> None:
    if cb:
        cb(msg)


def _api(path: str, data=None, timeout: int = 30) -> dict:
    req = urllib.request.Request(COMFY_URL + path)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:  # type: ignore[name-defined]
        raise ComfyError(f"ComfyUI unreachable at {COMFY_URL} ({e}). Is it running?") from e


def copy_input_image(path: str | Path) -> str:
    """Copy a keypose into ComfyUI/input/ so LoadImage can read it. Returns basename."""
    path = Path(path)
    if not path.exists():
        raise ComfyError(f"Keypose not found: {path}")
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = COMFY_INPUT_DIR / path.name
    if not (dest.exists() and dest.stat().st_size == path.stat().st_size):
        shutil.copy2(path, dest)
    return path.name


def _nodes_by_class(wf: dict, class_type: str) -> list[str]:
    ids = [nid for nid, n in wf.items() if n.get("class_type") == class_type]
    return sorted(ids, key=lambda s: (len(s), s))  # numeric-ish ascending


def prepare_workflow(template: dict, *, start_img: Optional[str] = None,
                     end_img: Optional[str] = None, prompt: Optional[str] = None,
                     negative: Optional[str] = None, seed: Optional[int] = None,
                     node_roles: Optional[dict] = None,
                     sets: Optional[dict] = None) -> dict:
    """Return a mutated copy of `template` with our inputs applied.

    node_roles (from model_library 'comfy_nodes') may name: start_image, end_image,
    prompt, negative, seed_nodes[]. Missing roles fall back to heuristics.
    """
    wf = copy.deepcopy(template)
    roles = node_roles or {}

    loads = _nodes_by_class(wf, "LoadImage")
    clips = _nodes_by_class(wf, "CLIPTextEncode")

    def set_input(node_id: str, field: str, value) -> None:
        if node_id not in wf:
            raise ComfyError(f"node {node_id} not in workflow")
        wf[node_id].setdefault("inputs", {})[field] = value

    if start_img is not None:
        nid = roles.get("start_image") or (loads[0] if loads else None)
        if nid is None:
            raise ComfyError("no LoadImage node for start image")
        set_input(nid, "image", copy_input_image(start_img))
    if end_img is not None:
        nid = roles.get("end_image") or (loads[1] if len(loads) > 1 else None)
        if nid is None:
            raise ComfyError("no second LoadImage node for end image")
        set_input(nid, "image", copy_input_image(end_img))
    if prompt is not None:
        nid = roles.get("prompt") or (clips[0] if clips else None)
        if nid is None:
            raise ComfyError("no CLIPTextEncode node for prompt")
        set_input(nid, "text", prompt)
    if negative is not None:
        nid = roles.get("negative") or (clips[1] if len(clips) > 1 else None)
        if nid is not None:
            set_input(nid, "text", negative)

    if seed is not None:
        seed_nodes = roles.get("seed_nodes")
        if not seed_nodes:
            seed_nodes = [nid for nid, n in wf.items()
                          if {"seed", "noise_seed"} & set(n.get("inputs", {}))]
        if not seed_nodes:
            raise ComfyError("--seed given but no seed/noise_seed field in workflow")
        for nid in seed_nodes:
            for field in ("seed", "noise_seed"):
                if field in wf[nid].get("inputs", {}):
                    wf[nid]["inputs"][field] = seed

    for target, value in (sets or {}).items():
        node_id, field = target.split(".", 1)
        cur = wf[node_id]["inputs"].get(field)
        if isinstance(cur, bool):
            value = str(value).lower() in ("1", "true", "yes")
        elif isinstance(cur, int):
            value = int(value)
        elif isinstance(cur, float):
            value = float(value)
        wf[node_id]["inputs"][field] = value

    return wf


def submit(wf: dict, out_path: Path, progress_cb: ProgressCb = None,
           timeout_s: int = 3600, poll_s: int = 5) -> dict:
    res = _api("/prompt", {"prompt": wf})
    pid = res["prompt_id"]
    _log(progress_cb, f"queued {pid}")
    t0 = time.time()
    while True:
        time.sleep(poll_s)
        hist = _api(f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = status.get("messages", [])
                detail = next((json.dumps(m[1])[:1500] for m in msgs
                               if m[0] == "execution_error"), "")
                raise ComfyError(f"workflow error: {detail}")
            if status.get("completed") and not entry.get("outputs"):
                raise ComfyError("completed with NO outputs (full cache hit?) - "
                                 "change seed/prompt so something renders")
            if entry.get("outputs"):
                produced = []
                for out in entry["outputs"].values():
                    for key in ("images", "video", "videos", "gifs"):
                        for item in out.get(key, []):
                            sub = item.get("subfolder", "")
                            produced.append(COMFY_OUTPUT_DIR / sub / item["filename"])
                _log(progress_cb, f"done in {int(time.time() - t0)}s")
                if produced:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(produced[-1], out_path)
                return {"video_path": str(out_path), "produced": [str(p) for p in produced]}
        if time.time() - t0 > timeout_s:
            raise ComfyError("timed out after 1h")


def generate(template_path: str | Path, out_path: Path, *, start: Optional[str] = None,
             end: Optional[str] = None, prompt: Optional[str] = None,
             negative: Optional[str] = None, seed: Optional[int] = None,
             node_roles: Optional[dict] = None, sets: Optional[dict] = None,
             progress_cb: ProgressCb = None, dry_run: bool = False) -> dict:
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    wf = prepare_workflow(template, start_img=start, end_img=end, prompt=prompt,
                          negative=negative, seed=seed, node_roles=node_roles, sets=sets)
    if dry_run:
        return {"dry_run": True, "workflow": wf}
    return submit(wf, Path(out_path), progress_cb)
