#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pillow>=11.3", "pillow-heif>=0.18", "numpy", "scipy"]
# ///
"""thumb_formats.py: what a grid thumbnail costs in each image format, on a sample of a library's photos.

  uv run tools/thumb_formats.py --db ~/estate/estate.db --store /Volumes/estate/store --sample 300
  uv run tools/thumb_formats.py --db ... --store ... --sample 300 --side 1600     # the viewer's previews

Each sampled photo is decoded as the app decodes it (draft, exif_transpose, RGB, thumbnail to --side) and
kept uncompressed as the reference. It is then encoded as the app's JPEG (quality 82; 90 above 2000 px)
with optimised Huffman tables (the same pixels), progressive, and as WebP and AVIF at several
qualities. For each encoding it measures the bytes, the time to encode and
to decode, and how close the result is to the reference: SSIM on luminance (1.0 is identical), and the
mean of the worst 1% of SSIM windows, which shows a visible artefact in a small part of a photo that the
average hides. The report gives each format's median and total, and which quality of each format matches
the JPEG's closeness.

It also times what the viewer waits for today: decoding the original from the store (the first open of a
photo, with no preview cached), against reading a cached preview. The catalog is opened read-only and
nothing is written to the store or a cache. --out DIR also writes each encoding of the first few photos
there, for looking at side by side.
"""
from __future__ import annotations

import argparse
import io
import random
import sqlite3
import statistics
import time
from pathlib import Path

import numpy as np
import pillow_heif
from PIL import Image, ImageOps
from scipy.ndimage import gaussian_filter

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

IMAGE_EXT = (".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".tif", ".tiff")


def luma(im: Image.Image) -> np.ndarray:
    return np.asarray(im.convert("L"), dtype=np.float64)


def ssim_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """SSIM per pixel (Wang et al. 2004: Gaussian window, sigma 1.5, K1 0.01, K2 0.03, 8-bit range)."""
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = lambda x: gaussian_filter(x, 1.5, truncate=3.5)
    ma, mb = g(a), g(b)
    va, vb, cov = g(a * a) - ma * ma, g(b * b) - mb * mb, g(a * b) - ma * mb
    return ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))


