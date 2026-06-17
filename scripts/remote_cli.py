"""Thin client for AnimGen's remote-control server (remote/server.py).

Launch the app with the server on:
    ANIMGEN_REMOTE=1 .venv/Scripts/python.exe app.py

Then drive it (defaults to http://127.0.0.1:8765; override with ANIMGEN_REMOTE_PORT or
--port). Uses only the stdlib so it runs with the system python too.

    python scripts/remote_cli.py snapshot                 # list drivable widgets
    python scripts/remote_cli.py shot screen.png          # save a window screenshot
    python scripts/remote_cli.py shot modelFilter f.png   # screenshot one widget
    python scripts/remote_cli.py click --text Generate     # click by visible text
    python scripts/remote_cli.py click --ref modelFilter   # click by ref/objectName
    python scripts/remote_cli.py set --ref mainTabs --value Assets   # switch tab
    python scripts/remote_cli.py type --ref QLineEdit:0 --text hello
    python scripts/remote_cli.py key --ref QLineEdit:0 --key enter
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _base(port: int | None) -> str:
    port = port or int(os.environ.get("ANIMGEN_REMOTE_PORT", "8765"))
    return f"http://127.0.0.1:{port}"


def _request(base: str, method: str, path: str, body: dict | None = None) -> tuple[bytes, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.read(), exc.headers.get("Content-Type", "")


def _print_response(raw: bytes) -> None:
    try:
        print(json.dumps(json.loads(raw), indent=2))
    except ValueError:  # server died / returned a non-JSON body
        print(raw.decode("utf-8", "replace"))


def _selector_body(args) -> dict:
    body: dict = {}
    for key in ("ref", "object_name", "text"):
        val = getattr(args, key, None)
        if val is not None:
            body[key] = val
    return body


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Drive the AnimGen GUI over its control server.")
    ap.add_argument("--port", type=int, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    sub.add_parser("snapshot")

    p_shot = sub.add_parser("shot", help="save a screenshot (window, or one widget by ref)")
    p_shot.add_argument("a", nargs="?", help="ref (optional) then output path, or just path")
    p_shot.add_argument("b", nargs="?")

    for name in ("click", "type", "key", "set"):
        p = sub.add_parser(name)
        p.add_argument("--ref")
        p.add_argument("--object-name", dest="object_name")
        p.add_argument("--text")
        if name == "key":
            p.add_argument("--key", required=True)
        if name == "set":
            p.add_argument("--value")
            p.add_argument("--checked", choices=("true", "false"))

    args = ap.parse_args(argv)
    if args.cmd == "type" and not args.text:
        ap.error("type requires --text")
    base = _base(args.port)

    if args.cmd in ("health", "snapshot"):
        raw, _ = _request(base, "GET", "/" + args.cmd)
        _print_response(raw)
        return 0

    if args.cmd == "shot":
        ref, out = (args.a, args.b) if args.b else (None, args.a or "screenshot.png")
        path = "/screenshot" + (f"?ref={ref}" if ref else "")
        raw, ctype = _request(base, "GET", path)
        if "image/png" not in ctype:
            print(raw.decode("utf-8", "replace"), file=sys.stderr)
            return 1
        with open(out, "wb") as fh:
            fh.write(raw)
        print(f"wrote {out} ({len(raw)} bytes)")
        return 0

    body = _selector_body(args)
    if args.cmd == "type":
        body["text"] = args.text
    if args.cmd == "key":
        body["key"] = args.key
    if args.cmd == "set":
        if args.value is not None:
            body["value"] = args.value
        if args.checked is not None:
            body["checked"] = args.checked == "true"
    raw, _ = _request(base, "POST", "/" + args.cmd, body)
    _print_response(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
