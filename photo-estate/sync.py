"""The catalog as a change feed, so a phone can keep its own copy and work offline.

The server is the only writer; a device is a read-only replica. Each photo's row (what /api/search
returns for it) gets a version: a counter that goes up whenever a load of the catalog finds rows that
are new or different. A device remembers the last version it has and asks for what changed since:

    GET /api/sync?epoch=E&since=V[&after=SHA][&limit=N]

    {"epoch": E, "photos": [rows], "deleted": [shas], "since": V2, "after": SHA2, "more": bool,
     "total": N, "trips": [...]}

Rows come in (version, sha) order and `since`/`after` are the cursor to send next time, so a device
can stop at any page and carry on later; when `more` is false it is up to date. Versions live in a small SQLite file beside the
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
from pathlib import Path

from core import schema


def digest(row: dict) -> bytes:
    return hashlib.blake2b(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode(),
                           digest_size=8).digest()


class Feed:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        con = self._open()
        got = dict(con.execute("SELECT k, v FROM kv"))
        if "epoch" not in got:
            got = {"epoch": secrets.token_hex(8), "version": "0"}
            con.executemany("INSERT OR REPLACE INTO kv VALUES (?, ?)", got.items())
            con.commit()
        self.epoch, self.version = got["epoch"], int(got["version"])
        self.rows: dict[str, tuple[bytes, int]] = {}   # sha -> (digest, version it last changed at)
        self.gone: dict[str, int] = {}                 # sha -> version it was deleted at
        for sha, dg, ver, gone in con.execute("SELECT sha, digest, ver, gone FROM rows"):
            if gone:
                self.gone[sha] = ver
            else:
                self.rows[sha] = (dg, ver)
        con.close()
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
            changed = [sha for sha, dg in fresh.items() if (self.rows.get(sha) or (None,))[0] != dg]
            removed = [sha for sha in self.rows if sha not in fresh] if whole else []
            if changed or removed:
                ver = self.version + 1
                con = self._open()
                con.executemany("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, 0)", ((s, fresh[s], ver) for s in changed))
                con.executemany("INSERT OR REPLACE INTO rows VALUES (?, NULL, ?, 1)", ((s, ver) for s in removed))
                con.execute("INSERT OR REPLACE INTO kv VALUES ('version', ?)", (str(ver),))
                con.commit()
                con.close()
                for s in changed:
                    self.rows[s] = (fresh[s], ver)
                    self.gone.pop(s, None)
                for s in removed:
                    del self.rows[s]
                    self.gone[s] = ver
                self.version = ver
            self.ready = self.ready or whole
            return len(changed) + len(removed)

    def page(self, epoch: str, since: int, after: str, limit: int):
        """What a device at cursor (since, after) is missing: (shas to send, shas deleted, new cursor, more?).
        The cursor means "I have every row up to this (version, sha)"; a device that is up to date holds
        (version, "~"), which sorts after every sha."""
        with self.lock:
            if epoch != self.epoch:
                since, after = 0, ""
            todo = sorted((ver, sha) for sha, (_, ver) in self.rows.items() if (ver, sha) > (since, after))
            deleted = sorted(sha for sha, ver in self.gone.items() if ver >= since)
            more = len(todo) > limit
            cursor = todo[limit - 1] if more else (self.version, "~")
            return [sha for _, sha in todo[:limit]], deleted, cursor, more
