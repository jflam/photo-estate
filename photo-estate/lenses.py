"""lenses.py — what each member sees: lenses, subscriptions, "show it anyway" and each member's favourites
(the standard library; numpy only to work a member's masks out, so `estate.py migrate` can use it). The words are the multiplayer plan's (docs/notes/MULTIPLAYER-PLAN.md).

A **lens** is a named view, a row of `lenses`, kept on top of change sets (changes.py, which goes on holding
the photos, sections, reasons and searches):

  filter   leaves photos out. Its photos are what the hiding sets that name it hide (change_sets.lens_id;
           hidden(sha256, set_id) is unchanged): each reviewed batch of hides stays its own set, applied and
           reverted on its own. A filter is always on: once a member is subscribed it changes what they see
           everywhere, and the filters a member is subscribed to add their hides together.
  series   picks and orders photos: one album (lenses.set_id), made when its owner first publishes it. It
           is opened, never on: subscribing puts it in the member's Albums and changes nothing else.

A lens is its owner's; it is private until published. An **estate default** is a published filter every
member is subscribed to unless they opt out: "Estate defaults" is the owner's hiding from before multiplayer
(`estate.py migrate` names those sets), and any member may propose a published filter of theirs as one,
which a sysadmin accepts or declines.

What a member M sees (State.masks, which catalog.Live.visible_for works a View out from):
  - shared: every photo (private photos come in phase 5: shared_for changes here, and nowhere else);
  - hidden by M's filters: what the hiding sets hide whose lens is on for M: M's own filters; every estate
    default M has not opted out of (subscriptions.mode 'opted_out', or 'snapshot'); every other published
    filter M subscribes to live. A hiding set with no lens (one from before `estate.py migrate`) hides for
    everyone, as all hiding did before;
  - less what M has said to show anyway (`shown`);
  - M's favourites: what the photo's source said (assets.favorited), for its sharers only, then M's own
    word (member_favorites). Until `migrate`, a photo with no sharer is the first sysadmin's, and the old
    `favorites` table is the first sysadmin's word too; nothing writes it any more.
A **snapshot** subscription copies what the lens holds now into one of M's own (a filter: a new filter lens
of M's and one applied hiding set; a series: a new album of M's): later changes to the lens do not follow.

  State, read(con) -> State      every lens, hiding set, subscription, shown photo and favourite: Live's looks
  State.masks(cat, member)       (shared, hide, fav) for that member, a bool per photo
  State.on(member) / hidden_for(member) / default_hidden()   which filters are on; what they hide; what the
                                 estate defaults hide (People is built without those: faces.py)
  add(con, owner, title, ...)    a lens row (and its lens.created entry), in the caller's transaction
  mine(con, member)              the member's own filter lens, made on first use ("Hidden photos": MINE_TITLE)
  estate_default(con)            the Estate defaults lens, made if there is none (both: in the caller's transaction)
  create(con, member, title)     a new filter lens of the member's
  subscribe(con, member, lens, mode, shas=None) / unsubscribe(con, member, lens)
  publish(con, member, lens, on) / publish_album(con, member, album, on)
  propose(con, member, lens) / decide(con, member, lens, accept)     (decide: a sysadmin)
  show(con, member, sha, on)     "show it anyway", and taking that back
  set_favorites(con, member, shas, on)
  listing(con, state, member) -> {lenses: [...]}     why(con, state, member, sha) -> dict
  day(at)                        a day as the pages say it: "Nov 3", or "Nov 3, 2025" in another year
  LensError                      what a member may not do, with the HTTP status to answer it with

Every write appends one journal entry (journal.py) in the same transaction and commits: lens.created,
lens.subscribed {mode, copy?, album?, photos?}, lens.unsubscribed {mode}, lens.published / lens.unpublished,
lens.proposed, lens.accepted / lens.declined (the sysadmin), album.published / album.unpublished {lens},
photo.shown / photo.unshown (subject: the sha). A write that changes nothing appends nothing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import journal

KINDS = ("filter", "series")
MODES = ("live", "snapshot")              # what a member asks for; 'opted_out' is an estate default left


class LensError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Lens:
    id: int
    owner: int | None
    title: str
    kind: str
    set_id: int | None
    published: bool
    estate_default: bool
    proposed_at: str | None
    created_at: str | None


LENS_COLS = "id, owner, title, kind, set_id, published, estate_default, proposed_at, created_at"


def _lens(row) -> Lens:
    i, owner, title, kind, sid, pub, dflt, prop, at = row
    return Lens(i, owner, title or "", kind or "filter", sid, bool(pub), bool(dflt), prop, at)


def _rows(con, q, args=()):
    try:
        return con.execute(q, args).fetchall()
    except sqlite3.OperationalError:     # a catalog the app has not opened yet (no lenses tables)
        return []


@dataclass
class State:
    """Who sees what, as the tables said when read(): every route's mask is worked out from it, through
    catalog.Live (its `looks`). Favourites set since are added in place (set_favorites, under the gate's
    write side); anything else that changes is read again (PhotoApp.looks_changed)."""
    sysadmin: int | None = None                               # the first active sysadmin: whose an owner-less row is
    lenses: dict[int, Lens] = field(default_factory=dict)
    sets: dict[int, tuple[int | None, frozenset[str]]] = field(default_factory=dict)   # hiding set -> (its lens, what it hides)
    subs: dict[int, dict[int, str]] = field(default_factory=dict)   # member -> {lens: live | snapshot | opted_out}
    shown: dict[int, set[str]] = field(default_factory=dict)        # member -> photos shown anyway
    favs: dict[int, dict[str, bool]] = field(default_factory=dict)  # member -> {sha: their word} (member_favorites)
    legacy: dict[str, bool] = field(default_factory=dict)           # the old favorites table: the first sysadmin's

    def who(self, member: int | None) -> int | None:
        return member if member is not None else self.sysadmin

    def owner(self, lens: Lens) -> int | None:
        return lens.owner if lens.owner is not None else self.sysadmin

    def on(self, member: int | None) -> set[int]:
        """The filter lenses whose hides apply to `member`."""
        m, out = self.who(member), set()
        mine = self.subs.get(m, {}) if m is not None else {}
        for lens in self.lenses.values():
            if lens.kind != "filter":
                continue
            mode = mine.get(lens.id)
            if lens.estate_default and lens.published:
                ok = mode in (None, "live")
            elif self.owner(lens) == m:
                ok = True
            else:
                ok = lens.published and mode == "live"
            if ok:
                out.add(lens.id)
        return out

    def hidden_for(self, member: int | None, but: int | None = None, anyway: bool = True) -> set[str]:
        """What `member`'s filters hide (with `anyway`, less what they show anyway); `but`: leaving that set out."""
        on = self.on(member)
        out: set[str] = set()
        for sid, (lens, shas) in self.sets.items():
            if sid != but and (lens is None or lens in on):
                out |= shas
        m = self.who(member)
        return out - self.shown.get(m, set()) if anyway and m is not None else out

    def default_hidden(self) -> set[str]:
        """What the estate defaults (and the hiding sets from before lenses) hide: People is built without it."""
        out: set[str] = set()
        for lens, shas in self.sets.values():
            if lens is None or (lens in self.lenses and self.lenses[lens].estate_default):
                out |= shas
        return out

    def shared(self, cat, member: int | None):
        """What `member` may ever be shown, a bool per photo: every photo, until private photos (multiplayer
        phase 5 changes this, and nothing else: catalog.Live.shared_for, and so every pixel route, asks it)."""
        import numpy as np
        return np.ones(cat.N, bool)

    def masks(self, cat, member: int | None):
        """(shared, hide, fav) for `member`: a bool per photo of `cat` (catalog.Looks)."""
        import numpy as np
        m, index = self.who(member), cat.index
        hide = np.zeros(cat.N, bool)
        hide[[index[s] for s in self.hidden_for(member) if s in index]] = True
        fav = np.zeros(cat.N, bool)
        for i in np.flatnonzero(cat.said_fav).tolist():   # what the source said: the sharers' favourite
            sharers = cat.fav_sharers.get(cat.shas[i])
            fav[i] = m in sharers if sharers else m == self.sysadmin
        words = dict(self.legacy) if m == self.sysadmin else {}
        words.update(self.favs.get(m, {}) if m is not None else {})
        for sha, on in words.items():
            if (i := index.get(sha)) is not None:
                fav[i] = on
        return self.shared(cat, member), hide, fav

    __call__ = masks

    def set_favorites(self, member: int | None, shas, on: bool) -> None:
        m = self.who(member)
        if m is None:
            self.legacy.update((s, on) for s in shas)
        else:
            self.favs.setdefault(m, {}).update((s, on) for s in shas)


