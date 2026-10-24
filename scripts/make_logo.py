"""Make the transparent logo files from the original in docs/brand/ (white background removed).
Run: python3 scripts/make_logo.py   (needs Pillow; the app itself does not)."""
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs/brand/find-fund-operate-stool.webp"
OUT = ROOT / "bluemoon/web/static/brand"


def main() -> None:
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
