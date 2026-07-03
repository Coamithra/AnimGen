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
import io
import json
import mimetypes
import socket
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


# Replicate's edge returns these HTTP statuses; map each to a plain-English note so a failed
# take says WHY instead of a bare "HTTP 504". A 504/502/503/500 is a TRANSIENT server-side
# hiccup (NOT a billing/quota problem - that's 402/429), so those + 429 are retried.
_HTTP_EXPLAIN = {
    400: "the request was malformed",
    401: "authentication failed - check REPLICATE_TOKEN",
    402: "payment required - the Replicate account is out of credit",
    403: "forbidden - the token can't access this model",
    404: "not found - wrong model id or endpoint",
    422: "the model rejected the inputs",
    429: "rate-limited - accounts under $5 credit are throttled to ~1 create/min",
    500: "Replicate had an internal server error (transient - retried, then gave up)",
    502: "Replicate gateway error (transient - retried, then gave up)",
    503: "Replicate is temporarily unavailable (transient - retried, then gave up)",
    504: "Replicate gateway timeout, their server was slow to respond "
         "(transient - retried, then gave up)",
}
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})  # transient - safe to retry the request

# A connection reset / DNS blip / read timeout mid-poll is transient too, but a raw URLError or
# read TimeoutError isn't an HTTPError so it skips the loop above. We fold it in - but ONLY for
# IDEMPOTENT GET requests. Retrying a non-idempotent POST (the create prediction, a file upload)
# on a network error risks a double-create/double-spend (finding M2), so those propagate wrapped
# on the first blip instead of being retried.
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, socket.timeout)


def _is_idempotent(method: Optional[str], body: Optional[bytes]) -> bool:
    """True when the effective HTTP method is GET (no explicit non-GET method, no request body) -
    the only requests safe to retry on a bare network error. urllib sends GET when data is None
    and method is unset; anything with a body or an explicit method is treated as non-idempotent."""
    if method is not None:
        return method.upper() == "GET"
    return body is None


def _network_wait(attempt: int) -> int:
    """Seconds of escalating backoff before retrying a transport-level failure. Pure (no sleep);
    the same curve as _retry_wait's 5xx tail, kept in one place so the two policies can't drift."""
    return min(4 * (attempt + 1), 20)


def _network_error_message(url: str, err: Exception, *, retried: bool = True) -> str:
    """A human-readable one-liner for a Replicate call that failed at the transport layer (no HTTP
    response): a connection reset, DNS failure, or read timeout. Pure. retried=False is the
    non-idempotent POST case - it says WHY there was no retry, so the reader isn't invited to
    just re-send a request that may already have been accepted (and billed) server-side."""
    reason = getattr(err, "reason", None) or err
    head = f"Network error reaching Replicate (from {url}): {reason}"
    if retried:
        return f"{head}\nThe connection failed before a response - retried, then gave up."
    return (f"{head}\nNot retried: the request may already have been accepted server-side, and "
            f"re-sending it could create (and bill) a duplicate prediction.")


def _http_error_message(code: int, url: str, detail: str) -> str:
    """A human-readable one-liner for a failed Replicate HTTP call: the status, a plain-English
    explanation when the code is recognised, the endpoint, then the raw response body. Pure."""
    note = _HTTP_EXPLAIN.get(code)
    head = f"HTTP {code}" + (f" - {note}" if note else "") + f" (from {url})"
    detail = (detail or "").strip()
    return f"{head}\n{detail}" if detail else head


def _retry_wait(code: int, detail: str, attempt: int) -> int:
    """Seconds to wait before retrying a transient response: honour a 429's retry_after; give a
    transient 5xx a short escalating backoff. Pure (no sleep)."""
    if code == 429:
        try:
            return int(json.loads(detail).get("retry_after", 15)) + 3
        except (ValueError, AttributeError, TypeError):
            return 15
    return _network_wait(attempt)


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
    idempotent = _is_idempotent(method, body)
    # 429 (low-credit throttle) and transient 5xx (gateway timeout / unavailable) are retried;
    # a bare network error (connection reset / DNS / read timeout) is retried too, but ONLY for
    # idempotent GETs (a POST could have already committed - see M2). Any other status fails fast
    # with a humanized message (see _http_error_message).
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in _RETRY_STATUS and attempt < 7:
                time.sleep(_retry_wait(e.code, detail, attempt))
                continue
            raise ReplicateError(_http_error_message(e.code, url, detail)) from e
        except _NETWORK_ERRORS as e:
            if idempotent and attempt < 7:
                time.sleep(_network_wait(attempt))
                continue
            raise ReplicateError(_network_error_message(url, e, retried=idempotent)) from e
    raise ReplicateError(f"Gave up retrying Replicate after repeated transient errors: {url}")


