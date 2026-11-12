#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["requests", "pillow>=10.0", "timezonefinder"]
# ///
"""
make_demo.py — build the demo estate in demo/ from ~100 CC0 photos on Wikimedia Commons.

  uv run tools/make_demo.py            # download (cached), downscale, run the whole pipeline
  uv run embed.py serve --demo         # then browse it

demo/photos.json lists the photos by Commons file name, grouped by what they show. The groups exist
to show off search: each holds several photos that look alike (eight lighthouses, eight sunsets
over the sea...), so "More like this" and descriptions like "red lighthouse" have real neighbours to
find. The groups are not stored anywhere in the catalog; search has to find them from the pixels.

The photos are Unsplash photos that were copied to Commons while Unsplash was CC0 (before mid-2017):
far better than most of Commons. Each has camera EXIF and a date; most have coordinates.
Each is fetched as a Commons thumbnail (originals are rate-limited hard), downscaled to 1600 px on
the long side, given back the camera EXIF Commons read from the original, then laid out as a Google Takeout
folder with a JSON sidecar (title, Commons description, UTC time, coordinates) and run through the
same steps as a real source: ingest -> link -> exif -> places -> embed run. A few go in albums too,
as Takeout does, which also exercises duplicate collapsing.

Output (committed): demo/store/ (content-addressed photos), demo/estate.db, demo/CREDITS.md.
Downloads are cached in ~/.cache/photo-estate-demo so a rebuild doesn't download them again.
Needs exiftool, like `estate.py exif`.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image

HERE = Path(__file__).resolve().parent.parent
DEMO = HERE / "demo"
API = "https://commons.wikimedia.org/w/api.php"
SIDE = 1600
# Takeout albums: the photo also appears in this folder, as Takeout puts album photos in both places
ALBUMS = {"Winter": ("Snow", "Snowy", "snowy", "Winter Dog"),
          "Golden hour": ("Golden Hour", "sunset", "Sunset", "Dusk")}
FAVOURITES = {"Lighthouse drone view (Unsplash).jpg", "Dew-covered tulip (Unsplash).jpg", "Snuggly kittens (Unsplash).jpg",
              "Temples and hot air balloons. (Unsplash).jpg", "Two Hands Cafe latte art (Unsplash).jpg",
              "Snow covered mountain peaks in Moraine Lake (Unsplash).jpg"}

S = requests.Session()
S.headers["User-Agent"] = "photo-estate-demo-builder/0.1 (builds a small public-domain demo catalog)"


def get(url: str, **kw) -> requests.Response:
    for i in range(10):  # Commons answers bursts with 429; its Retry-After of 1 s is too short to help
        r = S.get(url, timeout=120, **kw)
        if r.status_code == 200:
            return r
        if r.status_code not in (429, 500, 502, 503, 504):
            r.raise_for_status()
        wait = max(int(r.headers.get("retry-after") or 0), min(300, 15 * 2 ** i))
        print(f"    HTTP {r.status_code}, retrying in {wait}s", flush=True)
        time.sleep(wait)
    r.raise_for_status()


def plain(s: str | None) -> str:
    """Commons descriptions are HTML, sometimes in several languages; keep the first readable text."""
    s = re.sub(r"<(script|style)\b.*?</\1>", "", s or "", flags=re.S)
    s = re.sub(r"<div[^>]*lang=\"(?!en)[^\"]*\".*?</div>", "", s, flags=re.S)  # other-language blocks
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def info(titles: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(titles), 20):
        batch = titles[i:i + 20]
        r = get(API, params=dict(action="query", format="json", prop="imageinfo|coordinates",
                                 titles="|".join(f"File:{t}" for t in batch), iiprop="url|size|metadata|extmetadata",
                                 iimetadataversion="latest", colimit="max",
                                 iiextmetadatafilter="ImageDescription|Artist|LicenseShortName|LicenseUrl|Credit")).json()
        norm = {n["to"]: n["from"] for n in r["query"].get("normalized", [])}
        for p in r["query"]["pages"].values():
            if "imageinfo" not in p:
                sys.exit(f"not on Commons: {p.get('title')}")
            ii = p["imageinfo"][0]
            md = {m["name"]: m["value"] for m in ii.get("metadata") or []}
            em = {k: v.get("value") for k, v in (ii.get("extmetadata") or {}).items()}
            title = norm.get(p["title"], p["title"]).removeprefix("File:")
            c = (p.get("coordinates") or [{}])[0]
            out[title] = {"url": ii["url"].split("?")[0], "w": ii["width"], "h": ii["height"], "md": md, "page": ii["descriptionurl"], "lat": c.get("lat"), "lon": c.get("lon"),
                          "taken": str(md.get("DateTimeOriginal") or ""), "desc": plain(em.get("ImageDescription")),
                          "artist": plain(em.get("Artist")) or plain(em.get("Credit")) or "unknown",
                          "license": em.get("LicenseShortName") or "", "license_url": em.get("LicenseUrl") or ""}
    return out


def thumb_url(m: dict) -> str:
    """A standard-width Commons thumbnail at least SIDE px on its long side. Originals are rate-limited
    hard (429 with a 10-minute Retry-After after a handful); thumbnails at the standard widths aren't."""
    w, h = m["w"], m["h"]
    fits = [tw for tw in (960, 1280, 1920, 3840) if tw < w]
    tw = next((tw for tw in fits if max(tw, h * tw / w) >= SIDE), fits[-1] if fits else None)
    if tw is None:
        return m["url"]
    base, name = m["url"].rsplit("/", 1)
    return f"{base.replace('/commons/', '/commons/thumb/', 1)}/{name}/{tw}px-{name}"


