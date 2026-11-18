# /// script
# dependencies = ["pillow"]
# ///
"""make_icons.py: draw the photo app's icons (run again after changing the design): the web app's
home-screen icons into ../app/, and the iPhone app's icon into ../../ios/PhotoEstate/Assets.xcassets.

  uv run tools/make_icons.py
"""
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app"
IOS = Path(__file__).resolve().parents[2] / "ios" / "PhotoEstate" / "Assets.xcassets"
PAPER, INK, SUN, HILL = (248, 246, 241), (27, 25, 21), (255, 246, 230), (232, 138, 46)
SKY = ((0, (255, 217, 163)), (.55, (255, 176, 97)), (1, (217, 115, 26)))     # top to bottom
# On a 1024 square: the print (a house), the picture inside it, and the hills along the picture's foot.
PRINT, PRINT_EDGE = [(512, 170), (850, 440), (850, 840), (174, 840), (174, 440)], 64
PICTURE, PICTURE_EDGE = [(512, 264), (774, 474), (774, 764), (250, 764), (250, 474)], 28
HILLS = [(140, 880), (140, 716), (404, 566), (528, 690), (616, 628), (884, 790), (884, 880)]
SUN_AT, SUN_R = (632, 506), 66


def draw(size: int, scale: float = 1.0) -> Image.Image:
    """A house-shaped photo print on a field of sun, with a night scene inside: hills and a low sun.
    `scale` shrinks the house about the centre, to leave room for maskable crops (Android)."""
    S = 2048                                                         # drawn large, then scaled down smooth
    k = S / 1024
    pt = lambda x, y: ((512 + (x - 512) * scale) * k, (512 + (y - 512) * scale) * k)

    def rounded(points, edge):
        """A mask of the polygon grown by half of `edge`, which rounds its corners."""
        mask = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(mask)
        ps, r = [pt(*q) for q in points], edge * scale * k / 2
        d.polygon(ps, fill=255)
        for (x, y), (x2, y2) in zip(ps, ps[1:] + ps[:1]):
            d.ellipse((x - r, y - r, x + r, y + r), fill=255)
            nx, ny = (y2 - y) * r / math.dist((x, y), (x2, y2)), (x - x2) * r / math.dist((x, y), (x2, y2))
            d.polygon([(x + nx, y + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x - nx, y - ny)], fill=255)
        return mask

    column = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        (t0, c0), (t1, c1) = next(pair for pair in zip(SKY, SKY[1:]) if t <= pair[1][0])
        f = (t - t0) / (t1 - t0)
        column.putpixel((0, y), tuple(round(a + (b - a) * f) for a, b in zip(c0, c1)))
    im = column.resize((S, S))
    im.paste(PAPER, mask=rounded(PRINT, PRINT_EDGE))
    scene = Image.new("RGB", (S, S), INK)
    d = ImageDraw.Draw(scene)
    (sx, sy), r = pt(*SUN_AT), SUN_R * scale * k
    d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=SUN)
    d.polygon([pt(*q) for q in HILLS], fill=HILL)
    im.paste(scene, mask=rounded(PICTURE, PICTURE_EDGE))
    return im.resize((size, size), Image.Resampling.LANCZOS)


OUT.mkdir(exist_ok=True)
for name, size, scale in (("icon-180.png", 180, 1), ("icon-192.png", 192, 1), ("icon-512.png", 512, 1),
                          ("icon-maskable-512.png", 512, .76)):
    draw(size, scale).save(OUT / name, optimize=True)
    print(OUT / name)

# The iPhone app: one 1024 px square, no transparency (iOS rounds the corners and makes the smaller sizes).
icon = IOS / "AppIcon.appiconset"
icon.mkdir(parents=True, exist_ok=True)
draw(1024).save(icon / "icon-1024.png", optimize=True)
(icon / "Contents.json").write_text(json.dumps({
    "images": [{"filename": "icon-1024.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}],
    "info": {"author": "xcode", "version": 1}}, indent=2) + "\n")
(IOS / "Contents.json").write_text(json.dumps({"info": {"author": "xcode", "version": 1}}, indent=2) + "\n")
print(icon / "icon-1024.png")
