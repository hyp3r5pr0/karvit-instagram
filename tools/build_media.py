#!/usr/bin/env python3
"""Build reels and carousel slides from Etsy listing photos (content/build.yml).

Runs in GitHub Actions ("Build media" workflow); also works locally with
internet access and ffmpeg installed.

  python tools/build_media.py                 # build everything in build.yml
  python tools/build_media.py --only chai     # just outputs whose path contains "chai"
  python tools/build_media.py --skip-existing # don't rebuild files already in media/
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, font, text_block, wordmark  # noqa: E402
from prep_image import prep  # noqa: E402

BUILD_FILE = ROOT / "content" / "build.yml"
CACHE = ROOT / ".cache" / "photos"
PREVIEWS = ROOT / "media" / "previews"
BRAND_BG = (24, 24, 27)
ACCENT = (232, 168, 56)   # warm marigold, fits the desi + Diwali designs
SIZE_TOKEN = re.compile(r"/il_[0-9a-zA-Z]+x[0-9a-zA-Z]+\.")


def candidates(url: str) -> list[str]:
    """Etsy serves the same photo at several sizes; try the biggest first."""
    if not SIZE_TOKEN.search(url):
        return [url]
    out = []
    for size in ("fullxfull", "1588xN", "1080xN", "794xN"):
        u = SIZE_TOKEN.sub(f"/il_{size}.", url, count=1)
        if u not in out:
            out.append(u)
    if url not in out:
        out.append(url)
    return out


def fetch(name: str, url: str, *, http=requests) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / f"{name}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.jpg"
    if dest.exists():
        return dest
    errors = []
    headers = {"User-Agent": "Mozilla/5.0 (KARVIT media builder; shop owner)"}
    for u in candidates(url):
        for attempt in range(2):
            try:
                r = http.get(u, headers=headers, timeout=60)
                if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF"):
                    dest.write_bytes(r.content)
                    with Image.open(dest) as im:
                        print(f"  {name}: {im.width}x{im.height}  ({u.split('/il_')[-1][:12]}…)")
                    return dest
                errors.append(f"{u} → HTTP {r.status_code}")
                break
            except requests.RequestException as exc:
                errors.append(f"{u} → {exc}")
                time.sleep(3)
    sys.exit(f"Couldn't download photo '{name}':\n  " + "\n  ".join(errors[-4:]))


def cover_slide(out: Path, title: str, sub: str | None) -> None:
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), BRAND_BG)
    wordmark(im, y=110, size=38)
    text_block(im, title, center_y=H // 2 - 40, size=104, max_w=900, pill=False)
    if sub:
        text_block(im, sub, center_y=H - 230, size=46, max_w=900, fill=(20, 20, 20),
                   pill_rgba=ACCENT + (255,))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=92, optimize=True, progressive=True)


def reel_preview(video: Path, dest: Path) -> None:
    """4 frames side by side, so the reel can be checked on a phone."""
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(video)], capture_output=True, text=True).stdout.strip() or 0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, frac in enumerate((0.12, 0.38, 0.62, 0.95)):
        f = dest.with_suffix(f".f{i}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=360:-1", str(f)], check=True)
        frames.append(f)
    _montage([Image.open(f) for f in frames], dest)
    for f in frames:
        f.unlink()


def _montage(images: list[Image.Image], dest: Path) -> None:
    gap = 12
    h = max(i.height for i in images)
    sheet = Image.new("RGB", (sum(i.width for i in images) + gap * (len(images) - 1), h), "white")
    x = 0
    for i in images:
        sheet.paste(i, (x, 0))
        x += i.width + gap
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(dest, "JPEG", quality=85)


def build(only: str | None, skip_existing: bool, *, fetcher=fetch) -> list[Path]:
    spec = yaml.safe_load(BUILD_FILE.read_text(encoding="utf-8"))
    photos: dict[str, str] = spec.get("photos") or {}
    made: list[Path] = []

    def photo(name: str) -> Path:
        if name not in photos:
            sys.exit(f"build.yml: photo '{name}' is not listed under photos:")
        return fetcher(name, photos[name])

    for reel in spec.get("reels") or []:
        out = ROOT / reel["out"]
        if (only and only not in reel["out"]) or (skip_existing and out.exists()):
            continue
        print(f"Reel → {reel['out']}")
        srcs = [photo(n) for n in reel["photos"]]
        cmd = [sys.executable, str(ROOT / "tools" / "make_reel.py"), *map(str, srcs),
               "--out", str(out)]
        if reel.get("hook"):
            cmd += ["--hook", reel["hook"]]
        for t in reel.get("texts") or []:
            cmd += ["--text", t]
        if "cta" in reel:
            cmd += ["--cta", reel["cta"] or ""]
        if reel.get("music"):
            cmd += ["--music", str(ROOT / reel["music"])]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            sys.exit(f"make_reel failed for {reel['out']}:\n{res.stderr or res.stdout}")
        reel_preview(out, PREVIEWS / f"{out.stem}.jpg")
        made.append(out)

    for car in spec.get("carousels") or []:
        outdir = ROOT / car["outdir"]
        if only and only not in car["outdir"]:
            continue
        print(f"Carousel → {car['outdir']}")
        slides: list[Path] = []
        if car.get("cover"):
            c = car["cover"]
            dest = outdir / c["file"]
            if not (skip_existing and dest.exists()):
                cover_slide(dest, c["title"], c.get("sub"))
            slides.append(dest)
        for s in car.get("slides") or []:
            dest = outdir / s["file"]
            if not (skip_existing and dest.exists()):
                prep(photo(s["photo"]), dest, "portrait", None, s.get("text"), 0.04)
            slides.append(dest)
        _montage([Image.open(p).resize((270, 338)) for p in slides],
                 PREVIEWS / f"{outdir.name}.jpg")
        made += slides
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()
    _ = font(10)  # fail early if fonts are missing
    made = build(args.only, args.skip_existing)
    print(f"\nBuilt {len(made)} file(s). Previews in media/previews/")


if __name__ == "__main__":
    main()