def camera_exif(md: dict) -> Image.Exif:
    """Thumbnails have no EXIF; put back the camera tags Commons read from the original."""
    from fractions import Fraction
    from PIL.TiffImagePlugin import IFDRational

    def rational(v):
        f = Fraction(str(v)).limit_denominator(100000)
        return IFDRational(f.numerator, f.denominator)
    exif = Image.Exif()
    for tag, key in ((271, "Make"), (272, "Model"), (305, "Software")):
        if md.get(key):
            exif[tag] = str(md[key])
    ifd = exif.get_ifd(0x8769)
    for tag, key, conv in ((36867, "DateTimeOriginal", str), (36881, "OffsetTimeOriginal", str),
                           (33434, "ExposureTime", rational), (33437, "FNumber", rational),
                           (37386, "FocalLength", rational), (41989, "FocalLengthIn35mmFilm", int),
                           (34855, "ISOSpeedRatings", int), (42036, "LensModel", str)):
        v = md.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):  # some values come back as {"_type": ..., "0": value}
            v = next((x for k, x in v.items() if k != "_type"), None)
        if v not in (None, ""):
            try:
                ifd[tag] = conv(v)
            except (ValueError, ZeroDivisionError, TypeError):
                pass
    return exif


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cache", default=str(Path.home() / ".cache" / "photo-estate-demo"), help="images downloaded from Commons")
    p.add_argument("--out", default=str(DEMO), help="where store/, estate.db and CREDITS.md go")
    a = p.parse_args()
    if not shutil.which("exiftool"):
        sys.exit("needs exiftool (brew install exiftool / pacman -S perl-image-exiftool)")
    groups = json.loads((DEMO / "photos.json").read_text())
    titles = [t for g in groups.values() for t in g]
    cache, out = Path(a.cache), Path(a.out)
    cache.mkdir(parents=True, exist_ok=True)

    print(f"{len(titles)} photos: reading Commons metadata", flush=True)
    meta = info(titles)
    for t, m in meta.items():
        if not re.match(r"(CC0|Public domain|PD)", m["license"], re.I):
            sys.exit(f"{t}: license {m['license']!r} is not public domain or CC0")
        if not m["taken"][:4].isdigit():
            sys.exit(f"{t}: needs an EXIF date")
    from timezonefinder import TimezoneFinder
    tf = TimezoneFinder()

    work = Path(tempfile.mkdtemp(prefix="photo-estate-demo-"))
    src = work / "Takeout"
    try:
        for n, t in enumerate(titles, 1):
            m = meta[t]
            got = cache / ("thumb-" + re.sub(r"[/\\]", "_", t))
            if not got.exists():
                print(f"  [{n}/{len(titles)}] downloading {t}", flush=True)
                got.write_bytes(get(thumb_url(m)).content)
                time.sleep(4)  # be gentle with upload.wikimedia.org
            with Image.open(got) as im:  # thumbnails are already upright
                icc = im.info.get("icc_profile")
                im = im.convert("RGB")
                im.thumbnail((SIDE, SIDE), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=82, optimize=True, progressive=True, exif=camera_exif(m["md"]),
                        **({"icc_profile": icc} if icc else {}))
            # Takeout sidecar times are UTC; EXIF is wall-clock time where the photo was taken
            local = datetime.strptime(m["taken"][:19], "%Y:%m:%d %H:%M:%S")
            zone = tf.timezone_at(lat=m["lat"], lng=m["lon"]) if m["lat"] is not None else None
            utc = local.replace(tzinfo=ZoneInfo(zone) if zone else timezone.utc).astimezone(timezone.utc)
            name = Path(t).stem + ".jpg"
            sidecar = {"title": name, "description": m["desc"][:280],
                       "photoTakenTime": {"timestamp": str(int(utc.timestamp()))},
                       "geoData": {"latitude": m["lat"] or 0.0, "longitude": m["lon"] or 0.0, "altitude": 0.0},  # 0,0 = none
                       "favorited": t in FAVOURITES, "url": m["page"]}
            dirs = [f"Google Photos/Photos from {local.year}"]
            dirs += [f"Google Photos/{album}" for album, keys in ALBUMS.items()
                     if any(k in t for k in ([keys] if isinstance(keys, str) else keys))]
            for d in dirs:
                (src / d).mkdir(parents=True, exist_ok=True)
                (src / d / name).write_bytes(buf.getvalue())
                (src / d / f"{name}.supplemental-metadata.json").write_text(json.dumps(sidecar, ensure_ascii=False))

        for d in ("store", "review"):
            shutil.rmtree(out / d, ignore_errors=True)
        db = work / "estate.db"
        store = out / "store"
        steps = [["estate.py", "--db", db, "ingest", "--store", store, src],
                 ["estate.py", "--db", db, "link"],
                 ["estate.py", "--db", db, "exif", "--store", store],
                 ["places.py", "--db", db, "run"],
                 ["embed.py", "--db", db, "run", "--store", store]]
        for s in steps:
            print("$", " ".join(map(str, s)), flush=True)
            subprocess.run(["uv", "run", "-q", str(HERE / s[0]), *map(str, s[1:])], check=True, cwd=HERE)
        shutil.rmtree(store / ".tmp", ignore_errors=True)

        # one self-contained file: no WAL beside it, no free pages
        con = sqlite3.connect(db)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
        con.close()
        shutil.copyfile(db, out / "estate.db")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    lines = ["# Demo photo credits", "",
             "The photos in `store/` are downscaled copies of these files on Wikimedia Commons. Each is",
             "public domain or CC0, so no attribution is required; it is given anyway.", "",
             "| Photo | Author | License |", "|---|---|---|"]
    for g, ts in groups.items():
        for t in ts:
            m = meta[t]
            lic = f"[{m['license']}]({m['license_url']})" if m["license_url"] else m["license"]
            lines.append(f"| [{t}]({m['page']}) | {m['artist'].replace('|', '/')[:80]} | {lic} |")
    (out / "CREDITS.md").write_text("\n".join(lines) + "\n")
    size = sum(f.stat().st_size for f in (out / "store").rglob("*") if f.is_file())
    print(f"demo built: {out}/estate.db ({(out / 'estate.db').stat().st_size / 1e6:.1f} MB), "
          f"store {size / 1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