def check_token() -> dict:
    """Validate the token against /account (no spend). Returns the account dict."""
    return api_request(load_token(), f"{API}/account")


DATA_URI_B64_CAP = 250_000     # ~183KB binary; Replicate's inline data-URI ceiling for
                               # requires_data_uri models (vidu/q3-pro - no Files API there).
_DATA_URI_BG = (255, 0, 255)   # pipeline/framing.MAGENTA - the keying-contract background.


def _b64_len(raw: bytes) -> int:
    return len(base64.b64encode(raw))


def _flatten_to_bg(im, bg=_DATA_URI_BG):
    """Composite any transparency onto the magenta keying background, returning an RGB image, so
    quantizing can't leave a halo. A keypose rendered by the framing pipeline is already RGB on
    magenta (this is a no-op there); it only guards a stray RGBA asset."""
    from PIL import Image
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, bg + (255,))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return im.convert("RGB")


def _encode_png_quantized(im, colors: int) -> bytes:
    """PNG-encode `im` reduced to an adaptive `colors`-entry palette - crisp sprite edges and the
    flat magenta survive (unlike JPEG), at a fraction of a truecolor PNG's size."""
    from PIL import Image
    buf = io.BytesIO()
    im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def fit_data_uri(raw: bytes, cap: int = DATA_URI_B64_CAP) -> tuple[bytes, str]:
    """Shrink an image so its base64 data-URI fits under `cap` chars, for models that inline the
    input (vidu/q3-pro: no Files API). Quantize-first at full resolution - a flat-bg sprite often
    fits on palette reduction alone - then progressively downscale + re-quantize. Returns
    (png_bytes, "image/png"). Pure (no network); raises ReplicateError only if it can't get under
    the cap even at the floor resolution (i.e. the bytes aren't a real image)."""
    from PIL import Image
    im = _flatten_to_bg(Image.open(io.BytesIO(raw)))
    palettes = (256, 128, 64)
    for colors in palettes:                                  # 1) palette reduction at full res
        out = _encode_png_quantized(im, colors)
        if _b64_len(out) <= cap:
            return out, "image/png"
    w, h = im.size                                           # 2) downscale + re-quantize to fit
    floor = 64
    for _ in range(16):
        w, h = max(floor, int(w * 0.85)), max(floor, int(h * 0.85))
        small = im.resize((w, h), Image.Resampling.LANCZOS)
        for colors in palettes:
            out = _encode_png_quantized(small, colors)
            if _b64_len(out) <= cap:
                return out, "image/png"
        if w <= floor and h <= floor:
            break
    raise ReplicateError(
        f"Could not shrink the image under the ~{cap // 1000}KB data-URI cap even after "
        f"downscaling + quantizing - is it valid image data?")


def upload_file(token: str, path: str | Path, as_data_uri: bool = False,
                progress_cb: ProgressCb = None) -> str:
    path = Path(path)
    if not path.exists():
        raise ReplicateError(f"Input image not found: {path}")
    if as_data_uri:
        raw = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        if _b64_len(raw) > DATA_URI_B64_CAP:                 # too big to inline -> auto-shrink it
            _log(progress_cb, f"input image over the data-URI cap, shrinking {path.name}")
            raw, mime = fit_data_uri(raw)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
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


def derive_capabilities(props: dict) -> dict:
    """Capability flags inferred from a model's live input schema, for the Model Library
    refresh to sync into model_library.json. Field-presence is the signal (mirrors the
    shot editor's own negative-prompt check over ALIASES["negative"]).

    Deliberately omits end-frame support: `supports_end_frame` stays hand-authored (it
    predates this sync and a roster invariant relies on it), so the refresh never risks
    silently flipping it from a transient/renamed schema."""
    return {
        "supports_negative_prompt": any(n in props for n in ALIASES["negative"]),
        "supports_camera_fixed": "camera_fixed" in props,
    }


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