def read(con: sqlite3.Connection) -> State:
    st = State()
    row = _rows(con, "SELECT id FROM members WHERE role='sysadmin' AND status='active' ORDER BY id LIMIT 1")
    st.sysadmin = row[0][0] if row else None
    st.lenses = {r[0]: _lens(r) for r in _rows(con, f"SELECT {LENS_COLS} FROM lenses")}
    got: dict[int, tuple[int | None, set[str]]] = {}
    rows = _rows(con, "SELECT h.set_id, s.lens_id, h.sha256 FROM hidden h LEFT JOIN change_sets s ON s.id = h.set_id") \
        or _rows(con, "SELECT set_id, NULL, sha256 FROM hidden")      # (a catalog from before change_sets.lens_id)
    for sid, lens, sha in rows:
        got.setdefault(sid, (lens, set()))[1].add(sha)
    st.sets = {sid: (lens, frozenset(shas)) for sid, (lens, shas) in got.items()}
    for mid, lid, mode in _rows(con, "SELECT member_id, lens_id, mode FROM subscriptions"):
        st.subs.setdefault(mid, {})[lid] = mode
    for sha, mid in _rows(con, "SELECT sha256, member_id FROM shown"):
        st.shown.setdefault(mid, set()).add(sha)
    for sha, mid, on in _rows(con, "SELECT sha256, member_id, fav FROM member_favorites"):
        st.favs.setdefault(mid, {})[sha] = bool(on)
    st.legacy = {s: bool(on) for s, on in _rows(con, "SELECT sha256, fav FROM favorites")}
    return st


