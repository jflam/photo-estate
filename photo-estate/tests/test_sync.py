#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""The change feed a phone syncs from (sync.py): a device that pages through it ends with the same
rows as the catalog, later changes and deletions reach it as a small delta, versions survive a
restart, and a lost feed file makes devices start again."""
import sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sync import Feed  # noqa: E402


def pull(feed, device, limit=7):
    """One sync, as the app does it. Returns how many rows were sent."""
    sent = 0
    while True:
        ids, deleted, (since, after), more = feed.page(device["epoch"], device["since"], device["after"], limit)
        if device["epoch"] != feed.epoch:
            device["stale"] = set(device["rows"])
        for sha in deleted:
            device["rows"].pop(sha, None)
        for sha in ids:
            device["rows"][sha] = dict(feed.catalog[sha])
            device.get("stale", set()).discard(sha)
        device.update(epoch=feed.epoch, since=since, after=after)
        sent += len(ids)
        if not more:
            for sha in device.pop("stale", set()):
                device["rows"].pop(sha, None)
            return sent


def load(feed, catalog):
    feed.catalog = catalog
    return feed.update(catalog)


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "sync-feed.db"
    catalog = {f"{i:064x}": {"ext": ".jpg", "fav": False, "people": []} for i in range(40)}
    feed = Feed(path)
    assert not feed.ready
    assert load(feed, catalog) == 40 and feed.ready and feed.version == 1
    phone = {"epoch": "", "since": 0, "after": "", "rows": {}}
    assert pull(feed, phone) == 40 and phone["rows"] == catalog
    assert pull(feed, phone) == 0, "nothing changed, nothing sent"

    # a change, an addition and a deletion arrive as exactly those rows
    catalog = dict(catalog)
    first, last = f"{0:064x}", f"{39:064x}"
    catalog[first] = dict(catalog[first], fav=True)
    catalog[f"{99:064x}"] = {"ext": ".heic", "fav": False, "people": ["Ben"]}
    del catalog[last]
    assert load(feed, catalog) == 3 and feed.version == 2
    assert load(feed, catalog) == 0 and feed.version == 2, "the same catalog again is not a change"
    assert pull(feed, phone) == 2 and phone["rows"] == catalog

    # a device that stopped mid-way carries on from its cursor
    slow = {"epoch": "", "since": 0, "after": "", "rows": {}}
    ids, _, (since, after), more = feed.page("", 0, "", 10)
    assert more and len(ids) == 10
    slow.update(epoch=feed.epoch, since=since, after=after, rows={s: dict(catalog[s]) for s in ids})
    assert pull(feed, slow) == 30 and slow["rows"] == catalog

    # versions survive a restart
    again = Feed(path)
    assert (again.epoch, again.version) == (feed.epoch, 2)
    assert load(again, catalog) == 0
    assert pull(again, phone) == 0

    # a photo that comes back after being deleted is sent again
    catalog[last] = {"ext": ".jpg", "fav": False, "people": []}
    assert load(again, catalog) == 1
    assert pull(again, phone) == 1 and phone["rows"] == catalog

    # a lost feed file: a new epoch, and devices fetch everything, dropping what is no longer there
    path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    del catalog[first]
    fresh = Feed(path)
    assert fresh.epoch != feed.epoch
    load(fresh, catalog)
    assert pull(fresh, phone) == len(catalog) and phone["rows"] == catalog

print("sync feed: ok")
