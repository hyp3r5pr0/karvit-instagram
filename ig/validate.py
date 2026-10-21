"""Checks the queue and media against Instagram's publishing rules *before*
publish time, so problems show up when you commit, not at 7:30 PM in New York."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .content import (Post, Settings, caption_problems, is_url, load_state,
                      resolve_media)
from .publisher import JSDELIVR_MAX_BYTES, VIDEO_EXT, entry_for

IMAGE_EXT = {".jpg", ".jpeg"}


def _check_image(path: Path, errors: list[str], warns: list[str], label: str) -> float | None:
    if path.suffix.lower() not in IMAGE_EXT:
        errors.append(f"{label}: Instagram only accepts JPEG. Convert with: "
                      f"python tools/prep_image.py {path.name}")
        return None
    try:
        with Image.open(path) as im:
            fmt, (w, h) = im.format, im.size
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label}: can't open image ({exc})")
        return None
    if fmt != "JPEG":
        errors.append(f"{label}: file is really {fmt}, not JPEG")
    ratio = w / h
    if not 0.8 - 0.005 <= ratio <= 1.91 + 0.005:
        errors.append(f"{label}: aspect {w}x{h} ({ratio:.2f}) outside 4:5..1.91:1. "
                      f"Fix with tools/prep_image.py")
    if path.stat().st_size > 8 * 1024 * 1024:
        errors.append(f"{label}: larger than 8 MB")
    if w < 320 or w > 1440:
        warns.append(f"{label}: width {w}px; Instagram resizes to 320-1440 (1080 is ideal)")
    return ratio


def _probe(path: Path) -> dict | None:
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else {}


def _check_video(path: Path, errors: list[str], warns: list[str], label: str,
                 *, reel: bool, settings: Settings) -> None:
    if path.suffix.lower() not in VIDEO_EXT:
        errors.append(f"{label}: videos must be .mp4 or .mov")
        return
    size = path.stat().st_size
    if size > JSDELIVR_MAX_BYTES and settings.reel_upload == "url":
        warns.append(f"{label}: {size / 1e6:.0f} MB is over the free CDN's 20 MB limit; "
                     f"the publisher will try a direct upload. Smaller is safer.")
    info = _probe(path)
    if info is None:
        warns.append(f"{label}: ffprobe not installed, video specs not checked")
        return
    if not info:
        errors.append(f"{label}: unreadable video")
        return
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        errors.append(f"{label}: no video stream")
        return
    if v.get("codec_name") not in {"h264", "hevc"}:
        errors.append(f"{label}: codec {v.get('codec_name')}; use H.264 (tools/make_reel.py does)")
    if a is None:
        warns.append(f"{label}: no audio track; add music for reach (make_reel.py --music)")
    elif a.get("codec_name") != "aac":
        errors.append(f"{label}: audio must be AAC")
    dur = float(info.get("format", {}).get("duration") or 0)
    if not 3 <= dur <= 900:
        errors.append(f"{label}: duration {dur:.1f}s (API allows 3s-15min)")
    elif reel and dur > 90:
        warns.append(f"{label}: {dur:.0f}s; reels under ~30s get watched to the end more")
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if reel and h and abs(w / h - 9 / 16) > 0.02:
        warns.append(f"{label}: {w}x{h} is not 9:16; reels look best at 1080x1920")
    if v.get("pix_fmt") not in {"yuv420p", None}:
        errors.append(f"{label}: pixel format {v.get('pix_fmt')}; use yuv420p")


def validate(settings: Settings, posts: list[Post], *, now: datetime | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warns: list[str] = []
    now = now or datetime.now(timezone.utc)
    state = load_state()

    for post in posts:
        label = f"[{post.id}]"
        caption = post.full_caption(settings)
        for p in caption_problems(caption):
            errors.append(f"{label} {p}")
        if not post.caption.strip():
            warns.append(f"{label} empty caption")
        if post.type == "reel" and post.alt_text:
            warns.append(f"{label} alt_text only applies to images; ignored for reels")

        ratios = []
        for ref in post.media + ([post.cover] if post.cover else []):
            if is_url(ref):
                continue
            _, local = resolve_media(ref, settings)
            sub = f"{label} {ref}"
            if not local.exists():
                # a draft can be planned before its media exists
                (warns if post.draft else errors).append(
                    f"{sub}: file not found" + (" (draft, media still to add)" if post.draft else ""))
                continue
            if local.suffix.lower() in VIDEO_EXT:
                if post.type == "image":
                    errors.append(f"{sub}: an 'image' post can't be a video (use type: reel)")
                    continue
                _check_video(local, errors, warns, sub, reel=post.type == "reel",
                             settings=settings)
            else:
                if post.type == "reel" and ref != post.cover:
                    errors.append(f"{sub}: a reel needs a video file")
                    continue
                r = _check_image(local, errors, warns, sub)
                if r and ref != post.cover:
                    ratios.append(r)
        if post.type == "carousel" and len({round(r, 2) for r in ratios}) > 1:
            warns.append(f"{label} carousel images have different shapes; Instagram crops "
                         f"all of them to the first one's shape")

        entry = entry_for(post, state)
        if not post.draft and post.when <= now and entry.get("status") != "published":
            late_h = (now - post.when).total_seconds() / 3600
            if late_h > settings.max_lateness_hours:
                warns.append(f"{label} 'when' is {late_h:.0f}h in the past and will be skipped")
            else:
                warns.append(f"{label} is already due; it will go out on the next run")
    return errors, warns


def schedule_lines(settings: Settings, posts: list[Post], now: datetime) -> list[str]:
    state = load_state()
    lines = []
    for p in posts:
        status = "draft" if p.draft else entry_for(p, state).get("status") or (
            "due" if p.when <= now else "scheduled")
        local = p.when.astimezone(settings.tz)
        ist = p.when.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
        lines.append(f"{local:%a %d %b %H:%M %Z}  (IST {ist:%a %H:%M})  {p.type:<8} "
                     f"{status:<10} {p.id}")
    return lines
