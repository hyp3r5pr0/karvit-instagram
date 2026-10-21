#!/usr/bin/env python3
"""Turn any mockup (PNG/WebP/JPEG, any size) into an Instagram-ready JPEG.

Examples
  python tools/prep_image.py mockup.png                       # 4:5 portrait, 1080x1350
  python tools/prep_image.py mockup.png --shape square --out media/posts/tee.jpg
  python tools/prep_image.py a.png b.png c.png --outdir media/carousels/drop1
  python tools/prep_image.py mockup.png --text "New drop" --bg "#F4F1EA"

The product is never cropped: it is fitted inside the frame over a blurred copy
of itself (or a solid colour with --bg).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import blurred_backdrop, contain, load_rgb, text_block  # noqa: E402

SHAPES = {"portrait": (1080, 1350), "square": (1080, 1080), "landscape": (1080, 566)}


def hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def prep(src: Path, out: Path, shape: str, bg: str | None, text: str | None,
         margin: float) -> Path:
    size = SHAPES[shape]
    im = load_rgb(src)
    canvas = blurred_backdrop(im, size, solid=hex_rgb(bg) if bg else None)
    # room for the headline; longer text wraps to more lines, so give it more
    top = int(size[1] * (0.22 if len(text) > 38 else 0.17)) if text else 0
    box = (int(size[0] * (1 - 2 * margin)), int((size[1] - top) * (1 - 2 * margin)))
    fg = contain(im, box)
    canvas.paste(fg, ((size[0] - fg.width) // 2, top + (size[1] - top - fg.height) // 2))
    if text:
        text_block(canvas, text, center_y=int(top * 0.55), size=56, max_w=int(size[0] * 0.8))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=92, optimize=True, progressive=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--shape", choices=SHAPES, default="portrait",
                    help="portrait 4:5 gets the most feed space (default)")
    ap.add_argument("--out", type=Path, help="output file (single image only)")
    ap.add_argument("--outdir", type=Path, help="output folder (default: media/posts)")
    ap.add_argument("--bg", help="solid background colour, e.g. '#F4F1EA'")
    ap.add_argument("--text", help="short headline across the top")
    ap.add_argument("--margin", type=float, default=0.04, help="space around product (0-0.3)")
    args = ap.parse_args()
    if args.out and len(args.images) > 1:
        ap.error("--out works with one image; use --outdir for several")
    outdir = args.outdir or Path("media/posts")
    for i, src in enumerate(args.images, start=1):
        out = args.out or outdir / (f"{i:02d}-{src.stem}.jpg" if len(args.images) > 1
                                    else f"{src.stem}.jpg")
        print(prep(src, out, args.shape, args.bg, args.text, args.margin))


if __name__ == "__main__":
    main()