# -- reading one lens ----------------------------------------------------------------------------------

def get(con, lid) -> Lens | None:
    row = con.execute(f"SELECT {LENS_COLS} FROM lenses WHERE id=?", (lid,)).fetchone() if isinstance(lid, int) else None
    return _lens(row) if row else None


def sysadmin_id(con) -> int | None:
    row = con.execute("SELECT id FROM members WHERE role='sysadmin' AND status='active' ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def is_sysadmin(con, member: int | None) -> bool:
    if member is None:      # (no members yet: this computer, which is the sysadmin)
        return True
    return con.execute("SELECT 1 FROM members WHERE id=? AND role='sysadmin' AND status='active'", (member,)).fetchone() is not None


def owner_of(con, lens: Lens) -> int | None:
    return lens.owner if lens.owner is not None else sysadmin_id(con)


def hides(con, lid: int) -> list[str]:
    """What a filter lens hides now: the photos its sets hide."""
    return [r[0] for r in con.execute("SELECT DISTINCT h.sha256 FROM hidden h JOIN change_sets s ON s.id = h.set_id"
                                      " WHERE s.lens_id=? ORDER BY h.sha256", (lid,))]


# -- writing ---------------------------------------------------------------------------------------------

def add(con, owner, title, kind="filter", *, set_id=None, published=False, estate_default=False, actor=None,
         acting_for=None, entry=True) -> int:
    lid = con.execute("INSERT INTO lenses(owner, title, kind, set_id, published, estate_default, created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (owner, title[:200], kind, set_id, int(published), int(estate_default),
                                                  now())).lastrowid
    assert lid is not None
    if entry:
        journal.append(con, actor if actor is not None or acting_for is not None else owner, "lens.created",
                       f"lens:{lid}", {"title": title[:200], "kind": kind, "estate_default": estate_default or None},
                       acting_for)
    return lid


