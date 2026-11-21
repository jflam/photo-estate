"""images.py's thumbnails as WebP (#104) and its background job (#26): every missing thumbnail, then every missing preview, newest first; it
stops for a full disk, and gives way to a newer run so a new photo's thumbnail never waits behind it.

  uv run pytest tests/test_pregenerate.py
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent          # photo-estate/
sys.path.insert(0, str(ROOT))
import images  # noqa: E402
from images import Images  # noqa: E402


def library(tmp_path, n=5):
    """A store of n small JPEGs and a catalog of them; the last is the newest."""
    store, shas = tmp_path / "store", [f"{i:02x}" * 32 for i in range(n)]
    for i, s in enumerate(shas):
        f = store / s[:2] / s[2:4] / f"{s}.jpg"
        f.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2400, 1800), (40 * i, 90, 200)).save(f, "JPEG")
    cat = SimpleNamespace(shas=shas, newest=list(range(n - 1, -1, -1)), meta={s: {"ext": ".jpg"} for s in shas})
    return SimpleNamespace(cat=cat), store, shas


def test_every_photo_gets_a_thumbnail_and_a_preview(tmp_path):
    live, store, shas = library(tmp_path)
    im = Images(live, store, tmp_path / "cache", tmp_path / "cache")
    im.pregenerate()
    for s in shas:
        with Image.open(tmp_path / "cache" / "thumbs" / f"{s}.webp") as t:
            assert max(t.size) == 360
        with Image.open(tmp_path / "cache" / "previews" / f"{s}.jpg") as p:
            assert max(p.size) == 1600
    assert not (tmp_path / "cache" / "large").exists(), "the 4096 px images are left to be made on first use"
    assert not im.pregen_running


def test_thumbnails_are_webp_and_previews_jpeg(tmp_path):
    live, store, shas = library(tmp_path, 1)
    im = Images(live, store, tmp_path / "cache", tmp_path / "cache")
    status, body, ctype, cache = im.handle("GET", f"/thumb/{shas[0]}", {}, None)
    assert (status, ctype, body[:4], body[8:12]) == (200, "image/webp", b"RIFF", b"WEBP")
    status, body, ctype, cache = im.handle("GET", f"/preview/{shas[0]}", {}, None)
    assert (status, ctype, body[:2]) == (200, "image/jpeg", b"\xff\xd8")
    from catalog import image_size
    assert image_size(str(tmp_path / "cache" / "thumbs" / f"{shas[0]}.webp")) == (360, 270)


def test_a_jpeg_thumbnail_from_before_is_served_until_the_webp_is_made(tmp_path):
    """The grid must not wait on decoding originals while 100,000 thumbnails are redone as WebP."""
    live, store, shas = library(tmp_path, 2)
    seed, fast = tmp_path / "seed", tmp_path / "fast"
    (seed / "thumbs").mkdir(parents=True)
    (fast / "thumbs").mkdir(parents=True)
    (seed / "thumbs" / f"{shas[0]}.jpg").write_bytes(b"\xff\xd8 old, on the disk")
    (fast / "thumbs" / f"{shas[1]}.jpg").write_bytes(b"\xff\xd8 old, on the SSD")
    im = Images(live, store, fast, seed)
    assert im.handle("GET", f"/thumb/{shas[0]}", {}, None)[1:3] == (b"\xff\xd8 old, on the disk", "image/jpeg")
    assert im.handle("GET", f"/thumb/{shas[1]}", {}, None)[1:3] == (b"\xff\xd8 old, on the SSD", "image/jpeg")
    assert not list(fast.glob("thumbs/*.webp")), "no decode on the request's path"
    im.pregenerate()
    for s in shas:
        assert im.handle("GET", f"/thumb/{s}", {}, None)[2] == "image/webp"


def test_previews_the_disk_cache_has_are_not_made_again(tmp_path):
    live, store, shas = library(tmp_path, 3)
    seed = tmp_path / "seed"
    (seed / "previews").mkdir(parents=True)
    (seed / "previews" / f"{shas[0]}.jpg").write_bytes(b"made before")
    Images(live, store, tmp_path / "fast", seed).pregenerate()
    assert not (tmp_path / "fast" / "previews" / f"{shas[0]}.jpg").exists(), "copied on first use instead"
    assert all((tmp_path / "fast" / "previews" / f"{s}.jpg").exists() for s in shas[1:])


def test_no_previews_while_the_disk_is_nearly_full(tmp_path, monkeypatch):
    live, store, shas = library(tmp_path, 2)
    monkeypatch.setattr(images, "PREVIEW_ROOM", 1 << 60)
    Images(live, store, tmp_path / "cache", tmp_path / "cache").pregenerate()
    assert all((tmp_path / "cache" / "thumbs" / f"{s}.webp").exists() for s in shas), "thumbnails still made"
    assert not list((tmp_path / "cache" / "previews").iterdir())


def test_a_call_during_a_run_runs_again_with_the_new_catalog(tmp_path, monkeypatch):
    """Before: a second pregenerate() while one ran returned and did nothing, so photos added meanwhile
    waited for the next start. With a preview pass of hours, that would be a new photo's thumbnail."""
    live, store, shas = library(tmp_path, 3)
    im = Images(live, store, tmp_path / "cache", tmp_path / "cache")
    started, go, calls = threading.Event(), threading.Event(), []
    real = im._previews

    def slow_previews():
        calls.append(list(live.cat.shas))
        if len(calls) == 1:
            started.set()
            go.wait(10)
        real()
    monkeypatch.setattr(im, "_previews", slow_previews)
    t = threading.Thread(target=im.pregenerate)
    t.start()
    assert started.wait(10)
    more, _, added = library(tmp_path / "more", 4)            # the catalog is reloaded with a new photo
    new = added[3]
    src = store / new[:2] / new[2:4] / f"{new}.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes((tmp_path / "more" / "store" / new[:2] / new[2:4] / f"{new}.jpg").read_bytes())
    live.cat = more.cat
    im.pregenerate()                                            # returns at once: one is under way
    assert im.pregen_again
    go.set()
    t.join(10)
    assert len(calls) == 2 and new in calls[1]
    assert (tmp_path / "cache" / "thumbs" / f"{new}.webp").exists()
    assert (tmp_path / "cache" / "previews" / f"{new}.jpg").exists()
    assert not im.pregen_running and not im.pregen_again


def test_a_failing_pass_does_not_leave_it_marked_running(tmp_path, monkeypatch):
    live, store, _ = library(tmp_path, 1)
    im = Images(live, store, tmp_path / "cache", tmp_path / "cache")
    monkeypatch.setattr(im, "_thumbs", lambda: 1 / 0)
    im.pregenerate()
    assert not im.pregen_running


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
