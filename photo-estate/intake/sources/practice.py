"""Practice sources: stand-ins for the real services, made from the demo library's 104 public-domain
photos, so the whole setup flow can be tried (and tested) with no account and no network. They exist
only when the app runs with --sandbox.

Each one rehearses a different way of signing in, the same ways the real sources use:

  practice_takeout   none: a Takeout-shaped zip with Google's JSON sidecars appears in staging
  practice_drive     browser: approve access on a (fake) consent page, then files are "downloaded"
  practice_icloud    password, then a code sent to your phone, then an iCloud-shaped mirror with XMP sidecars

They overlap on purpose (some photos are in two of them), so the run shows duplicates being collapsed."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from upload import xmp_for

from ..base import AUTH_BROWSER, AUTH_NONE, AUTH_PASSWORD_CODE, Preview, Prompt, Source, Step, register

DEMO = Path(__file__).resolve().parents[2] / "demo"
PRACTICE_CODE = "271828"


def demo_photos() -> list[dict]:
    """The demo photos with what the demo catalog knows about them, in a fixed order."""
    con = sqlite3.connect(f"file:{DEMO / 'estate.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT a.sha256, a.ext, a.taken_at, a.lat, a.lon, a.description, a.favorited,"
            " (SELECT filename FROM occurrences o WHERE o.sha256=a.sha256 ORDER BY id LIMIT 1),"
            " (SELECT album FROM occurrences o WHERE o.sha256=a.sha256 AND album IS NOT NULL ORDER BY id LIMIT 1)"
            " FROM assets a WHERE kind='image' ORDER BY a.sha256").fetchall()
    finally:
        con.close()
    out = []
    for sha, ext, taken, lat, lon, desc, fav, fn, album in rows:
        out.append({"sha": sha, "path": DEMO / "store" / sha[:2] / sha[2:4] / f"{sha}{ext}", "taken": taken,
                    "lat": lat, "lon": lon, "desc": desc, "fav": bool(fav), "name": fn or f"{sha[:12]}{ext}",
                    "album": album})
    return out


def pace(n: int) -> None:
    """Make a practice download take a few seconds, so its progress can be watched."""
    if not os.environ.get("PHOTO_ESTATE_PRACTICE_FAST"):
        time.sleep(4.0 / max(n, 1))


class Practice(Source):
    practice = True
    group = "practice"
    take = slice(0, 0)

    def photos(self) -> list[dict]:
        return demo_photos()[self.take]

    def preview(self, config):
        if not (DEMO / "estate.db").exists():
            return Preview(ok=False, problems=[f"The demo library is missing from {DEMO}."])
        n = len(self.photos())
        return Preview(ok=True, summary=f"{n} practice photos from the demo library.", media=n)


@register
class PracticeTakeout(Practice):
    id = "practice_takeout"
    name = "Practice: Google Takeout"
    blurb = "A small pretend Takeout export, with Google's dates, places and albums."
    auth = AUTH_NONE
    time_hint = "A few seconds"
    take = slice(0, 48)

    def guide(self):
        return [Step("Nothing to do", "This stands in for the zips Google emails you. Start it to watch an export "
                     "being read: dates and places come from Google's sidecar files.")]

    def acquire(self, config, ctx):
        photos = self.photos()
        dest = ctx.staging / "takeout-practice-001.zip"
        if dest.exists():
            return [dest]
        tmp = dest.with_suffix(".part")
        albums = set()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            for i, p in enumerate(photos, 1):
                year = (p["taken"] or "2020")[:4]
                folders = [f"Takeout/Google Photos/Photos from {year}"]
                if p["album"]:
                    folders.append(f"Takeout/Google Photos/{p['album']}")
                side = {"title": p["name"], "description": p["desc"] or ""}
                if p["taken"]:
                    ts = int(datetime.fromisoformat(p["taken"]).timestamp())
                    side["photoTakenTime"] = {"timestamp": str(ts)}
                if p["lat"] is not None:
                    side["geoData"] = {"latitude": p["lat"], "longitude": p["lon"], "altitude": 0.0}
                if p["fav"]:
                    side["favorited"] = True
                for d in folders:
                    zf.write(p["path"], f"{d}/{p['name']}")
                    zf.writestr(f"{d}/{p['name']}.supplemental-metadata.json", json.dumps(side, indent=2))
                if p["album"] and p["album"] not in albums:   # an album folder's own title
                    albums.add(p["album"])
                    zf.writestr(f"Takeout/Google Photos/{p['album']}/metadata.json", json.dumps({"title": p["album"]}))
                ctx.progress(i, len(photos), f"Downloading takeout-practice-001.zip: {i} of {len(photos)}")
                pace(len(photos))
        tmp.rename(dest)
        return [dest]


def copy_slowly(photos, dest_of, ctx, what: str, sidecar=None) -> None:
    for i, p in enumerate(photos, 1):
        d = dest_of(p)
        if not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p["path"], d.with_name(d.name + ".part"))
            d.with_name(d.name + ".part").rename(d)
            if sidecar and (text := sidecar(p)):
                d.with_name(d.name + ".xmp").write_text(text)
        ctx.progress(i, len(photos), f"{what}: {i} of {len(photos)}")
        pace(len(photos))


@register
class PracticeDrive(Practice):
    id = "practice_drive"
    name = "Practice: a cloud drive"
    blurb = "Rehearses signing in through a browser, as Dropbox, OneDrive and Google Drive will."
    auth = AUTH_BROWSER
    time_hint = "A few seconds"
    take = slice(40, 72)

    def guide(self):
        return [Step("Approve access", "A pretend consent page opens. Real cloud drives show their own page there, "
                     "and Photo Estate only ever asks to read."),
                Step("Watch it download", "Files with no sidecar get their date and place from the photo itself.")]

    def acquire(self, config, ctx):
        ctx.ask(Prompt("browser", "Allow Photo Estate to read your practice drive",
                       help="Opens a pretend consent page in a new tab. Approve there and come back.",
                       url=f"/setup/practice-consent?job={ctx.job_id}"))
        ctx.log("signed in to the practice drive")
        root = ctx.staging / "practice-drive"
        copy_slowly(self.photos(), lambda p: root / "Camera Uploads" / p["name"], ctx, "Downloading from the drive")
        return [root]


@register
class PracticeICloud(Practice):
    id = "practice_icloud"
    name = "Practice: iCloud"
    blurb = "Rehearses a password and a code sent to your phone, as iCloud will."
    auth = AUTH_PASSWORD_CODE
    time_hint = "A few seconds"
    take = slice(64, 104)

    def guide(self):
        return [Step("Sign in", "Type any password except “wrong” (try “wrong” to see what a mistake looks like)."),
                Step("Enter the code", f"A real iCloud sends a code to your iPhone. Here the code is {PRACTICE_CODE}.")]

    def acquire(self, config, ctx):
        label = "Apple Account password (practice)"
        while (pw := ctx.ask(Prompt("password", label, help="Any password except “wrong”.", secret=True))) == "wrong":
            ctx.log("practice iCloud: that password was not accepted")
            label = "That password didn't work. Try again"
        del pw
        label = "Enter the code sent to your iPhone"
        while ctx.ask(Prompt("code", label, help=f"Practice: the code is {PRACTICE_CODE}.")).strip() != PRACTICE_CODE:
            label = "That code didn't match. Try again"
        ctx.log("signed in to practice iCloud")
        root = ctx.staging / "practice-icloud" / "PrimarySync"

        def where(p):
            t = datetime.fromisoformat(p["taken"]) if p["taken"] else datetime(2020, 1, 1, tzinfo=timezone.utc)
            return root / f"{t:%Y/%m/%d}" / p["name"]

        def xmp(p):
            meta = {"lat": p["lat"], "lon": p["lon"], "fav": p["fav"]}
            if p["taken"]:   # as an iPhone records it: local time with its offset (here, a fixed -0700)
                t = datetime.fromisoformat(p["taken"]).astimezone(timezone(timedelta(hours=-7)))
                meta["taken"] = t.strftime("%Y-%m-%dT%H:%M:%S%z")
            return xmp_for(meta)

        copy_slowly(self.photos(), where, ctx, "Downloading from iCloud", sidecar=xmp)
        return [root]