MINE_TITLE = "Hidden photos"     # a member's own filter: "your lens Hidden photos", "Ben's lens Hidden photos"


def mine(con, member: int | None) -> tuple[int, bool]:
    """(the member's own filter lens, whether it was made now): where their hides go by default, made on first
    use and named plainly (MINE_TITLE: whose it is is said beside it, never in it), in the caller's
    transaction. The caller's journal entry says so (changes.py: album.applied {lens, lens_made})."""
    m = member if member is not None else sysadmin_id(con)
    row = con.execute("SELECT id FROM lenses WHERE kind='filter' AND estate_default=0 AND owner IS ? ORDER BY id LIMIT 1",
                      (m,)).fetchone()
    if row:
        return row[0], False
    return add(con, m, MINE_TITLE, entry=False), True


def estate_default(con) -> tuple[int, bool]:
    """(the Estate defaults lens, whether it was made now): the oldest estate default filter, owned by the
    first sysadmin and published, made if there is none (`estate.py migrate` makes it for the hiding from
    before lenses), in the caller's transaction, as mine()."""
    row = con.execute("SELECT id FROM lenses WHERE kind='filter' AND estate_default=1 ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0], False
    return add(con, sysadmin_id(con), "Estate defaults", published=True, estate_default=True, entry=False), True


def create(con, member: int | None, title, actor=None) -> int:
    title = " ".join(str(title or "").split())
    if not title:
        raise LensError(400, "a lens needs a title")
    with con:
        return add(con, member if member is not None else sysadmin_id(con), title, actor=actor)


def _theirs(con, lid, member) -> Lens:
    lens = get(con, lid)
    if lens is None:
        raise LensError(404, "no such lens")
    if owner_of(con, lens) != (member if member is not None else sysadmin_id(con)):
        raise LensError(403, "this lens is someone else's")
    return lens


def _visible(con, lid, member) -> Lens:
    """A lens `member` may see: their own, or a published one."""
    lens = get(con, lid)
    m = member if member is not None else sysadmin_id(con)
    if lens is None or not (lens.published or owner_of(con, lens) == m):
        raise LensError(404, "no such lens")
    return lens


def subscribe(con, member: int | None, lid, mode: str, shas=None, title: str | None = None) -> dict:
    """Subscribe `member` to a published lens of someone else's (or take an estate default back), `live` or
    as a `snapshot`: a copy, now, into a lens or album of their own. A series' snapshot copies `shas` (its
    photos as the member sees them, which the caller lists: changes.Changes.shas). One lens.subscribed entry."""
    m = member if member is not None else sysadmin_id(con)
    if mode not in MODES:
        raise LensError(400, f"mode is one of {', '.join(MODES)}")
    lens = _visible(con, lid, member)
    own = owner_of(con, lens) == m
    if own and not lens.estate_default:
        raise LensError(409, "this lens is your own: it is always on")
    if not lens.published:
        raise LensError(404, "no such lens")
    t, data = now(), {"mode": mode}
    data: dict[str, Any]
    with con:
        if mode == "snapshot":
            name = title or f"{lens.title} (a copy)"
            if lens.kind == "filter":
                photos = hides(con, lens.id)
                copy = add(con, m, name, entry=False)
                sid = con.execute("INSERT INTO change_sets(title, prompt, op, status, created_by, created_at, updated_at,"
                                  " applied_at, kind, owner, lens_id) VALUES (?,?,'hide','open',?,?,?,?,'list',?,?)",
                                  (name[:200], f"A copy of {lens.title}, as it was on {t[:10]}", f"lens:{lens.id}", t, t,
                                   t, m, copy)).lastrowid
                con.executemany("INSERT OR IGNORE INTO change_items(set_id, sha256, applied, position) VALUES (?,?,1,?)",
                                ((sid, s, k + 1) for k, s in enumerate(photos)))
                con.executemany("INSERT OR IGNORE INTO hidden VALUES (?,?,?)", ((s, sid, t) for s in photos))
                data.update(copy=copy, album=sid, photos=len(photos))
            else:
                photos = list(dict.fromkeys(shas or []))
                sid = con.execute("INSERT INTO change_sets(title, prompt, op, status, created_by, created_at, updated_at,"
                                  " kind, owner) VALUES (?,?,'view','open',?,?,?,'list',?)",
                                  (name[:200], f"A copy of {lens.title}, as it was on {t[:10]}", f"lens:{lens.id}", t, t,
                                   m)).lastrowid
                con.executemany("INSERT OR IGNORE INTO change_items(set_id, sha256, position) VALUES (?,?,?)",
                                ((sid, s, k + 1) for k, s in enumerate(photos)))
                data.update(album=sid, photos=len(photos))
        was = con.execute("SELECT mode FROM subscriptions WHERE member_id=? AND lens_id=?", (m, lens.id)).fetchone()
        if lens.estate_default and mode == "live":   # back in: an estate default needs no row
            if was is None:
                return {"lens": lens.id, "mode": "live", "changed": False}
            con.execute("DELETE FROM subscriptions WHERE member_id=? AND lens_id=?", (m, lens.id))
        else:
            if was is not None and was[0] == mode and mode == "live":
                return {"lens": lens.id, "mode": mode, "changed": False}
            con.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?)", (m, lens.id, mode, t))
        journal.append(con, member, "lens.subscribed", f"lens:{lens.id}", data)
    return {"lens": lens.id, **data, "changed": True}


