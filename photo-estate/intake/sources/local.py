"""Sources whose files are already on this computer (or a disk plugged into it): a Google Takeout
someone downloaded from the email links, a Flickr export, or any folder of photos.

They share one rule for what to hand `ingest`: every zip under the chosen path, plus the path itself
when it also holds loose photos (ingest reads a folder's photos and ignores the zips in it). Nothing is
copied: ingest reads zips in place, and the store keeps its own copy of every photo."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

from ..base import AUTH_MANUAL, AUTH_NONE, Field, Preview, Source, Step, register
from ..media import MEDIA_EXT, human_bytes

PATH_FIELD = Field("path", "Where are the files?", kind="path",
                   help="A folder (or a single .zip). Folders inside it are included.",
                   placeholder="~/Downloads")


def resolve(config: dict) -> Path | None:
    raw = (config.get("path") or "").strip()
    return Path(os.path.expanduser(raw)).resolve() if raw else None


def scan(root: Path, limit_dirs: int = 200_000) -> tuple[list[Path], int, int, int]:
    """(zips, media in zips, loose media, bytes) under root. Reads zip directories only, not their contents."""
    zips, in_zips, loose, size, seen = [], 0, 0, 0, 0
    paths = [root] if root.is_file() else (p for p in root.rglob("*"))
    for p in paths:
        seen += 1
        if seen > limit_dirs:
            break
        if p.name.startswith(".") or not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext == ".zip":
            zips.append(p)
            size += p.stat().st_size
            try:
                with zipfile.ZipFile(p) as zf:
                    in_zips += sum(1 for n in zf.namelist() if os.path.splitext(n)[1].lower() in MEDIA_EXT)
            except (zipfile.BadZipFile, OSError):
                pass
        elif ext in MEDIA_EXT:
            loose += 1
            size += p.stat().st_size
    return sorted(zips), in_zips, loose, size


def zip_names(z: Path, n: int = 4000) -> list[str]:
    try:
        with zipfile.ZipFile(z) as zf:
            return zf.namelist()[:n]
    except (zipfile.BadZipFile, OSError):
        return []


class LocalFiles(Source):
    auth = AUTH_NONE
    group = "computer"
    fields = [PATH_FIELD]

    def notes(self, zips: list[Path], loose: int) -> list[str]:
        """Source-specific warnings about what was found (e.g. half of a Flickr export)."""
        return []

    def preview(self, config: dict) -> Preview:
        root = resolve(config)
        if root is None:
            return Preview(ok=False, problems=["Choose a folder first."])
        if not root.exists():
            return Preview(ok=False, problems=[f"{root} does not exist. Is the disk plugged in?"])
        if root.is_file() and root.suffix.lower() != ".zip":
            return Preview(ok=False, problems=["Choose a folder or a .zip file."])
        zips, in_zips, loose, size = scan(root)
        media = in_zips + loose
        if not media:
            return Preview(ok=False, problems=[f"No photos or videos found in {root}."])
        things = lambda n: f"{n:,} photo{'s' * (n != 1)} and video{'s' * (n != 1)}" if n != 1 else "1 photo or video"
        parts = []
        if zips:
            parts.append(f"{len(zips)} zip{'s' * (len(zips) != 1)} holding {things(in_zips)}")
        if loose:
            parts.append(f"{things(loose)} in folders")
        items = [str(z) for z in zips] + ([str(root)] if loose and root.is_dir() else [])
        return Preview(ok=True, summary=f"Found {' and '.join(parts)} ({human_bytes(size)}).",
                       problems=self.notes(zips, loose), items=items, media=media)

    def acquire(self, config: dict, ctx) -> list[Path]:
        p = self.preview(config)
        if not p.ok:
            raise RuntimeError(" ".join(p.problems))
        ctx.progress(p.media, p.media, p.summary)
        return [Path(i) for i in p.items]


@register
class Folder(LocalFiles):
    id = "folder"
    name = "A folder on this computer"
    blurb = "Photos from an old camera, a backup disk, Dropbox or OneDrive's folder on this Mac."
    time_hint = "Ready now"

    def guide(self):
        return [Step("Find the folder",
                     "Any folder of photos and videos works, including folders inside it and .zip files. "
                     "If Dropbox or OneDrive is installed on this computer, its folder works too (make sure the "
                     "files are downloaded, not online-only)."),
                Step("Choose it below",
                     "Photo Estate copies each photo into its library once. The folder is only read, never changed.")]


@register
class TakeoutFiles(LocalFiles):
    id = "takeout_files"
    name = "Google Photos"
    blurb = "From a Google Takeout export you downloaded."
    group = "google"
    auth = AUTH_MANUAL
    time_hint = "Google takes hours to a few days to prepare the export"

    def guide(self):
        return [
            Step("Ask Google for your photos",
                 "Open Google Takeout, click “Deselect all”, then tick only Google Photos.",
                 link="https://takeout.google.com/", link_label="Open Google Takeout"),
            Step("Choose how it arrives",
                 "Destination: “Send download link via email”. Frequency: “Export once”. File type: .zip. "
                 "Size: 4 GB, so no single download is too big.",
                 note="Photos you uploaded at “Storage saver” quality come back compressed; Google keeps no original."),
            Step("Wait for the email",
                 "Google emails you when the export is ready: usually hours, sometimes a few days for a big library. "
                 "The links expire after about a week."),
            Step("Download every zip into one folder",
                 "Download all the parts (takeout-…-001.zip, -002.zip, …) into one folder. "
                 "You don't need to unzip them."),
            Step("Choose that folder below",
                 "A photo and its details can land in different zips; once every zip is in, they are matched up. "
                 "You can add zips later as they finish downloading."),
        ]

    def notes(self, zips, loose):
        if zips and not any("Google Photos/" in n or "Google Fotos/" in n for z in zips[:3] for n in zip_names(z)):
            return ["These zips don't look like a Google Takeout (no “Google Photos” folder inside). They will still be read."]
        return []


@register
class FlickrFiles(LocalFiles):
    id = "flickr_files"
    name = "Flickr"
    blurb = "From the “Your Flickr data” export."
    group = "flickr"
    auth = AUTH_MANUAL
    time_hint = "Flickr takes a day or more to prepare the export"

    def guide(self):
        return [
            Step("Request your Flickr data",
                 "Sign in to Flickr, open Settings, and under “Your Flickr data” click “Request my Flickr data”.",
                 link="https://www.flickr.com/account", link_label="Open Flickr settings"),
            Step("Wait for the email", "Flickr emails you when the export is ready, often within a day or two."),
            Step("Download both kinds of zip",
                 "There are two: data-download-1.zip, -2.zip… hold your original photos; a zip named like "
                 "12345678@N00_…_part1.zip holds the titles, descriptions, tags, albums and places. Put them all in one folder.",
                 note="Without the second kind your photos still come in, but without their Flickr titles and albums."),
            Step("Choose that folder below", ""),
        ]

    def notes(self, zips, loose):
        names = [z.name for z in zips]
        originals = [n for n in names if n.startswith("data-download")]
        account = [n for n in names if "_part" in n and "data-download" not in n]
        out = []
        if zips and not account:
            out.append("No account-data zip (…_part1.zip) found: photos will come in without Flickr titles, tags and albums.")
        if zips and account and not originals:
            out.append("Only account data found: download the data-download-N.zip files too, which hold the photos.")
        return out