def closeness(ref: np.ndarray, data: bytes) -> tuple[float, float, float]:
    """(SSIM, mean of the worst 1% of the map, ms to decode)."""
    t = time.perf_counter()
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        dec = (time.perf_counter() - t) * 1000
        m = ssim_map(ref, luma(im))
    worst = np.sort(m, axis=None)[: max(1, m.size // 100)]
    return float(m.mean()), float(worst.mean()), dec


def encode(im: Image.Image, fmt: str, q: int) -> tuple[bytes, float]:
    buf, t = io.BytesIO(), time.perf_counter()
    if fmt == "JPEG":
        im.save(buf, "JPEG", quality=q)
    elif fmt == "JPEG-OPT":         # the same pixels, smaller Huffman tables
        im.save(buf, "JPEG", quality=q, optimize=True)
    elif fmt == "JPEG-PROG":
        im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
    elif fmt == "WEBP":
        im.save(buf, "WEBP", quality=q, method=6)
    else:
        im.save(buf, "AVIF", quality=q, speed=6)
    return buf.getvalue(), (time.perf_counter() - t) * 1000


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--sample", type=int, default=300)
    p.add_argument("--side", type=int, default=360)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--cache", help="a thumbnail cache folder (with previews/) to time reading cached previews from")
    p.add_argument("--out", help="write each encoding of the first 6 photos here")
    p.add_argument("--try", dest="tries", help="the encodings to try besides the app's JPEG, e.g. WEBP:76,WEBP:78"
                   " (default: optimised and progressive JPEG, WebP 60-85, AVIF 45-70)")
    a = p.parse_args()

    con = sqlite3.connect(f"file:{Path(a.db).expanduser()}?mode=ro", uri=True)
    rows = con.execute("SELECT sha256, ext FROM assets WHERE kind='image'").fetchall()
    con.close()
    rows = [r for r in rows if r[1].lower() in IMAGE_EXT]
    random.Random(a.seed).shuffle(rows)
    store = Path(a.store).expanduser()
    jq = 90 if a.side > 2000 else 82
    grid = [("JPEG", jq)] + ([(f, int(q)) for f, q in (x.split(":") for x in a.tries.split(","))] if a.tries else
                            [("JPEG-OPT", jq), ("JPEG-PROG", jq), ("JPEG-PROG", 75)] + [("WEBP", q) for q in (60, 70, 75, 80, 85)]
                            + [("AVIF", q) for q in (45, 55, 60, 65, 70)])
    res: dict[tuple[str, int], list[tuple[int, float, float, float, float]]] = {g: [] for g in grid}
    opens, exts = [], {}
    out = Path(a.out).expanduser() if a.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    done = 0
    for sha, ext in rows:
        if done >= a.sample:
            break
        src = store / sha[:2] / sha[2:4] / f"{sha}{ext}"
        try:
            t = time.perf_counter()
            with Image.open(src) as im:
                im.draft("RGB", (a.side, a.side))
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((a.side, a.side))
            opens.append((time.perf_counter() - t) * 1000)
        except Exception as e:
            print(f"skip {sha[:12]}{ext}: {type(e).__name__}: {e}", flush=True)
            continue
        exts[ext.lower()] = exts.get(ext.lower(), 0) + 1
        ref = luma(im)
        for fmt, q in grid:
            data, enc = encode(im, fmt, q)
            s, worst, dec = closeness(ref, data)
            res[(fmt, q)].append((len(data), s, worst, enc, dec))
            if out and done < 6:
                (out / f"{done}-{sha[:8]}-{fmt.lower()}{q}.{'jpg' if fmt.startswith('JPEG') else fmt.lower()}").write_bytes(data)
        if out and done < 6:
            im.save(out / f"{done}-{sha[:8]}-reference.png")
        done += 1
        if done % 50 == 0:
            print(f"{done} photos", flush=True)

    med = statistics.median
    base = res[("JPEG", jq)]
    jb, js, jw = sum(r[0] for r in base), med(r[1] for r in base), med(r[2] for r in base)
    print(f"\n{done} photos at {a.side} px ({', '.join(f'{k} {v}' for k, v in sorted(exts.items()))})\n")
    print("| Format | Median bytes | Total vs JPEG | SSIM (median) | Worst 1% (median) | Encode ms | Decode ms |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for (fmt, q), rs in res.items():
        b = sum(r[0] for r in rs)
        print(f"| {fmt} {q} | {med(r[0] for r in rs):,.0f} | {b / jb:.0%} | {med(r[1] for r in rs):.4f} |"
              f" {med(r[2] for r in rs):.4f} | {med(r[3] for r in rs):.1f} | {med(r[4] for r in rs):.2f} |")
    for fmt in ("WEBP", "AVIF"):
        ok = [(q, rs) for (f, q), rs in res.items() if f == fmt
              and med(r[1] for r in rs) >= js and med(r[2] for r in rs) >= jw]
        if ok:
            q, rs = min(ok, key=lambda x: x[0])
            print(f"\nLowest {fmt} quality at least as close as JPEG {jq} on both measures: {q},"
                  f" {sum(r[0] for r in rs) / jb:.0%} of the JPEG's bytes")
        else:
            print(f"\nNo {fmt} quality tried is as close as JPEG {jq} on both measures")
    print(f"\nDecoding the original to {a.side} px (what a first open waits for): median {med(opens):.0f} ms,"
          f" 90th percentile {sorted(opens)[int(len(opens) * 0.9)]:.0f} ms")
    if a.cache:
        cached = sorted((Path(a.cache).expanduser() / "previews").glob("*.jpg"))[: a.sample]
        ts = []
        for f in cached:
            t = time.perf_counter()
            f.read_bytes()
            ts.append((time.perf_counter() - t) * 1000)
        if ts:
            print(f"Reading a cached preview ({len(ts)} files): median {med(ts):.2f} ms")


if __name__ == "__main__":
    main()
