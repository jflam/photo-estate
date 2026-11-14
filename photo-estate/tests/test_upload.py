#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10.0", "pillow-heif>=0.18"]
# ///
"""Phone uploads: the inbox rules in upload.py, then an uploaded photo + sidecar through the real
`estate.py ingest` and `link`, to check it arrives with its date, place and favourite, and that a
second run (and the same photo under another name) adds nothing."""
import hashlib, io, random, sqlite3, subprocess, sys, tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from upload import clean_path, save_upload  # noqa: E402

ESTATE = HERE.parent / "estate.py"


def jpeg(seed: int) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", (800, 600), tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(15):
        x, y = rnd.randrange(800), rnd.randrange(600)
        d.ellipse([x, y, x + 150, y + 150], fill=tuple(rnd.randrange(256) for _ in range(3)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def put(inbox, rel, data, sha=None):
    return save_upload(inbox, rel, io.BytesIO(data), len(data), sha)


def run(*args):
    r = subprocess.run(["uv", "run", "--quiet", "--script", str(ESTATE), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def main():
    # which paths are accepted
    for ok in ("2026/11/14/IMG_0042-A1B2C3D.HEIC", "a.jpg", "2026/11/14/IMG_1.HEIC.xmp", "x/IMG_1 (1).MOV"):
        assert clean_path(ok), ok
    for bad in ("", "../x.jpg", "a/../b.jpg", "/etc/passwd", ".hidden.jpg", "a/.b/c.jpg", "a\\b.jpg",
                "a.exe", "a.json", "note.xmp","a//b.jpg", "x.jpg.", "a" + "/a" * 7 + ".jpg"):
        assert clean_path(bad) is None, bad

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        inbox, store, db = td / "iphone", td / "store", str(td / "estate.db")
        a = jpeg(1)
        rel = "2026/11/14/IMG_0042-A1B2C3D.JPG"

        assert put(inbox, rel, a, hashlib.sha256(a).hexdigest())[0] == 201
        assert (inbox / rel).read_bytes() == a
        assert put(inbox, rel, a)[0] == 200, "same file again is a no-op"
        assert put(inbox, rel, a + b"x")[0] == 409, "a path never changes under ingest"
        assert put(inbox, "../escape.jpg", a)[0] == 400
        assert put(inbox, "2026/11/14/bad.jpg", a, "0" * 64)[0] == 400, "wrong checksum is refused"
        assert not (inbox / "2026/11/14/bad.jpg").exists()
        code, msg = save_upload(inbox, "2026/11/14/short.jpg", io.BytesIO(a[:100]), len(a))
        assert code == 400 and "100 of" in msg and not (inbox / "2026/11/14/short.jpg").exists()
        assert not any(inbox.rglob("*.partial")) and not any((td / "iphone.partial").iterdir()), "no leftovers"

        xmp = ("<x:xmpmeta><rdf:Description>"
               "<photoshop:DateCreated>2026-11-14T10:00:00-0800</photoshop:DateCreated>"
               "<exif:GPSLatitude>48.8584</exif:GPSLatitude><exif:GPSLongitude>2.2945</exif:GPSLongitude>"
               "<xmp:Rating>5</xmp:Rating></rdf:Description></x:xmpmeta>").encode()
        assert put(inbox, rel + ".xmp", xmp)[0] == 201
        assert put(inbox, "2026/11/14/IMG_0043-FFFFFFF.JPG", a)[0] == 201, "same bytes, other name"

        D = ("--db", db)
        out = run(*D, "ingest", "--store", str(store), "--force", str(inbox))
        assert "1 new, 1 exact dups, 1 sidecars" in out, out
        run(*D, "link")
        con = sqlite3.connect(db)
        lat, lon, fav, taken = con.execute("SELECT lat, lon, favorited, taken_at FROM assets").fetchone()
        assert abs(lat - 48.8584) < 1e-6 and abs(lon - 2.2945) < 1e-6 and fav == 1, (lat, lon, fav)
        assert taken.startswith("2026-11-14T18:00:00"), taken      # 10:00 at UTC-8
        assert con.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 2

        # the next batch: one new photo; nothing already seen is added again
        assert put(inbox, "2026/11/15/IMG_0044-1111111.JPG", jpeg(2))[0] == 201
        out = run(*D, "ingest", "--store", str(store), "--force", str(inbox))
        assert "1 new" in out and "1 exact dups" not in out.replace("0 exact dups", ""), out
        assert con.execute("SELECT count(*) FROM assets").fetchone()[0] == 2
        blobs = [p for p in store.rglob("*") if p.is_file() and ".tmp" not in p.parts]
        assert len(blobs) == 2, blobs
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