def cancel_prediction(pred_id: str, token: Optional[str] = None) -> None:
    """Best-effort cancel of a running prediction (stops spend). Server-side idempotent;
    a no-op if the prediction already finished. Raises ReplicateError on a transport
    failure - callers that just want to stop a render should swallow it."""
    api_request(token or load_token(), f"{API}/predictions/{pred_id}/cancel", method="POST")


def _output_video_url(output: object) -> str:
    """Resolve a succeeded prediction's `output` to a video URL, raising ReplicateError
    (quoting the raw output) when it carries no recognizable URL. A non-str URL — a list
    whose first element is neither a str nor a dict (e.g. ``[None]``, ``[[...]]``, ``[42]``),
    or a dict whose ``url``/``video`` value is itself nested — yields no usable URL and
    falls through to the ReplicateError rather than an opaque AttributeError/TypeError."""
    url = None
    if isinstance(output, str):
        url = output
    elif isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str):
            url = first
        elif isinstance(first, dict):
            url = first.get("url")
    elif isinstance(output, dict):
        url = output.get("url") or output.get("video")
    if not isinstance(url, str) or not url:
        raise ReplicateError(f"No video URL in output: {json.dumps(output)[:500]}")
    return url


def _download_result(url: str, token: str, out_path: Path) -> None:
    """Fetch a succeeded prediction's video to `out_path`. An idempotent GET, so a transient
    network blip (reset / DNS / read timeout) mid-download is retried with backoff before giving
    up; an HTTP error and a final network give-up both raise ReplicateError. The whole body is
    read into memory then written, so a partial read never leaves a truncated file on disk - a
    deliberate tradeoff (a transient RAM spike the size of one clip; these are short, bounded
    videos, and the pre-existing code buffered the full body the same way)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            out_path.write_bytes(data)
            return
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in _RETRY_STATUS and attempt < 7:
                time.sleep(_retry_wait(e.code, detail, attempt))
                continue
            raise ReplicateError(_http_error_message(e.code, url, detail)) from e
        except _NETWORK_ERRORS as e:
            if attempt < 7:
                time.sleep(_network_wait(attempt))
                continue
            raise ReplicateError(_network_error_message(url, e)) from e
    raise ReplicateError(f"Gave up retrying the result download after repeated transient errors: {url}")


def run_prediction(token: str, replicate_model_id: str, inp: dict, out_path: Path,
                   progress_cb: ProgressCb = None, poll_s: int = 10,
                   timeout_s: int = 1800,
                   on_submit: Optional[Callable[[str], None]] = None) -> dict:
    resp = api_request(token, f"{API}/models/{replicate_model_id}/predictions",
                       payload={"input": inp})
    pred_id = resp["id"]
    get_url = resp.get("urls", {}).get("get") or f"{API}/predictions/{pred_id}"
    if on_submit:                     # record the prediction id NOW so a take can be
        on_submit(pred_id)            # cancelled mid-render (or reconciled after a restart)
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

    url = _output_video_url(resp.get("output"))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _log(progress_cb, "downloading result")
    _download_result(url, token, out_path)
    return {"prediction_id": pred_id, "predict_time": resp.get("metrics", {}).get("predict_time")}


def generate(replicate_model_id: str, *, start: str, out_path: Path,
             end: Optional[str] = None, prompt: str = "", negative: str = "",
             duration=None, resolution=None, seed=None, extra: Optional[dict] = None,
             data_uri: bool = False, progress_cb: ProgressCb = None,
             dry_run: bool = False,
             on_submit: Optional[Callable[[str], None]] = None) -> dict:
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
    start_url = upload_file(token, start, data_uri, progress_cb)
    end_url = None
    if end:
        _log(progress_cb, "uploading end frame")
        end_url = upload_file(token, end, data_uri, progress_cb)
    inp = build_input(props, start_url=start_url, end_url=end_url, prompt=prompt,
                      negative=negative, duration=duration, resolution=resolution,
                      seed=seed, extra=extra)
    meta = run_prediction(token, replicate_model_id, inp, Path(out_path), progress_cb,
                          on_submit=on_submit)
    meta["video_path"] = str(out_path)
    return meta
