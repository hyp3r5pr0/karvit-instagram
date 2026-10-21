"""Shared drawing helpers for prep_image.py and make_reel.py."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"

BRAND = "KARVIT"


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Prefer a font you drop into assets/fonts (first .ttf/.otf found with
    'bold' in the name for bold text), else the bundled DejaVu."""
    custom = sorted(p for p in FONT_DIR.glob("*") if p.suffix.lower() in {".ttf", ".otf"}
                    and not p.name.startswith("DejaVu"))
    pick = None
    if custom:
        bolds = [p for p in custom if "bold" in p.name.lower()]
        regs = [p for p in custom if "bold" not in p.name.lower()]
        pick = (bolds or custom)[0] if bold else (regs or custom)[0]
    if pick is None:
        pick = FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    return ImageFont.truetype(str(pick), size)


def load_rgb(path: str | Path) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (245, 243, 238, 255))
        bg.alpha_composite(im)
        im = bg
    return im.convert("RGB")


def cover(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(im, size, Image.LANCZOS)


def contain(im: Image.Image, box: tuple[int, int]) -> Image.Image:
    im = im.copy()
    im.thumbnail(box, Image.LANCZOS)
    return im


def blurred_backdrop(im: Image.Image, size: tuple[int, int], darken: float = 0.55,
                     solid: tuple[int, int, int] | None = None) -> Image.Image:
    if solid:
        return Image.new("RGB", size, solid)
    bg = cover(im, (size[0] // 4, size[1] // 4)).filter(ImageFilter.GaussianBlur(12))
    bg = bg.resize(size, Image.LANCZOS)
    return Image.blend(bg, Image.new("RGB", size, (0, 0, 0)), 1 - darken)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words, line = para.split(), ""
        for w in words:
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=fnt) <= max_w or not line:
                line = trial
            else:
                lines.append(line)
                line = w
        lines.append(line)
    return lines


def text_block(canvas: Image.Image, text: str, *, center_y: int, size: int = 72,
               max_w: int = 900, pill: bool = True, fill=(255, 255, 255),
               pill_rgba=(0, 0, 0, 150), bold: bool = True) -> None:
    """Centered, wrapped text with a soft rounded backing for legibility."""
    if not text:
        return
    fnt = font(size, bold)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lines = wrap(d, text, fnt, max_w)
    lh = int(size * 1.22)
    total_h = lh * len(lines)
    top = center_y - total_h // 2
    widest = max(d.textlength(l, font=fnt) for l in lines)
    if pill:
        pad_x, pad_y = int(size * 0.55), int(size * 0.35)
        x0 = (canvas.width - widest) / 2 - pad_x
        d.rounded_rectangle([x0, top - pad_y, x0 + widest + 2 * pad_x, top + total_h + pad_y],
                            radius=int(size * 0.45), fill=pill_rgba)
    for i, l in enumerate(lines):
        w = d.textlength(l, font=fnt)
        x = (canvas.width - w) / 2
        y = top + i * lh + (lh - size) // 2 - int(size * 0.08)
        if not pill:
            d.text((x + 3, y + 3), l, font=fnt, fill=(0, 0, 0, 160))
        d.text((x, y), l, font=fnt, fill=fill)
    base = canvas.convert("RGBA")
    base.alpha_composite(layer)
    canvas.paste(base.convert("RGB"))


def wordmark(canvas: Image.Image, *, y: int, size: int = 40, text: str = BRAND) -> None:
    fnt = font(size, True)
    d = ImageDraw.Draw(canvas)
    spaced = " ".join(text)
    w = d.textlength(spaced, font=fnt)
    d.text(((canvas.width - w) / 2, y), spaced, font=fnt, fill=(255, 255, 255))


def drop_shadow(canvas: Image.Image, fg: Image.Image, pos: tuple[int, int],
                radius: int = 28, offset: int = 14) -> None:
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    box = Image.new("RGBA", fg.size, (0, 0, 0, 140))
    shadow.paste(box, (pos[0] + offset // 2, pos[1] + offset))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    base = canvas.convert("RGBA")
    base.alpha_composite(shadow)
    base.paste(fg, pos)
    canvas.paste(base.convert("RGB"))
