"""The catalog as a change feed, so a phone can keep its own copy and work offline.

The server is the only writer; a device is a read-only replica. Each photo's row (what /api/search
returns for it) gets a version: a counter that goes up whenever a load of the catalog finds rows that
are new or different. A device remembers the last version it has and asks for what changed since:

    GET /api/sync?epoch=E&since=V[&after=SHA][&limit=N]

    {"epoch": E, "photos": [rows], "deleted": [shas], "since": V2, "after": SHA2, "more": bool,
     "total": N, "trips": [...]}

Rows come in (version, sha) order and `since`/`after` are the cursor to send next time, so a device
can stop at any page and carry on later; when `more` is false it is up to date. A deletion has a place
in the same order, (the version it was deleted at, its sha), and a page's `deleted` is the deletions
between the cursor sent and the cursor returned: each one reaches a device once, on the page whose
window it falls in, and a device that is up to date is sent none (issue #86; before, every page repeated
every deletion at or after `since`). A device must apply `deleted` from every page, as it does `photos`. Versions live in a small SQLite file beside the
catalog (never in it), so a restart of the app does not make every device start again. `epoch` names
that file: if it is ever lost, the new one has a new epoch, and a device that sees an epoch it does
not know fetches everything and drops what it did not get.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from core import schema


def digest(row: dict) -> bytes:
    return hashlib.blake2b(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode(),
                           digest_size=8).digest()


class Feed:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.rows: dict[str, tuple[bytes, int]] = {}   # sha -> (digest, version it last changed at)
        self.gone: dict[str, int] = {}                 # sha -> version it was deleted at
        with closing(self._open()) as con:
            got = dict(con.execute("SELECT k, v FROM kv"))
            if "epoch" not in got:
                got = {"epoch": secrets.token_hex(8), "version": "0"}
                con.executemany("INSERT OR REPLACE INTO kv VALUES (?, ?)", got.items())
                con.commit()
            self.epoch, self.version = got["epoch"], int(got["version"])
            for sha, dg, ver, gone in con.execute("SELECT sha, digest, ver, gone FROM rows"):
                if gone:
                    self.gone[sha] = ver
                else:
                    self.rows[sha] = (dg, ver)
        self.ready = False    # until the first update(), the feed describes the catalog of the last run

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=60)
        con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        schema.ensure(con, "sync")
        return con

    def touch(self, rows: dict[str, dict]) -> int:
        """These rows may have changed (a favourite set in the app); the rest of the catalog has not been
        looked at. Those that differ get the next version; nothing is taken as gone."""
        return self.update(rows, whole=False)

    def update(self, rows: dict[str, dict], whole: bool = True) -> int:
        """Compare the catalog as now loaded (sha -> row) with the feed; whatever is new, different or
        gone gets the next version. Returns how many rows that was."""
        fresh = {sha: digest(row) for sha, row in rows.items()}
        with self.lock:
            changed = {sha: dg for sha, dg in fresh.items() if (self.rows.get(sha) or (None,))[0] != dg}
            removed = [sha for sha in self.rows if sha not in fresh] if whole else []
            if changed or removed:
                self._write(changed, removed)
            self.ready = self.ready or whole
            return len(changed) + len(removed)

    def _write(self, changed: dict[str, bytes], removed: list[str]) -> None:
        """Under the lock: these rows new or different (sha -> digest), these gone, at the next version."""
        ver = self.version + 1
        with closing(self._open()) as con, con:   # one transaction: committed, or rolled back on an error
            con.executemany("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, 0)", ((s, dg, ver) for s, dg in changed.items()))
            con.executemany("INSERT OR REPLACE INTO rows VALUES (?, NULL, ?, 1)", ((s, ver) for s in removed))
            con.execute("INSERT OR REPLACE INTO kv VALUES ('version', ?)", (str(ver),))
        for s, dg in changed.items():
            self.rows[s] = (dg, ver)
            self.gone.pop(s, None)
        for s in removed:
            del self.rows[s]
            self.gone[s] = ver
        self.version = ver

    def _agree(self, start: tuple[int, str], limit: int, now: Callable[[str], dict | None]) -> None:
        """Under the lock, before a page is cut: what it is about to send is made to agree with `now` (the member's
        view as the request has it). The rows after `start`, in order, until `limit` of them are as `now` has them:
        each one on the way that is not is put right (gone, if `now` has no row for it; its row, if it differs); and
        every deletion after `start` of a photo `now` has is taken back. All at the next version, so they come later
        in the order (specs/PhoneReplica.tla: the feed can be behind the view, or an older view can be written over a
        newer one, and the page used to send the view's rows whatever the feed said)."""
        fresh: dict[str, bytes] = {}
        removed: list[str] = []
        good = 0
        for _, sha in sorted((ver, sha) for sha, (_, ver) in self.rows.items() if (ver, sha) > start):
            if good == limit:
                break
            row = now(sha)
            if row is None:
                removed.append(sha)
            elif (dg := digest(row)) == self.rows[sha][0]:
                good += 1
            else:
                fresh[sha] = dg
        for sha, ver in self.gone.items():
            if (ver, sha) > start and (row := now(sha)) is not None:
                fresh[sha] = digest(row)
        if fresh or removed:
            self._write(fresh, removed)

    def page(self, epoch: str, since: int, after: str, limit: int, now: Callable[[str], dict | None] | None = None):
        """What a device at cursor (since, after) is missing: (shas to send, shas deleted, new cursor, more?).
        The cursor means "I have every row, and every deletion, up to this (version, sha)"; a device that is
        up to date holds (version, "~"), which sorts after every sha. `limit` counts rows; the deletions
        sent are those after the old cursor and up to the new one, so consecutive pages never repeat one.
        With `now` (sha -> the row the member's view has for it now, None if it does not show it), what the page
        sends is first made to agree with it (_agree): a page never carries a row of a photo the member does not see,
        each row it sends is the one the feed has, and a deletion it sends is of a photo the member does not see."""
        with self.lock:
            if epoch != self.epoch:
                since, after = 0, ""
            start = (since, after)
            if now is not None:
                self._agree(start, limit, now)
            todo = sorted((ver, sha) for sha, (_, ver) in self.rows.items() if (ver, sha) > start)
            more = len(todo) > limit
            cursor = todo[limit - 1] if more else (self.version, "~")
            deleted = [sha for _, sha in sorted((ver, sha) for sha, ver in self.gone.items() if start < (ver, sha) <= cursor)]
            return [sha for _, sha in todo[:limit]], deleted, cursor, more