def unsubscribe(con, member: int | None, lid) -> dict:
    """Leave a lens: a live or snapshot subscription ends (a snapshot's copy stays the member's), and an
    estate default is opted out of (until subscribe takes it back). One lens.unsubscribed entry."""
    m = member if member is not None else sysadmin_id(con)
    lens = _visible(con, lid, member)
    was = con.execute("SELECT mode FROM subscriptions WHERE member_id=? AND lens_id=?", (m, lens.id)).fetchone()
    with con:
        if lens.estate_default and lens.published:
            if was is not None and was[0] == "opted_out":
                return {"lens": lens.id, "mode": "opted_out", "changed": False}
            con.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?)", (m, lens.id, "opted_out", now()))
            mode = "opted_out"
        else:
            if was is None:
                return {"lens": lens.id, "mode": None, "changed": False}
            con.execute("DELETE FROM subscriptions WHERE member_id=? AND lens_id=?", (m, lens.id))
            mode = None
        journal.append(con, member, "lens.unsubscribed", f"lens:{lens.id}", {"was": was[0] if was else None, "mode": mode})
    return {"lens": lens.id, "mode": mode, "changed": True}


def publish(con, member: int | None, lid, on: bool) -> dict:
    lens = _theirs(con, lid, member)
    if lens.estate_default and not on:
        raise LensError(409, "an estate default stays published")
    if lens.published == on:
        return {"lens": lens.id, "published": on, "changed": False}
    with con:
        con.execute("UPDATE lenses SET published=?, proposed_at=CASE WHEN ? THEN proposed_at END WHERE id=?",
                    (int(on), int(on), lens.id))
        journal.append(con, member, "lens.published" if on else "lens.unpublished", f"lens:{lens.id}")
    return {"lens": lens.id, "published": on, "changed": True}


