"""Extract every layer of a Paint.NET (.pdn) file as an individual PNG frame.

Built for the AnimGen "repair" workflow: a take's raw frames get cleaned up in
Paint.NET and saved as one layered .pdn (one layer per frame). This pulls them
back out as a flat frame sequence.

KEY FACTS about these repaired files (learned the hard way, see the spider take):
  - Layer NAMES are unreliable editing cruft ("Layer 103" can repeat dozens of
    times). The only trustworthy frame order is the *stacking order* pypdn
    returns, so we number output by stacking index, NOT by any embedded name.
  - Pixels are kept verbatim: the magenta (255,0,255) background is preserved,
    not keyed -- downstream repack_for_engine.py does the magenta->alpha keying.
  - Hidden layers are exported too by default (they're usually valid continuation
    frames); pass --visible-only to drop them. A manifest.json records each
    layer's name/visibility/opacity/blend so nothing is lost.

usage:
  python pdn_extract_layers.py <file.pdn> [--out DIR] [--visible-only] [--prefix frame_]

DIR defaults to "<pdn-dir>/frames". Requires pypdn (pip install pypdn).
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

import numpy as np
import pypdn
from PIL import Image

MAGENTA = (255, 0, 255)


def non_magenta_bbox(img: np.ndarray, tol: int = 24):
    """Tight bbox of pixels that are neither magenta nor transparent (just for the
    manifest / sanity reporting; the saved PNG is the full untrimmed canvas)."""
    r, g, b, a = (img[:, :, i].astype(int) for i in range(4))
    is_mag = (abs(r - MAGENTA[0]) <= tol) & (g <= tol + 16) & (abs(b - MAGENTA[2]) <= tol)
    fg = (a > 8) & ~is_mag
    if not fg.any():
        return None
    ys, xs = np.where(fg)
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdn", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--prefix", default="frame_")
    ap.add_argument("--visible-only", action="store_true")
    args = ap.parse_args()

    layered = pypdn.read(str(args.pdn))
    layers = list(layered.layers)
    out_dir = args.out or (args.pdn.parent / "frames")
    out_dir.mkdir(parents=True, exist_ok=True)

    chosen = [(i, L) for i, L in enumerate(layers) if (L.visible or not args.visible_only)]
    width = max(3, len(str(len(chosen) - 1)))

    manifest = {
        "source": str(args.pdn),
        "canvas": [layered.width, layered.height],
        "total_layers": len(layers),
        "exported": len(chosen),
        "visible_only": args.visible_only,
        "note": "output index = pypdn stacking order = animation order; names are cruft",
        "frames": [],
    }

    for out_idx, (src_idx, L) in enumerate(chosen):
        img = np.ascontiguousarray(L.image)  # RGBA uint8
        name = f"{args.prefix}{out_idx:0{width}d}.png"
        Image.fromarray(img, "RGBA").save(out_dir / name)
        manifest["frames"].append({
            "file": name,
            "stack_index": src_idx,
            "layer_name": L.name,
            "visible": bool(L.visible),
            "opacity": int(getattr(L, "opacity", 255)),
            "blend": str(getattr(L, "blendMode", "")),
            "content_bbox": non_magenta_bbox(img),
        })

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"extracted {len(chosen)}/{len(layers)} layers -> {out_dir}")
    print(f"  canvas {layered.width}x{layered.height}, names {args.prefix}{{0..{len(chosen)-1}:0{width}d}}.png")
    if args.visible_only:
        print(f"  (visible-only: dropped {len(layers) - len(chosen)} hidden layer(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
