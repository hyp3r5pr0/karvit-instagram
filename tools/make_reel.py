#!/usr/bin/env python3
"""Make a 9:16 Instagram reel from product mockups (needs ffmpeg).

Each mockup becomes a slide with a slow push-in, joined by smooth transitions.
Slide 1 carries your hook (the line that stops the scroll), the last slide your
call to action. Text stays inside Instagram's safe zones so the app's buttons
and caption never cover it.

Examples
  python tools/make_reel.py front.png back.png model.png \\
      --hook "POV: your tee finally matches your playlist" \\
      --out media/reels/playlist-tee.mp4

  python tools/make_reel.py *.png --hook "3 ways to style it" \\
      --text "Oversized" --text "Tucked" --text "Layered" \\
      --music assets/music/track.mp3 --cover media/reels/playlist-tee-cover.jpg

Music: use tracks you have rights to (e.g. YouTube Audio Library, Pixabay Music).
Trending Instagram audio can't be attached through the API.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (blurred_backdrop, contain, drop_shadow, load_rgb,  # noqa: E402
                     text_block, wordmark)

W, H = 1080, 1920
FPS = 30
# Instagram safe zones on a 1080x1920 reel: keep text roughly within y 250..1550
WORDMARK_Y = 255
TOP_TEXT_Y = 400
PRODUCT_CENTER_Y = 1030
PRODUCT_BOX = (920, 860)
CTA_Y = 1560


def compose_slide(src: Path, *, top_text: str | None, cta: str | None,
                  bg: str | None, brand: str | None) -> "Image.Image":  # noqa: F821
    im = load_rgb(src)
    solid = tuple(int(bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) if bg else None
    canvas = blurred_backdrop(im, (W, H), darken=0.5, solid=solid)
    fg = contain(im, PRODUCT_BOX)
    pos = ((W - fg.width) // 2, PRODUCT_CENTER_Y - fg.height // 2)
    drop_shadow(canvas, fg, pos)
    if brand:
        wordmark(canvas, y=WORDMARK_Y - 20, size=34, text=brand)
    if top_text:
        text_block(canvas, top_text, center_y=TOP_TEXT_Y, size=66, max_w=880)
    if cta:
        text_block(canvas, cta, center_y=CTA_Y, size=54, max_w=820,
                   fill=(20, 20, 20), pill_rgba=(255, 255, 255, 235))
    return canvas


def run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{res.stderr[-1500:]}")


def build(args) -> Path:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is not installed (https://ffmpeg.org/download.html)")
    n = len(args.images)
    texts = list(args.text or [])
    trans = args.transition_seconds if n > 1 else 0.0
    durations = [args.seconds + (args.hook_extra if i == 0 else 0) for i in range(n)]
    if n == 1:
        durations = [max(args.seconds * 2, 4.0)]
    if args.cta and n > 1:
        durations[-1] += 0.6  # give the call to action a beat longer

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        clips = []
        for i, src in enumerate(args.images):
            top = args.hook if i == 0 else (texts[i - 1] if i - 1 < len(texts) else None)
            cta = args.cta if i == n - 1 else None
            slide = compose_slide(src, top_text=top, cta=cta, bg=args.bg,
                                  brand=None if args.no_brand else args.brand)
            still = tmpd / f"s{i}.png"
            slide.save(still)
            if i == 0 and args.cover:
                args.cover.parent.mkdir(parents=True, exist_ok=True)
                slide.save(args.cover, "JPEG", quality=92)
            frames = int(round(durations[i] * FPS))
            zoom_in = i % 2 == 0
            z = (f"1+{args.zoom}*on/{frames}" if zoom_in
                 else f"1+{args.zoom}-{args.zoom}*on/{frames}")
            clip = tmpd / f"c{i}.mp4"
            run(["ffmpeg", "-y", "-loop", "1", "-i", str(still),
                 "-vf", (f"scale={W * 2}:{H * 2},zoompan=z='{z}':"
                         f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:"
                         f"s={W}x{H}:fps={FPS},format=yuv420p"),
                 "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "16", str(clip)])
            clips.append(clip)

        total = sum(durations) - trans * (n - 1)
        cmd = ["ffmpeg", "-y"]
        for c in clips:
            cmd += ["-i", str(c)]
        if args.music:
            cmd += ["-stream_loop", "-1", "-i", str(args.music)]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

        parts, last, offset = [], "[0:v]", 0.0
        for i in range(1, n):
            offset += durations[i - 1] - trans
            out = f"[v{i}]"
            parts.append(f"{last}[{i}:v]xfade=transition={args.transition}:"
                         f"duration={trans}:offset={offset:.3f}{out}")
            last = out
        a_in = f"[{n}:a]"
        fade_out = max(total - 1.2, 0)
        parts.append(f"{a_in}atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                     f"volume={args.music_volume},afade=t=in:d=0.3,"
                     f"afade=t=out:st={fade_out:.3f}:d=1.2[a]")
        cmd += ["-filter_complex", ";".join(parts), "-map", last if n > 1 else "0:v",
                "-map", "[a]", "-t", f"{total:.3f}",
                "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart", str(args.out)]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        run(cmd)
    return args.out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path, help="mockups in order")
    ap.add_argument("--out", type=Path, required=True, help="e.g. media/reels/name.mp4")
    ap.add_argument("--hook", help="text on slide 1 (keep it under ~8 words)")
    ap.add_argument("--text", action="append",
                    help="text for slide 2, 3, ... (repeat the flag)")
    ap.add_argument("--cta", default="Shop KARVIT on Etsy\nlink in bio",
                    help="call to action on the last slide ('' to disable)")
    ap.add_argument("--brand", default="KARVIT")
    ap.add_argument("--no-brand", action="store_true", help="hide the wordmark")
    ap.add_argument("--music", type=Path, help="mp3/m4a/wav you have the rights to")
    ap.add_argument("--music-volume", type=float, default=0.9)
    ap.add_argument("--seconds", type=float, default=2.4, help="per slide")
    ap.add_argument("--hook-extra", type=float, default=0.8, help="extra time on slide 1")
    ap.add_argument("--transition", default="smoothleft",
                    help="ffmpeg xfade: fade, smoothleft, slideleft, circleopen, ...")
    ap.add_argument("--transition-seconds", type=float, default=0.35)
    ap.add_argument("--zoom", type=float, default=0.05, help="push-in amount")
    ap.add_argument("--bg", help="solid background colour instead of blur, e.g. '#111111'")
    ap.add_argument("--cover", type=Path, help="also save slide 1 as a JPEG cover image")
    args = ap.parse_args()
    # let "\n" typed on the command line mean a line break
    fix = lambda s: s.replace("\\n", "\n") if s else s  # noqa: E731
    args.hook, args.cta = fix(args.hook), fix(args.cta)
    args.text = [fix(t) for t in (args.text or [])]
    for p in args.images:
        if not p.exists():
            ap.error(f"not found: {p}")
    if args.music and not args.music.exists():
        ap.error(f"music not found: {args.music}")
    out = build(args)
    print(f"{out}  ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
