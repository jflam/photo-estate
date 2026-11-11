# /// script
# dependencies = ["pillow"]
# ///
"""make_icons.py: draw the photo app's home-screen icons into ../app/ (run again after changing the design).

  uv run tools/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app"
BG, CARD, INK, SUN = (12, 12, 13), (28, 28, 31), (241, 241, 242), (255, 176, 92)


def draw(size: int, inset: float) -> Image.Image:
    """A photo print with hills and a low sun. `inset` leaves room for maskable crops (Android)."""
    S = 1024
    im = Image.new("RGB", (S, S), BG)
    d = ImageDraw.Draw(im)
    m = S * inset
    x0, y0, x1, y1 = m, m + S * .08, S - m, S - m - S * .08          # the print, a little wider than tall
    d.rounded_rectangle((x0, y0, x1, y1), radius=S * .05, fill=CARD)
    w, h = x1 - x0, y1 - y0
    p = lambda fx, fy: (x0 + w * fx, y0 + h * fy)
    sx, sy = p(.70, .33)
    r = w * .085
    d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=SUN)
    d.polygon([p(.09, .80), p(.36, .44), p(.55, .68), p(.66, .56), p(.91, .80)], fill=INK)
    return im.resize((size, size), Image.Resampling.LANCZOS)


OUT.mkdir(exist_ok=True)
for name, size, inset in (("icon-180.png", 180, .14), ("icon-192.png", 192, .14), ("icon-512.png", 512, .14),
                          ("icon-maskable-512.png", 512, .22)):
    draw(size, inset).save(OUT / name, optimize=True)
    print(OUT / name)