def publish_album(con, member: int | None, sid: int, on: bool) -> dict:
    """Publish an album of the member's as a series (its lenses row, made the first time), or take it back."""
    m = member if member is not None else sysadmin_id(con)
    row = con.execute("SELECT title, op, owner FROM change_sets WHERE id=? AND status IS NOT 'discarded'", (sid,)).fetchone()
    if row is None:
        raise LensError(404, "no such album")
    title, op, owner = row
    if (owner if owner is not None else sysadmin_id(con)) != m:
        raise LensError(403, "this album is someone else's")
    if op != "view":
        raise LensError(409, "a change set up for hiding is not published; a filter lens is (its hides)")
    got = con.execute("SELECT id, published FROM lenses WHERE kind='series' AND set_id=?", (sid,)).fetchone()
    if got and bool(got[1]) == on or not got and not on:
        return {"album": sid, "lens": got[0] if got else None, "published": on, "changed": False}
    with con:
        if got:
            lid = got[0]
            con.execute("UPDATE lenses SET published=?, title=? WHERE id=?", (int(on), title, lid))
        else:
            lid = add(con, m, title, "series", set_id=sid, published=True, entry=False)
        journal.append(con, member, "album.published" if on else "album.unpublished", f"album:{sid}", {"lens": lid})
    return {"album": sid, "lens": lid, "published": on, "changed": True}


def propose(con, member: int | None, lid) -> dict:
    """Propose a filter of the member's own as an estate default: it is published, and waits for a sysadmin."""
    lens = _theirs(con, lid, member)
    if lens.kind != "filter" or lens.estate_default:
        raise LensError(409, "only a filter that is not an estate default yet can be proposed")
    if lens.proposed_at:
        return {"lens": lens.id, "proposed_at": lens.proposed_at, "changed": False}
    t = now()
    with con:
        con.execute("UPDATE lenses SET published=1, proposed_at=? WHERE id=?", (t, lens.id))
        journal.append(con, member, "lens.proposed", f"lens:{lens.id}", {"title": lens.title})
    return {"lens": lens.id, "proposed_at": t, "changed": True}


def decide(con, member: int | None, lid, accept: bool) -> dict:
    """A sysadmin accepts a proposed filter as an estate default (every member then sees through it unless
    they opt out), or declines it (it stays its owner's, published)."""
    if not is_sysadmin(con, member):
        raise LensError(403, "only a sysadmin decides the estate defaults")
    lens = get(con, lid)
    if lens is None:
        raise LensError(404, "no such lens")
    if not lens.proposed_at:
        raise LensError(409, "this lens has not been proposed")
    with con:
        if accept:
            con.execute("UPDATE lenses SET estate_default=1, published=1, proposed_at=NULL WHERE id=?", (lens.id,))
        else:
            con.execute("UPDATE lenses SET proposed_at=NULL WHERE id=?", (lens.id,))
        journal.append(con, member, "lens.accepted" if accept else "lens.declined", f"lens:{lens.id}", {"title": lens.title})
    return {"lens": lens.id, "estate_default": accept, "changed": True}


def show(con, member: int | None, sha: str, on: bool) -> dict:
    """"Show it anyway": one photo the member's filters hide is shown to them all the same (or no longer)."""
    m = member if member is not None else sysadmin_id(con)
    with con:
        if on:
            n = con.execute("INSERT OR IGNORE INTO shown VALUES (?,?,?)", (sha, m, now())).rowcount
        else:
            n = con.execute("DELETE FROM shown WHERE sha256=? AND member_id=?", (sha, m)).rowcount
        if n:
            journal.append(con, member, "photo.shown" if on else "photo.unshown", sha)
    return {"sha": sha, "shown": on, "changed": bool(n)}


def set_favorites(con, member: int | None, shas, on: bool) -> None:
    """The member's word on these photos (member_favorites), in the caller's transaction."""
    m = member if member is not None else sysadmin_id(con)
    t = now()
    con.executemany("INSERT OR REPLACE INTO member_favorites VALUES (?,?,?,?)", ((s, m, int(on), t) for s in shas))


# -- for the pages ------------------------------------------------------------------------------------

