"""Hosted (Replicate) generation backend.

A refactor of scripts/run_replicate.py's core into importable functions that RAISE
(ReplicateError) instead of sys.exit, take explicit params instead of an argparse
namespace, and accept a progress callback. Field names differ per model, so we fetch
the model's live input schema and map canonical inputs onto whatever it calls them
(same ALIASES as the CLI). dry_run builds + returns the resolved input WITHOUT
uploading or creating a prediction (no network spend), for cost-gate previews.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

from paths import ENV_FILE, FIGHTER_ENV_FILE

API = "https://api.replicate.com/v1"
_UA = "fighter-animgen/1.0 (python-urllib)"

# Canonical concept -> known field-name aliases across Replicate video models.
ALIASES = {
    "start": ["start_image", "image", "first_frame_image", "image_url",
              "start_image_url", "first_frame_url", "first_frame", "input_image"],
    "end": ["end_image", "last_frame_image", "end_image_url", "last_image",
            "tail_image_url", "last_frame_url", "last_frame", "end_frame_image"],
    "prompt": ["prompt"],
    "negative": ["negative_prompt"],
    "duration": ["duration", "duration_seconds", "video_length"],
    "resolution": ["resolution"],
    "seed": ["seed"],
}

ProgressCb = Optional[Callable[[str], None]]


class ReplicateError(RuntimeError):
    pass


def _log(cb: ProgressCb, msg: str) -> None:
    if cb:
        cb(msg)


def load_token() -> str:
    import os
    tok = os.environ.get("REPLICATE_TOKEN") or os.environ.get("REPLICATE_API_TOKEN")
    if tok:
        return tok.strip()
    # repo-local .env first, then the source project's .env (kept outside this repo)
    for env_file in (ENV_FILE, FIGHTER_ENV_FILE):
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith(("REPLICATE_TOKEN=", "REPLICATE_API_TOKEN=")):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise ReplicateError("No REPLICATE_TOKEN in environment, repo .env, or source-project .env")


def api_request(token: str, url: str, payload=None, method=None,
                raw_body=None, content_type=None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
    body = None
    if raw_body is not None:
        body = raw_body
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    # Accounts under $5 credit are throttled to ~1 prediction-create burst/min (429).
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < 7:
                try:
                    wait = int(json.loads(detail).get("retry_after", 15)) + 3
                except (ValueError, AttributeError):
                    wait = 15
                time.sleep(wait)
                continue
            raise ReplicateError(f"HTTP {e.code} from {url}\n{detail}") from e
    raise ReplicateError(f"Gave up after repeated throttling: {url}")


def check_token() -> dict:
    """Validate the token against /account (no spend). Returns the account dict."""
    return api_request(load_token(), f"{API}/account")


def upload_file(token: str, path: str | Path, as_data_uri: bool = False) -> str:
    path = Path(path)
    if not path.exists():
        raise ReplicateError(f"Input image not found: {path}")
    if as_data_uri:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        if len(b64) > 250_000:
            raise ReplicateError(
                f"{path.name} encodes to {len(b64) // 1024}KB of base64 - over the "
                f"~256KB data URI cap. Compress to <~185KB binary first.")
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return f"data:{mime};base64,{b64}"
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    boundary = uuid.uuid4().hex
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="content"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    body = part + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    resp = api_request(token, f"{API}/files", raw_body=body,
                       content_type=f"multipart/form-data; boundary={boundary}")
    url = resp.get("urls", {}).get("get")
    if not url:
        raise ReplicateError(f"Files API gave no URL: {json.dumps(resp)[:500]}")
    return url


def _deref(ref, schemas: dict) -> dict:
    """Resolve a '#/components/schemas/<name>' pointer against components.schemas."""
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        return {}
    target = schemas.get(ref.rsplit("/", 1)[-1])
    return target if isinstance(target, dict) else {}


def _follow_enum(prop: dict, schemas: dict) -> tuple[Optional[list], Optional[str]]:
    """Find a property's enum (+ its type) when Replicate stores it as a $ref/allOf/
    anyOf/oneOf into components.schemas instead of inline. Returns (None, None) if none."""
    candidates: list[dict] = []
    if "$ref" in prop:
        candidates.append(_deref(prop["$ref"], schemas))
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in prop.get(key) or []:
            if not isinstance(sub, dict):
                continue
            if "enum" in sub:                          # inline enum inside the combiner
                candidates.append(sub)
            elif "$ref" in sub:
                candidates.append(_deref(sub["$ref"], schemas))
    # First resolvable enum wins (handles the common optional shape
    # anyOf: [{$ref: <enum>}, {type: "null"}]); unions of multiple enums aren't merged.
    for cand in candidates:
        if isinstance(cand.get("enum"), list):
            return cand["enum"], cand.get("type")
    return None, None


def _resolve_enums(props: dict, schemas: dict) -> dict:
    """Inline each property's referenced enum (+ type) so callers see prop['enum']
    directly. Returns a new dict; inputs are not mutated. Props with an inline enum or
    no resolvable enum pass through unchanged (aside from the shallow copy)."""
    resolved = {}
    for name, prop in props.items():
        if not isinstance(prop, dict):
            resolved[name] = prop
            continue
        prop = dict(prop)
        if "enum" not in prop:
            enum, typ = _follow_enum(prop, schemas)
            if enum is not None:
                prop["enum"] = enum
                if typ and "type" not in prop:
                    prop["type"] = typ
        resolved[name] = prop
    return resolved


def get_input_schema(token: str, replicate_model_id: str) -> tuple[dict, list]:
    info = api_request(token, f"{API}/models/{replicate_model_id}")
    version = info.get("latest_version") or {}
    schema = version.get("openapi_schema") or {}
    schemas = schema.get("components", {}).get("schemas", {})
    comp = schemas.get("Input", {})
    props = comp.get("properties", {})
    if not props:
        raise ReplicateError(
            f"Could not read input schema for {replicate_model_id} - id ok?")
    return _resolve_enums(props, schemas), comp.get("required", [])


def _pick_field(props: dict, concept: str) -> Optional[str]:
    for name in ALIASES[concept]:
        if name in props:
            return name
    return None


def _coerce(value, prop: dict):
    t = prop.get("type")
    if t == "integer":
        return int(value)
    if t == "number":
        return float(value)
    if t == "boolean":
        return str(value).lower() in ("1", "true", "yes")
    return value


def build_input(props: dict, *, start_url: Optional[str], end_url: Optional[str],
                prompt: str, negative: str = "", duration=None, resolution=None,
                seed=None, extra: Optional[dict] = None) -> dict:
    """Map canonical inputs onto the model's real field names. Image args are
    pre-resolved URLs (or dry-run placeholders)."""
    inp: dict = {}

    def assign(concept, value):
        field = _pick_field(props, concept)
        if field is None:
            if concept in ("start", "end"):
                raise ReplicateError(
                    f"Model has no recognizable {concept}-image field. "
                    f"Fields: {', '.join(sorted(props))}")
            return
        inp[field] = value if concept in ("start", "end") else _coerce(value, props[field])

    if start_url is not None:
        assign("start", start_url)
    if end_url is not None:
        assign("end", end_url)
    assign("prompt", prompt)
    if negative:
        assign("negative", negative)
    if duration is not None:
        assign("duration", duration)
    if resolution:
        assign("resolution", resolution)
    if seed is not None:
        assign("seed", seed)

    # Only want frames - force any boolean audio toggle off.
    for audio_field in ("generate_audio", "audio", "with_audio"):
        if props.get(audio_field, {}).get("type") == "boolean":
            inp[audio_field] = False

    for k, v in (extra or {}).items():
        inp[k] = _coerce(v, props.get(k, {}))
    return inp


def run_prediction(token: str, replicate_model_id: str, inp: dict, out_path: Path,
                   progress_cb: ProgressCb = None, poll_s: int = 10,
                   timeout_s: int = 1800) -> dict:
    resp = api_request(token, f"{API}/models/{replicate_model_id}/predictions",
                       payload={"input": inp})
    pred_id = resp["id"]
    get_url = resp.get("urls", {}).get("get") or f"{API}/predictions/{pred_id}"
    _log(progress_cb, f"submitted {pred_id}")
    t0 = time.time()
    status = resp.get("status")
    while status in ("starting", "processing", None):
        if time.time() - t0 > timeout_s:
            raise ReplicateError(f"Timed out after {timeout_s}s (https://replicate.com/p/{pred_id})")
        time.sleep(poll_s)
        resp = api_request(token, get_url)
        status = resp.get("status")
        _log(progress_cb, f"[{int(time.time() - t0):>4}s] {status}")
    if status != "succeeded":
        raise ReplicateError(f"Prediction {status}: {resp.get('error')}\n"
                             f"{(resp.get('logs') or '')[-800:]}")

    output = resp.get("output")
    url = None
    if isinstance(output, str):
        url = output
    elif isinstance(output, list) and output:
        url = output[0] if isinstance(output[0], str) else output[0].get("url")
    elif isinstance(output, dict):
        url = output.get("url") or output.get("video")
    if not url:
        raise ReplicateError(f"No video URL in output: {json.dumps(output)[:500]}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _log(progress_cb, "downloading result")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(out_path, "wb") as f:
        f.write(r.read())
    return {"prediction_id": pred_id, "predict_time": resp.get("metrics", {}).get("predict_time")}


def generate(replicate_model_id: str, *, start: str, out_path: Path,
             end: Optional[str] = None, prompt: str = "", negative: str = "",
             duration=None, resolution=None, seed=None, extra: Optional[dict] = None,
             data_uri: bool = False, progress_cb: ProgressCb = None,
             dry_run: bool = False) -> dict:
    """End-to-end hosted generation. With dry_run=True, resolves the input WITHOUT
    uploading images or creating a prediction (no spend) and returns it under
    {'dry_run': True, 'input': ...}."""
    token = load_token()
    if dry_run:
        props, _ = get_input_schema(token, replicate_model_id)
        inp = build_input(props, start_url=(f"<dry:{Path(start).name}>" if start else None),
                          end_url=(f"<dry:{Path(end).name}>" if end else None),
                          prompt=prompt, negative=negative, duration=duration,
                          resolution=resolution, seed=seed, extra=extra)
        return {"dry_run": True, "input": inp}

    props, _ = get_input_schema(token, replicate_model_id)
    _log(progress_cb, "uploading start frame")
    start_url = upload_file(token, start, data_uri)
    end_url = None
    if end:
        _log(progress_cb, "uploading end frame")
        end_url = upload_file(token, end, data_uri)
    inp = build_input(props, start_url=start_url, end_url=end_url, prompt=prompt,
                      negative=negative, duration=duration, resolution=resolution,
                      seed=seed, extra=extra)
    meta = run_prediction(token, replicate_model_id, inp, Path(out_path), progress_cb)
    meta["video_path"] = str(out_path)
    return meta
