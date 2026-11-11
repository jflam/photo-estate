#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10.0"]
# ///
"""End-to-end test: build a synthetic Google Takeout with the real-world quirks, ingest it in two
rolling batches, link sidecars, check de-dupe, tag through a mock OpenAI-compatible VLM, search."""
import base64, io, json, random, shutil, sqlite3, subprocess, sys, tempfile, threading, zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageEnhance

HERE = Path(__file__).resolve().parent
ESTATE = HERE.parent / "estate.py"
PLACES = HERE.parent / "places.py"
GP = "Takeout/Google Photos"


def photo(seed, date, size=(1200, 900)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size, tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(25):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        d.ellipse([x, y, x + rnd.randrange(80, 400), y + rnd.randrange(80, 400)],
                  fill=tuple(rnd.randrange(256) for _ in range(3)))
    return im, date


def jpeg(im, date=None, quality=92, camera=None):
    buf = io.BytesIO()
    exif = Image.Exif()
    if date:
        exif.get_ifd(0x8769)[36867] = date
    if camera:  # (make, model, exposure s, f-number, iso)
        exif[271], exif[272] = camera[0], camera[1]
        ifd = exif.get_ifd(0x8769)
        ifd[33434], ifd[33437], ifd[34855] = camera[2], camera[3], camera[4]
    im.save(buf, "JPEG", quality=quality, exif=exif)
    return buf.getvalue()


def sidecar(title, ts, lat=0.0, lon=0.0, desc="", people=(), fav=False):
    return json.dumps({"title": title, "description": desc,
                       "photoTakenTime": {"timestamp": str(ts)},
                       "geoData": {"latitude": lat, "longitude": lon, "altitude": 0.0},
                       "people": [{"name": p} for p in people], "favorited": fav,
                       "url": "https://photos.google.com/photo/x"})


def build_takeout(out: Path):
    p1, _ = photo(1, "2019:07:14 10:22:33")
    p2, _ = photo(2, "2019:08:01 09:00:00")
    p2b, _ = photo(22, "2019:08:01 09:00:05")
    p4, _ = photo(4, "2020:01:02 12:00:00")
    p5, _ = photo(5, "2021:05:05 18:30:00")
    pxl, _ = photo(6, "2020:07:04 18:30:15")
    j1, j2, j2b, j4, j5, jpxl = (jpeg(p1, "2019:07:14 10:22:33"), jpeg(p2), jpeg(p2b),
                                 jpeg(p4, "2020:01:02 12:00:00"), jpeg(p5), jpeg(pxl))
    w, h = p1.size
    edited = ImageEnhance.Brightness(p1.crop((20, 15, w - 20, h - 15))).enhance(1.12)
    small5 = p5.resize((600, 450))
    long = "PXL_20200704_183015123.PORTRAIT.ORIGINAL_EXPORTED_FROM_CAMERA.jpg"   # 63 chars
    trunc = (long + ".supplemental-metadata")[:46] + ".json"                    # Google cuts at 51

    z1 = {
        f"{GP}/Photos from 2019/IMG_0001.JPG": j1,
        f"{GP}/Photos from 2019/IMG_0001.JPG.supplemental-metadata.json":
            sidecar("IMG_0001.JPG", 1563099753, 48.8584, 2.2945, fav=True),
        f"{GP}/Photos from 2019/IMG_0001-edited.JPG": jpeg(edited, "2019:07:14 10:22:33"),
        f"{GP}/Photos from 2019/IMG_0002.JPG": j2,
        f"{GP}/Photos from 2019/IMG_0002.JPG.json": sidecar("IMG_0002.JPG", 1564650000, desc="grandma's garden"),
        f"{GP}/Photos from 2019/IMG_0002(1).JPG": j2b,
        f"{GP}/Photos from 2019/IMG_0002.JPG.supplemental-metadata(1).json": sidecar("IMG_0002.JPG", 1564650005),
        f"{GP}/Paris Trip/IMG_0001.JPG": j1,
        f"{GP}/Paris Trip/IMG_0001.JPG.supplemental-metadata.json": sidecar("IMG_0001.JPG", 1563099753, 48.8584, 2.2945),
        f"{GP}/Paris Trip/metadata.json": json.dumps({"title": "Paris Trip 🇫🇷", "description": ""}),
        f"{GP}/Photos from 2020/{long}": jpxl,
        f"{GP}/Photos from 2020/{trunc}": sidecar(long, 1593887415),
        f"{GP}/Photos from 2020/VID_0003.MP4": random.Random(3).randbytes(50_000),
        f"{GP}/Photos from 2020/VID_0003.MP4.supplemental-metadata.json": sidecar("VID_0003.MP4", 1580000000),
        f"{GP}/Photos from 2020/IMG_0004.JPG": j4,          # its sidecar is in zip 2
        "Takeout/archive_browser.html": b"<html></html>",
        f"{GP}/print-subscriptions.json": b"[]",
    }
    z2 = {
        f"{GP}/Photos from 2020/IMG_0004.JPG.supplemental-metadata.json": sidecar("IMG_0004.JPG", 1577966400),
        f"{GP}/Photos from 2021/IMG_0005.JPG": j5,
        f"{GP}/Photos from 2021/IMG_0005.JPG.supplemental-metadata.json":
            sidecar("IMG_0005.JPG", 1620239400, people=["Alice", "Bob"]),
        f"{GP}/Photos from 2021/IMG-20210505-WA0007.jpg": jpeg(small5, quality=60),
        f"{GP}/Photos from 2021/IMG-20210505-WA0007.jpg.supplemental-metadata.json":
            sidecar("IMG-20210505-WA0007.jpg", 1620239460),
        f"{GP}/Family/IMG_0002.JPG": j2,                    # exact dup across zips
        f"{GP}/Family/IMG_0002.JPG.supplemental-metadata.json": sidecar("IMG_0002.JPG", 1564650000),
    }
    paths = []
    for i, files in enumerate((z1, z2), 1):
        zp = out / f"takeout-20261108T000000Z-00{i}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        paths.append(zp)
    return paths


class MockVLM(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /v1/chat/completions that checks the image actually arrives."""
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        url = body["messages"][0]["content"][1]["image_url"]["url"]
        im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        assert max(im.size) <= 768, im.size
        r, g, b = im.convert("RGB").resize((1, 1)).getpixel((0, 0))
        color = max((("red", r), ("green", g), ("blue", b)), key=lambda t: t[1])[0]
        content = "```json\n" + json.dumps({"caption": f"Abstract {color} circles on a canvas.",
                                            "tags": ["Circles", "abstract", color], "scene": "unknown",
                                            "people_count": 0, "text": ""}) + "\n```"
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def run(*args, script=ESTATE):
    r = subprocess.run(["uv", "run", "--quiet", "--script", str(script), *args],
                       capture_output=True, text=True)
    print(f"$ estate.py {' '.join(args[2:]) if args[0] == '--db' else ' '.join(args)}\n{r.stdout}{r.stderr}")
    assert r.returncode == 0, r.returncode
    return r.stdout


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        dl, store, db = td / "downloads", td / "store", str(td / "estate.db")
        dl.mkdir()
        z1, z2 = build_takeout(dl)
        D = ("--db", db)

        run(*D, "ingest", "--store", str(store), str(z1))
        out = run(*D, "link")
        assert "IMG_0004.JPG" in out, "IMG_0004 should be unmatched until zip 2 arrives"
        run(*D, "ingest", "--store", str(store), "--delete-after", str(z2))
        assert not z2.exists()
        assert "skip" in run(*D, "ingest", "--store", str(store), str(z1))   # resumable
        out = run(*D, "link")
        assert "linked 11/11" in out, out

        con = sqlite3.connect(db)
        q = lambda s, *a: con.execute(s, a).fetchone()
        assert q("SELECT count(*) FROM occurrences")[0] == 11
        assert q("SELECT count(*) FROM assets")[0] == 9, "2 exact dups should collapse"
        blobs = [p for p in store.rglob("*") if p.is_file() and ".tmp" not in p.parts]
        assert len(blobs) == 9, blobs
        # metadata merged from sidecars (incl. truncated name, (1) dup, cross-zip, -edited)
        lat, fav, taken = q("SELECT lat, favorited, taken_at FROM assets a JOIN occurrences o USING(sha256)"
                            " WHERE o.filename='IMG_0001.JPG' LIMIT 1")
        assert abs(lat - 48.8584) < 1e-6 and fav == 1 and taken.startswith("2019-07-14T10:22:33")
        assert q("SELECT people FROM assets a JOIN occurrences o USING(sha256) WHERE o.filename='IMG_0005.JPG'")[0] == "Alice, Bob"
        assert q("SELECT album FROM occurrences WHERE dir LIKE '%Paris Trip'")[0] == "Paris Trip 🇫🇷"
        assert q("SELECT taken_at FROM assets a JOIN occurrences o USING(sha256) WHERE o.filename LIKE 'PXL%'")[0].startswith("2020-07-04")
        assert q("SELECT count(*) FROM assets WHERE kind='video'")[0] == 1

        out = run(*D, "dupes", "--json", str(td / "near.json"))
        clusters = json.loads((td / "near.json").read_text())
        names = sorted(sorted(Path(m["path"]).name for m in c["members"]) for c in clusters)
        assert {c["category"] for c in clusters} == {"edit", "copies"}, [c["category"] for c in clusters]
        assert names == [["IMG-20210505-WA0007.jpg", "IMG_0005.JPG"], ["IMG_0001-edited.JPG", "IMG_0001.JPG"]], names

        srv = HTTPServer(("127.0.0.1", 0), MockVLM)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ep = f"http://127.0.0.1:{srv.server_port}/v1"
        out = run(*D, "tag", "--store", str(store), "--endpoint", ep, "--model", "mock-vlm", "--limit", "3")
        assert q("SELECT count(*) FROM tags")[0] == 3
        run(*D, "tag", "--store", str(store), "--endpoint", ep, "--model", "mock-vlm")
        assert q("SELECT count(*) FROM tags")[0] == 8, "8 images (video skipped)"
        srv.shutdown()

        assert "1 result" in run(*D, "search", "Alice")
        assert "1 result" in run(*D, "search", "paris")          # original + Paris album copy = 1 asset
        assert "8 result" in run(*D, "search", "circles")
        assert "1 result" in run(*D, "search", "garden")
        run(*D, "stats")

        # iCloud (icloudpd --xmp-sidecar): a re-encoded copy of a Google photo whose EXIF time is
        # local and whose XMP time carries the offset. It must land on the same UTC instant, so the
        # pair is a "copies" group, not "same day, different times".
        grouped = {m["sha256"] for c in clusters for m in c["members"]}
        sha, ext, taken = next(r for r in con.execute(
            "SELECT sha256, ext, taken_at FROM assets WHERE kind='image' AND taken_at LIKE '%+00:00'")
            if r[0] not in grouped)
        utc = datetime.fromisoformat(taken)
        local = utc.astimezone(timezone(timedelta(hours=-7)))
        with Image.open(store / sha[:2] / sha[2:4] / f"{sha}{ext}") as im:
            im.load()
            data = jpeg(im.convert("RGB"), local.strftime("%Y:%m:%d %H:%M:%S"), quality=60)
        day = td / "icloud" / "PrimarySync" / local.strftime("%Y/%m/%d")
        day.mkdir(parents=True)
        (day / "IMG_9999_QUJDREV.JPG").write_bytes(data)
        (day / "IMG_9999_QUJDREV.JPG.xmp").write_text(
            "<x:xmpmeta><rdf:RDF><rdf:Description><photoshop:DateCreated>"
            f"{local.strftime('%Y-%m-%dT%H:%M:%S%z')}</photoshop:DateCreated><xmp:Rating>5</xmp:Rating>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>")
        run(*D, "ingest", "--store", str(store), str(td / "icloud" / "PrimarySync"))
        run(*D, "link")
        t2, fav2, album = q("SELECT a.taken_at, a.favorited, o.album FROM assets a JOIN occurrences o"
                            " USING(sha256) WHERE o.filename='IMG_9999_QUJDREV.JPG'")
        assert datetime.fromisoformat(t2) == utc and fav2 == 1 and album is None, (t2, fav2, album)
        run(*D, "dupes", "--json", str(td / "near2.json"))
        g = next(c for c in json.loads((td / "near2.json").read_text())
                 if sha in {m["sha256"] for m in c["members"]})
        assert g["category"] == "copies" and len(g["members"]) == 2, g

        # Flickr: originals zip(s) plus a separate account-data zip of photo_<id>.json files.
        fa, _ = photo(31, None)
        fb, _ = photo(32, None)
        with zipfile.ZipFile(td / "data-download-1.zip", "w") as z:
            z.writestr("sunset-at-rialto_12345678901_o.jpg", jpeg(fa))                      # no EXIF
            z.writestr("98765432101_0a1b2c3d4e_o.jpg", jpeg(fb, "2010:06:01 08:00:00",
                       camera=("NIKON CORPORATION", "NIKON D300", 0.004, 2.8, 400)))
        def fj(fid, name, desc, taken, albums=(), tags=(), geo=()):
            return json.dumps({"id": fid, "name": name, "description": desc, "date_taken": taken,
                               "photopage": f"https://www.flickr.com/photos/x/{fid}/", "privacy": "public",
                               "albums": [{"id": "1", "title": a} for a in albums],
                               "tags": [{"tag": t} for t in tags], "geo": list(geo), "people": []})
        with zipfile.ZipFile(td / "72157_abc_part1.zip", "w") as z:
            z.writestr("photo_12345678901.json", fj("12345678901", "Sunset at Rialto",
                       "Low tide &amp; <a href='x'>sea stacks</a>", "2011-07-04 20:45:00",
                       albums=("Rialto Beach", "Olympic NP"), tags=("beach", "sunset"),
                       geo=[{"latitude": "47920800", "longitude": "-124638600", "accuracy": "16"}]))
            z.writestr("photo_98765432101.json", fj("98765432101", "DSC_5300.jpg", "", "2010-06-01 09:30:00",
                                                    tags=("bicycle",)))
            z.writestr("albums.json", json.dumps({"albums": []}))
            z.writestr("account_profile.json", json.dumps({"real_name": "x"}))
        run(*D, "ingest", "--store", str(store), str(td / "data-download-1.zip"), str(td / "72157_abc_part1.zip"))
        out = run(*D, "link")
        assert "2 Flickr photos" in out, out
        taken, lat, lon, desc, album = q(
            "SELECT a.taken_at, a.lat, a.lon, a.description, o.album FROM assets a JOIN occurrences o"
            " USING(sha256) WHERE o.filename LIKE 'sunset-at-rialto%'")
        assert taken == "2011-07-04T20:45:00" and abs(lat - 47.9208) < 1e-6 and abs(lon + 124.6386) < 1e-6
        assert desc == "Sunset at Rialto — Low tide & sea stacks" and album == "Rialto Beach, Olympic NP", (desc, album)
        taken, desc = q("SELECT a.taken_at, a.description FROM assets a JOIN occurrences o USING(sha256)"
                        " WHERE o.filename LIKE '98765432101%'")
        assert taken == "2010-06-01T08:00:00" and desc is None, (taken, desc)   # EXIF wins; filename title dropped
        assert "1 result" in run(*D, "search", "bicycle")
        assert "1 result" in run(*D, "search", '"sea stacks"')
        assert "1 result" in run(*D, "search", "olympic")
        run(*D, "link")   # idempotent
        assert q("SELECT count(*) FROM tags WHERE model='flickr'")[0] == 2

        # exif: every asset read with exiftool; camera and exposure promoted and searchable
        if shutil.which("exiftool"):
            run(*D, "exif", "--store", str(store))
            assert q("SELECT count(*) FROM exif")[0] == q("SELECT count(*) FROM assets")[0]
            cam, fnum, expo, iso = q("SELECT e.camera, e.fnumber, e.exposure, e.iso FROM exif e JOIN occurrences o"
                                     " USING(sha256) WHERE o.filename LIKE '98765432101%'")
            assert (cam, fnum, iso) == ("Nikon D300", 2.8, 400) and abs(expo - 0.004) < 1e-9, (cam, fnum, expo, iso)
            assert "1 result" in run(*D, "search", "D300")
            assert "0 assets to read" in run(*D, "exif", "--store", str(store))   # resumable
        else:
            print("SKIPPED exif checks: exiftool not installed")

        # places: GPS -> town + zone; UTC sidecar times become local wall-clock time; events
        out = run(*D, "run", script=PLACES)
        city, country, local, tz, src = q("SELECT p.city, p.country, p.local_time, p.tz, p.tz_source FROM places p"
                                          " JOIN occurrences o USING(sha256) WHERE o.filename='IMG_0001.JPG' LIMIT 1")
        assert (city, country, tz, src) == ("Paris", "France", "Europe/Paris", "gps"), (city, country, tz, src)
        assert local == "2019-07-14T12:22:33", local          # 10:22:33 UTC is 12:22:33 in Paris (CEST)
        local, = q("SELECT p.local_time FROM places p JOIN occurrences o USING(sha256)"
                   " WHERE o.filename LIKE '98765432101%'")
        assert local == "2010-06-01T08:00:00", local          # EXIF wall-clock time is kept as is
        assert q("SELECT count(*) FROM events")[0] >= 5
        assert q("SELECT count(*) FROM places WHERE event_id IS NULL")[0] == 0
        assert "1 result" in run(*D, "search", "Forks")      # Flickr geo -> nearest town, searchable
        run(*D, "run", script=PLACES)                          # idempotent
        assert q("SELECT count(*) FROM places")[0] == q("SELECT count(*) FROM assets WHERE taken_at IS NOT NULL")[0]
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