def brief(con, st: State, lens: Lens, member: int | None, names: dict) -> dict:
    m = st.who(member)
    owner = st.owner(lens)
    mode = st.subs.get(m, {}).get(lens.id) if m is not None else None
    out = {**asdict(lens), "owner": {"id": owner, "name": names.get(owner)}, "mine": owner == m,
           "subscription": mode, "proposed": bool(lens.proposed_at)}
    del out["proposed_at"]
    if lens.kind == "filter":
        out["on"] = lens.id in st.on(member)
        out["photos"] = len(set().union(*(shas for sid, (lid, shas) in st.sets.items() if lid == lens.id)))
        if owner == m:
            out["sets"] = sorted(sid for sid, (lid, _) in st.sets.items() if lid == lens.id) or \
                [r[0] for r in con.execute("SELECT id FROM change_sets WHERE lens_id=? AND status IS NOT 'discarded'"
                                           " ORDER BY id", (lens.id,))]
    return out


def listing(con, st: State, member: int | None) -> dict:
    """GET /api/lenses: the member's own lenses, the estate defaults, and the lenses others have published."""
    m = st.who(member)
    names = dict(con.execute("SELECT id, name FROM members"))
    out = [brief(con, st, lens, member, names) for lens in sorted(st.lenses.values(), key=lambda x: x.id)
           if lens.published or st.owner(lens) == m]
    row = con.execute("SELECT id FROM lenses WHERE kind='filter' AND estate_default=0 AND owner IS ? ORDER BY id LIMIT 1",
                      (m,)).fetchone()
    return {"lenses": out, "mine": row[0] if row else None, "sysadmin": is_sysadmin(con, member),
            "proposed": [x["id"] for x in out if x["proposed"]]}


def why(con, st: State, member: int | None, sha: str) -> dict:
    """GET /api/photos/<sha>/why: why `member` does not see a photo, from the state tables (the hidden rows,
    their sets, the sets' lenses and the lenses' owners), never the journal. `reasons` lists each hiding set
    of a filter that is on for the member, newest first; `text` says the first in words."""
    m = st.who(member)
    on = st.on(member)
    rows = con.execute(
        "SELECT h.set_id, h.hidden_at, s.title, s.lens_id, l.title, l.owner, l.estate_default, s.owner"
        " FROM hidden h JOIN change_sets s ON s.id = h.set_id LEFT JOIN lenses l ON l.id = s.lens_id"
        " WHERE h.sha256=? ORDER BY h.hidden_at DESC, h.set_id DESC", (sha,)).fetchall()
    names = dict(con.execute("SELECT id, name FROM members"))
    reasons = []
    for sid, at, stitle, lid, ltitle, lowner, dflt, sowner in rows:
        if lid is not None and lid not in on:
            continue
        owner = lowner if lid is not None else sowner
        owner = owner if owner is not None else st.sysadmin
        reasons.append({"set": {"id": sid, "title": stitle}, "hidden_at": at,
                        "lens": {"id": lid, "title": ltitle, "estate_default": bool(dflt)} if lid is not None else None,
                        "owner": {"id": owner, "name": names.get(owner)}, "mine": owner == m})
    anyway = sha in st.shown.get(m, set()) if m is not None else False
    text = None
    if reasons:
        r = reasons[0]
        whose = "your" if r["mine"] else f"{r['owner']['name'] or 'someone'}'s"
        what = f"{whose} lens “{r['lens']['title']}”" if r["lens"] else f"{whose} change set “{r['set']['title']}”"
        when = f" on {day(r['hidden_at'])}" if day(r["hidden_at"]) else ""
        text = f"{'Shown anyway; hidden' if anyway else 'Hidden'} by {what}{when}"
    return {"sha": sha, "hidden": bool(reasons) and not anyway, "shown_anyway": anyway, "reasons": reasons, "text": text}


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day(at: str | None, today: datetime | None = None) -> str:
    """A day as the pages say it: "Nov 3", with the year when it is not this one ("Nov 3, 2025"); "" for none."""
    try:
        d = datetime.strptime((at or "")[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    year = (today or datetime.now(timezone.utc)).year
    return f"{MONTHS[d.month - 1]} {d.day}" + ("" if d.year == year else f", {d.year}")
