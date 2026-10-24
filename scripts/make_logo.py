"""Make the transparent logo files from the original in docs/brand/ (white background removed).
Run: python3 scripts/make_logo.py   (needs Pillow; the app itself does not)."""
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs/brand/find-fund-operate-stool.webp"
MOON = ROOT / "docs/brand/moon-photo.webp"   # the bright blue moon Seth chose (cropped to the disc)
OUT = ROOT / "bluemoon/web/static/brand"


def bright_moon(im: Image.Image) -> Image.Image:
    """The stool's old dull moon is swapped for Seth's brighter blue one: stool cut out by colour (wood and
    cream letters are warm, the moon is blue), new moon cropped to its disc and put behind it."""
    import numpy as np
    a = np.asarray(im.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    moonlike = (b >= r - 2) & (r + g + b > 150)          # the old blue-grey moon (wood, shadows and cream letters are warm)
    white = np.minimum(np.minimum(r, g), b) >= 236
    keep = ~(moonlike | white)
    stool = Image.fromarray((keep * 255).astype("uint8"))
    stool = stool.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))      # close small holes
    stool = stool.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.GaussianBlur(0.8))
    k = np.asarray(stool) > 128                              # keep only the big connected piece (the stool)
    lab = np.zeros(k.shape, dtype=int)
    from collections import deque
    best, n = [], 0
    for y0, x0 in zip(*np.where(k)):
        if lab[y0, x0]:
            continue
        n += 1
        q, cells = deque([(y0, x0)]), []
        lab[y0, x0] = n
        while q:
            y1, x1 = q.popleft(); cells.append((y1, x1))
            for yy, xx in ((y1 + 1, x1), (y1 - 1, x1), (y1, x1 + 1), (y1, x1 - 1)):
                if 0 <= yy < k.shape[0] and 0 <= xx < k.shape[1] and k[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = n; q.append((yy, xx))
        if len(cells) > len(best):
            best = cells
    only = np.zeros(k.shape, dtype=bool)
    for y1, x1 in best:
        only[y1, x1] = True
    stool = Image.fromarray(np.where(only, np.asarray(stool), 0).astype("uint8"))
    w, h = im.size
    moon = Image.open(MOON).convert("RGB")
    m = np.asarray(moon).astype(int)
    lit = (m[..., 2] > 215) & (m[..., 2] > m[..., 0] + 25)            # the bright disc itself, not its dark-blue halo or the sky
    ys, xs = np.where(lit)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    rad = min(xs.max() - xs.min(), ys.max() - ys.min()) / 2 - 2
    box = (int(cx - rad), int(cy - rad), int(cx + rad), int(cy + rad))
    disc = moon.crop(box).convert("RGBA")
    d = disc.width
    circle = Image.new("L", (d * 4, d * 4), 0)
    from PIL import ImageDraw
    ImageDraw.Draw(circle).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    disc.putalpha(circle.resize((d, d), Image.LANCZOS))
    target = 620                                            # the old moon was about this wide, centred behind the stool
    disc = disc.resize((target, target), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(disc, (w // 2 - target // 2 + 4, 45), disc)
    fg = im.convert("RGBA")
    fg.putalpha(stool)
    canvas.alpha_composite(fg)
    return canvas


def main() -> None:
    if MOON.exists():
        out = bright_moon(Image.open(SRC))
        box = out.getbbox()
        out = out.crop(box)
        pad = 6
        canvas = Image.new("RGBA", (out.width + 2 * pad, out.height + 2 * pad), (0, 0, 0, 0))
        canvas.paste(out, (pad, pad))
        save_all(canvas)
        return
    im = Image.open(SRC).convert("RGB")
    w, h = im.size
    px = im.load()
    near_white = lambda p: min(p) >= 232
    bg = bytearray(w * h)
    q = deque()
    for x in range(w):
        q.append((x, 0)); q.append((x, h - 1))
    for y in range(h):
        q.append((0, y)); q.append((w - 1, y))
    while q:
        x, y = q.popleft()
        if not (0 <= x < w and 0 <= y < h) or bg[y * w + x] or not near_white(px[x, y]):
            continue
        bg[y * w + x] = 1
        q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    # White also shows through between the stool's rungs (not connected to the border): clear it too.
    # Cream letters and the moon are never this white (their lowest channel stays well below).
    for y in range(h):
        for x in range(w):
            if not bg[y * w + x] and min(px[x, y]) >= 238:
                bg[y * w + x] = 1
    mask = Image.new("L", (w, h))
    mask.putdata([0 if b else 255 for b in bg])
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.1))
    out = im.convert("RGBA")
    out.putalpha(mask)
    box = out.getbbox()
    out = out.crop(box)
    pad = 6
    canvas = Image.new("RGBA", (out.width + 2 * pad, out.height + 2 * pad), (0, 0, 0, 0))
    canvas.paste(out, (pad, pad))
    save_all(canvas)


def save_all(canvas: Image.Image) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT / "stool.webp", "WEBP", quality=90, method=6)       # full size, transparent
    for name, height in (("stool-120.webp", 120), ("stool-320.webp", 320)):
        small = canvas.resize((round(canvas.width * height / canvas.height), height), Image.LANCZOS)
        small.save(OUT / name, "WEBP", quality=90, method=6)
    icon = canvas.resize((round(canvas.width * 64 / canvas.height), 64), Image.LANCZOS)
    sq = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    sq.paste(icon, ((64 - icon.width) // 2, 0))
    sq.save(OUT / "stool-64.png")


if __name__ == "__main__":
    main()
