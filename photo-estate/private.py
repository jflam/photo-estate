#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""private.py — photos private to their sharer, and the hold at ingestion (multiplayer phase 5). The words are
the multiplayer plan's (docs/notes/MULTIPLAYER-PLAN.md).

A **sharer** is a member who shared a photo (the `sharers` table). Ingest records it (record_sharer): a phone's
upload lands in <inbox>/m<member id>/YYYY/MM/DD/... (admin.py chooses the folder from the member the request is
for), and anything else, a bulk import or an upload from before member folders, is the first sysadmin's. A copy
a second member sends adds a sharer; it is not a new photo.

A photo is **private** to a sharer while a row of `private` names them and is not released:

  new        a phone upload not checked yet. Ingest writes it in the transaction that records the photo, so no
             catalog the app loads, at any moment, has the photo without it: nobody but its sharer can be shown
             a new upload before `check` has scored it (ingest-iphone.sh runs it after the embeddings).
  held       `check` found that it looks like a receipt, a screenshot or a document (PROMPTS, HOLD_AT,
             catalog.is_screenshot, and the phone's own mark: X-Screenshot on the upload, which upload.py writes
             into its sidecar as the keyword `screenshot`), or the one-off run over the library (`library`) did
  withdrawn  its sharer took it back from the family (POST /api/private, from the viewer)

Who may be shown a photo (lenses.State.shared, from read() here): everyone, unless every one of its sharers
holds it private (rule 1: a photo is shared for M if M is one of its sharers, or some sharer has it and has not
made it private); then only its sharers. A photo with no sharer row is the first sysadmin's. The estate's agent
acting for nobody (NOBODY, or the agent's own member id: Private.agents) is shown only photos private to nobody. People is built without every photo that is
private to anyone (faces.default_hidden), its sharer included, until it is released.

Released: by its sharer (now, or by itself on the day they set: `until`, released by the app's timer,
release_due). The row stays, with released_at, so `library` never holds again what a member let go.

Looked over (the review of held photos, /held: the change-set review page in its held mode). A held photo waits
for its sharer to look it over until they release it or keep it private there (reviewed_at): a photo kept is
private exactly as before (is:held and is:private in search still list it, the viewer's line still releases it);
it only leaves the review. Every waiting photo is listed, whatever the view options, stacks or hides, likeliest
mistakes first (review_list): a named person in it, then the rest by how little it cleared HOLD_AT ("close",
under HOLD_AT + CLOSE, then "documents"), then screenshots and the surest (SURE and over), and last those the
member hides. Nothing is released but what the page names: review() takes the photos to release and those to
keep as lists, and does nothing to a photo that does not wait to be looked over (a photo held after the page
loaded is in neither list).

  PROMPTS, HOLD_AT, VECTORS        what is held, in one place (the prompts' text vectors are VECTORS, made once
                                   by `embed.py hold-prompts`: the ingest path never loads the model)
  member_of(path)                  the member an inbox path names (m<id>/...), or None
  record_sharer(con, sha, member, new, image, upload)   ingest's half, in its transaction
  Private, read(con, sysadmin)     the open rows and the sharers of those photos: who may be shown what
  scores(vecs, model) / reasons(row, shot)    how a photo is scored, and why it would be held
  check(con, model)                the ingest step: every `new` row with a vector is held or let go
  library(con, member, model, dry_run, out)   the one-off run over a member's photos
  change(con, member, shas, private, until=KEEP)   withdraw, release, or set the day it is released on
  counts(con, member)              GET /api/held?count=1: {held, waiting} (waiting: not yet looked over)
  review_list(con, member, model, named, hidden)   GET /api/held: every held photo waiting to be looked over,
                                   in order, each with its section, why it was held and its scores
  review(con, member, release, keep)   POST /api/held/review: release these, keep those private and looked over
  close_held(con, member)          the member's held inbox item: how many wait, or done when none does
  SECTIONS, CLOSE, SURE            the review's sections, in order, and where its bands start
  release_due(con, today)          the timer's: every row whose day has come is released
  tombstones(con, member)          GET /api/tombstones: the member's own opt-outs, not lifted (hashes only)
  change_tombstones(con, member, add, lift)   POST /api/tombstones: opt out of hashes, or take opt-outs back
  refused_upload(con, sha, member) "opted_out" (that member's), "purged" (anyone's) or None: an upload refused
  PrivateError                     what a member may not do, with the HTTP status

  uv run private.py --db estate.db check                           (what ingest-iphone.sh runs)
  uv run private.py --db estate.db library --member 1 --dry-run    (then without --dry-run)

Every write appends one journal entry per member and action in the same transaction (journal.py): photo.held
{photos, how: upload | library} (no actor: the app, acting for the sharer), photo.withdrawn {photos, until},
photo.release_set {photos, until}, photo.released {photos} (by the sharer) or {photos, auto, until} (the timer:
no actor, acting for the sharer), photo.reviewed {photos} (by the sharer: those a review kept private, one entry
per submit, beside the photo.released of those it released); subject the sha for one photo, else "photos". A
`new` row that `check` lets go was never private to anyone in a way anyone saw, and is not journaled
(ingest.finished says the photo came). The inbox (member_inbox.py): a member has one open `held` item while any
held photo of theirs waits to be looked over, opening the review (/held) and saying how many wait; a later hold
joins it (its ref stays held:<the batch that opened it>), and it is done once none waits (looked over, or
released by any way).

Tombstones (multiplayer phase 6, the phone gate): a photo a member opted out of on their phone is a row of
`tombstones` (why opted_out) with their member id and the SHA-256 of the file, as the store keys it. It refuses
*that member's* upload of those bytes (admin.py, before the body when X-Content-SHA256 names it, else dropped
from the staging file once hashed), never another member's copy, and it is no withdraw: a photo already in the
estate stays as it is. Only its member lifts it (lifted_at; opting out again clears it). A purge's (member 0)
refuses everyone's and is never lifted (core/schema.py's triggers). Journaled once per call: tombstones.changed
{opted_out, lifted}, by the member.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import journal
import member_inbox

HERE = Path(__file__).resolve().parent
NOBODY = 0                 # the member an agent acting for no one reads as: only what is private to nobody
WHYS = ("new", "held", "withdrawn")
# What is held at ingestion. Each prompt is compared with a photo's SigLIP 2 vector (a cosine); a photo is held
# when any of them reaches HOLD_AT, or when catalog.is_screenshot says it is a screenshot. Set on the demo
# library and on made receipts, screenshots and documents (docs/notes/MULTIPLAYER-PLAN.md, phase 5's Built
# note): the demo's 104 photos score at most 0.058 on any prompt; made receipts 0.13-0.17 ("a receipt"),
# documents 0.14 ("a document"), phone screenshots 0.096-0.134 ("a screenshot of a phone"). It was first 0.075,
# below a search's own floor (0.085, embed.MATCH_FLOOR); the dry run on the real library held 12,005 photos
# there and 7,999 at 0.095, with no gap in the scores, and the owner chose 0.095 (2026-11-23): fewer to look
# over, and a phone's screenshots are held by their mark whatever they score. Check a change with `library --dry-run`.
PROMPTS = ("a receipt", "a screenshot of a phone", "a document")
HOLD_AT = 0.095
VECTORS = HERE / "hold-prompts.json"       # {model: {prompt: base64 float16 unit vector}}: embed.py hold-prompts
KEEP = object()            # change(): leave `until` as it is
# The review of held photos (/held), likeliest mistakes first: a named person in it ("named"); then by its best
# score, lowest first, just over the line (under HOLD_AT + CLOSE: "close") and the rest ("documents"); then the
# screenshots (catalog.is_screenshot, or the phone's mark) and those at SURE or over ("sure"); and last whatever
# the member hides from their own library ("hidden"). Made receipts score 0.13-0.17 and documents 0.14 (HOLD_AT's
# note), so SURE is where a photo is very likely what it was held for.
SECTIONS = ("named", "close", "documents", "sure", "hidden")
CLOSE = 0.01
SURE = 0.13


class PrivateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunks(xs, n=500):
    xs = list(xs)
    return (xs[i:i + n] for i in range(0, len(xs), n))


def member_of(path: str) -> int | None:
    """The member a phone upload's inbox path names: m<id>/YYYY/... -> id; None for any other path."""
    m = re.match(r"m(\d{1,9})/", path)
    return int(m[1]) if m else None


def _sysadmin(con) -> int | None:
    row = con.execute("SELECT id FROM members WHERE role='sysadmin' AND status='active' ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def _holders(con, sha: str) -> set[int]:
    return {m for m, in con.execute("SELECT member_id FROM private WHERE sha256=? AND released_at IS NULL", (sha,))}


def record_sharer(con, sha: str, member: int | None, new: bool, image: bool, upload: bool,
                  sysadmin: int | None = None) -> bool:
    """Ingest's half, in its transaction: `member` shared this photo (`new`: ingest just added it). A photo that
    was in the estate with no sharer row was the first sysadmin's, so that is written first. A phone upload
    (`upload`) of an image nobody else is shown yet (new, or every sharer holds it private) is private to its
    sharer until check() has scored it: a `new` row. True if it made one."""
    if member is None:
        return False
    had = {m for m, in con.execute("SELECT member_id FROM sharers WHERE sha256=?", (sha,))}
    if member in had:
        return False
    t = now()
    if not had and not new and sysadmin is not None and sysadmin != member:
        con.execute("INSERT OR IGNORE INTO sharers(sha256, member_id, shared_at) VALUES (?,?,?)", (sha, sysadmin, t))
        had = {sysadmin}
    con.execute("INSERT OR IGNORE INTO sharers(sha256, member_id, shared_at) VALUES (?,?,?)", (sha, member, t))
    if not (upload and image) or not (new or had <= _holders(con, sha)):
        return False
    con.execute("INSERT OR IGNORE INTO private(sha256, member_id, why, since) VALUES (?,?,'new',?)", (sha, member, t))
    return True


# -- who may be shown what ------------------------------------------------------------------------------

@dataclass
class Private:
    """The open rows of `private` (sha -> {member: (why, since, until)}) and the sharers of those photos only:
    everything else is shown to everyone. read() builds it; lenses.State keeps one."""
    rows: dict[str, dict[int, tuple[str, str | None, str | None]]] = field(default_factory=dict)
    sharers: dict[str, frozenset[int]] = field(default_factory=dict)
    sysadmin: int | None = None
    agents: frozenset[int] = frozenset()     # members of kind agent: each is shown what NOBODY is (the archivist)

    def to(self, sha: str) -> frozenset[int] | None:
        """Who may be shown the photo: None for everyone; else its sharers, every one of whom holds it private."""
        held = self.rows.get(sha)
        if not held:
            return None
        sharers = self.sharers.get(sha) or (frozenset({self.sysadmin}) if self.sysadmin is not None else frozenset())
        return None if sharers - held.keys() else sharers

    def refused(self, sha: str, member: int | None) -> bool:
        """`member` may not be shown this photo (None: the first sysadmin; NOBODY, or the estate's agent as itself:
        what is private to nobody)."""
        if member == NOBODY or member in self.agents:
            return sha in self.rows
        to = self.to(sha)
        m = member if member is not None else self.sysadmin
        return to is not None and m not in to

    def out_for(self, member: int | None) -> list[str]:
        """Every photo `member` may not be shown."""
        return [s for s in self.rows if self.refused(s, member)]

    def mine(self, member: int | None) -> dict[str, dict]:
        """The photos private to `member`: sha -> {why, since, until}."""
        m = member if member is not None else self.sysadmin
        return {s: {"why": r[m][0], "since": r[m][1], "until": r[m][2]} for s, r in self.rows.items() if m in r}


def read(con, sysadmin: int | None = None) -> Private:
    try:
        agents = frozenset(m for m, in con.execute("SELECT id FROM members WHERE kind='agent'"))
    except sqlite3.OperationalError:       # a catalog with no members table
        agents = frozenset()
    try:
        rows = con.execute("SELECT sha256, member_id, why, since, until FROM private WHERE released_at IS NULL").fetchall()
    except sqlite3.OperationalError:       # a catalog from before private photos
        return Private(sysadmin=sysadmin, agents=agents)
    p = Private(sysadmin=sysadmin, agents=agents)
    for sha, m, why, since, until in rows:
        p.rows.setdefault(sha, {})[m] = (why, since, until)
    got: dict[str, set[int]] = {}
    for part in chunks(p.rows):
        for sha, m in con.execute(f"SELECT sha256, member_id FROM sharers WHERE sha256 IN ({','.join('?' * len(part))})", part):
            got.setdefault(sha, set()).add(m)
    p.sharers = {s: frozenset(ms) for s, ms in got.items()}
    return p


# -- scoring --------------------------------------------------------------------------------------------

class MissingVectors(RuntimeError):
    pass


def prompt_vectors(model: str):
    """PROMPTS' text vectors for `model` (P x d, unit rows), from VECTORS. MissingVectors if it lacks any: then
    nothing is let go (a `new` photo stays private to its sharer) until `embed.py hold-prompts` has made them."""
    import numpy as np
    try:
        got = json.loads(VECTORS.read_text()).get(model) or {}
    except (OSError, ValueError):
        got = {}
    if any(p not in got for p in PROMPTS):
        raise MissingVectors(f"{VECTORS.name} has no vectors for {model}: run `embed.py hold-prompts`")
    return np.stack([np.frombuffer(base64.b64decode(got[p]), np.float16).astype(np.float32) for p in PROMPTS])


def scores(vecs, model: str, prompts=None):
    """Each photo's cosine with each prompt (n x len(PROMPTS)), from their unit vectors (n x d)."""
    import numpy as np
    P = prompt_vectors(model) if prompts is None else prompts
    return np.asarray(vecs, np.float32) @ P.T


def reasons(row, shot: bool, marked: bool = False) -> list[str]:
    """Why a photo would be held, from its scores (one per prompt), whether it is a screenshot (catalog.is_screenshot)
    and whether the phone said it is one (`marked`: X-Screenshot on its upload, a keyword in its sidecar): [] for none."""
    out = [p for p, s in zip(PROMPTS, row) if s >= HOLD_AT]
    return out + (["is_screenshot"] if shot else []) + (["phone_screenshot"] if marked else [])


SCREENSHOT_KEYWORD = "screenshot"   # what upload.xmp_for writes for X-Screenshot, and estate.parse_xmp reads back


def _facts(con, shas) -> dict[str, dict]:
    """What catalog.is_screenshot reads, from the catalog's tables, and `marked`: a sidecar of one of its files
    carries the phone's screenshot keyword."""
    out: dict[str, dict] = {}
    for part in chunks(shas):
        q = ",".join("?" * len(part))
        for sha, ext, w, h in con.execute(f"SELECT sha256, ext, width, height FROM assets WHERE sha256 IN ({q})", part):
            out[sha] = {"ext": ext, "w": w, "h": h, "camera": None, "lens": None, "path": None, "marked": False}
        for sha, data in con.execute(f"SELECT o.sha256, x.data FROM occurrences o JOIN xmp x ON x.dir = o.dir"
                                     f" AND x.filename = o.filename || '.xmp' WHERE o.sha256 IN ({q})", part):
            try:
                words = json.loads(data).get("keywords") or []
            except (TypeError, ValueError, AttributeError):
                continue
            if sha in out and SCREENSHOT_KEYWORD in [str(w).lower() for w in words]:
                out[sha]["marked"] = True
        try:
            for sha, camera, lens in con.execute(f"SELECT sha256, camera, lens FROM exif WHERE sha256 IN ({q})", part):
                if sha in out:
                    out[sha].update(camera=camera, lens=lens)
        except sqlite3.OperationalError:
            pass
        for sha, path in con.execute(f"SELECT sha256, path FROM occurrences WHERE sha256 IN ({q}) ORDER BY id", part):
            if sha in out and out[sha]["path"] is None:
                out[sha]["path"] = path
    return out


def _vectors(con, model: str, shas):
    """(the photos of `shas` that have a vector, their vectors as an n x d float32 matrix)."""
    import numpy as np
    got, vecs = [], []
    for part in chunks(shas):
        for sha, vec in con.execute(f"SELECT sha256, vec FROM embeddings WHERE model=? AND vec IS NOT NULL AND sha256 IN"
                                    f" ({','.join('?' * len(part))})", [model, *part]):
            got.append(sha)
            vecs.append(np.frombuffer(vec, np.float16))
    return got, (np.stack(vecs).astype(np.float32) if vecs else np.zeros((0, 1), np.float32))


def judge(con, model: str, shas) -> dict[str, tuple[list[str], list[float]]]:
    """sha -> (why it would be held, its score for each prompt), for those of `shas` that have a vector."""
    from catalog import is_screenshot
    got, M = _vectors(con, model, shas)
    if not got:
        return {}
    S, facts = scores(M, model), _facts(con, got)
    return {sha: (reasons(S[i], is_screenshot(facts.get(sha) or {}), bool((facts.get(sha) or {}).get("marked"))),
                  [round(float(x), 4) for x in S[i]])
            for i, sha in enumerate(got)}


def _title(n: int) -> str:
    """The held item's title: how many photos wait to be looked over."""
    one = n == 1
    return (f"{n:,} photo{'' if one else 's'} kept private to you, waiting for you to look {'it' if one else 'them'} over:"
            f" {'it looks' if one else 'they look'} like a receipt, a screenshot or a document")


def _open_held(con, member: int) -> list[str]:
    """The refs of `member`'s open held items, newest first."""
    return [r for r, in con.execute("SELECT ref FROM inbox_items WHERE member_id=? AND kind='held' AND state='open'"
                                    " ORDER BY updated_at DESC, id DESC", (member,))]


def _hold(con, member: int, shas: list[str], how: str, t: str) -> int:
    """Hold `shas` for `member` (rows that exist become held, to be looked over again; the rest are made), one
    photo.held entry, and their held inbox item: the one open already (brought up to date and to the top), else
    a new one, held:<batch>. In the caller's transaction. Returns the batch (the entry's id)."""
    batch = journal.append(con, None, "photo.held", shas[0] if len(shas) == 1 else "photos", {"photos": len(shas), "how": how},
                           member)         # (no actor: the app held them, for their sharer)
    for sha in shas:
        con.execute("INSERT INTO private(sha256, member_id, why, since, batch) VALUES (?,?,'held',?,?)"
                    " ON CONFLICT(sha256, member_id) DO UPDATE SET why='held', since=excluded.since, batch=excluded.batch,"
                    " until=NULL, released_at=NULL, reviewed_at=NULL", (sha, member, t, batch))
    refs = _open_held(con, member)
    ref = refs[0] if refs else f"held:{batch}"
    member_inbox.put(con, [member], "held", ref, _title(counts(con, member)["waiting"]))
    close_held(con, member, ref)       # (any other open one, from before one item held them all, is done)
    return batch


def check(con, model: str, out=print) -> dict:
    """The ingest step (ingest-iphone.sh, after the embeddings and before the app reloads): every `new` row whose
    photo has a vector is scored: held (photo.held, and a held inbox item per sharer), or let go (the row goes:
    the photo is shared). A photo with no vector yet keeps its row and is scored by the next run; it is in no
    view meanwhile (catalog.load reads only photos with a vector). MissingVectors (nothing changes) without the
    prompts' vectors. Safe to run again."""
    rows = con.execute("SELECT sha256, member_id FROM private WHERE why='new' AND released_at IS NULL").fetchall()
    if not rows:
        out("hold: nothing new to check")
        return {"checked": 0, "held": 0, "waiting": 0}
    found = judge(con, model, {s for s, _ in rows})
    held: dict[int, list[str]] = {}
    let_go: list[tuple[str, int]] = []
    for sha, m in rows:
        if sha not in found:            # no vector yet: it stays private to its sharer, and waits
            continue
        if found[sha][0]:
            held.setdefault(m, []).append(sha)
        else:
            let_go.append((sha, m))
    t = now()
    with con:
        con.executemany("DELETE FROM private WHERE sha256=? AND member_id=? AND why='new'", let_go)
        for m, shas in held.items():
            _hold(con, m, shas, "upload", t)
    n = sum(len(v) for v in held.values())
    waiting = len(rows) - len(let_go) - n
    out(f"hold: {len(rows) - waiting} new photos checked, {n} held as private"
        + (f", {waiting} waiting for their vectors" if waiting else ""))
    return {"checked": len(rows) - waiting, "held": n, "waiting": waiting}


def library(con, member: int, model: str, dry_run: bool = False, out=print, sample: int = 20) -> dict:
    """The one-off run over the library (open question 3): every image `member` shared (for the first sysadmin,
    every image with no sharer too) that has no private row of theirs, open or released, is scored as check()
    scores an upload, and what crosses is held as theirs: one photo.held entry {photos, how: library} and one
    held inbox item. With `dry_run` nothing changes; it prints, with no pixels and no file names, how many each
    prompt would hold, the scores near HOLD_AT, and a sample of ids. Safe to run again: what it held, and what
    the member released, has a row."""
    import numpy as np
    if not con.execute("SELECT 1 FROM members WHERE id=?", (member,)).fetchone():
        raise PrivateError(404, f"no member {member}")
    first = _sysadmin(con) == member
    q = ("SELECT a.sha256 FROM assets a WHERE a.kind='image' AND (EXISTS (SELECT 1 FROM sharers s WHERE s.sha256=a.sha256"
         " AND s.member_id=:m)" + (" OR NOT EXISTS (SELECT 1 FROM sharers s WHERE s.sha256=a.sha256)" if first else "") +
         ") AND NOT EXISTS (SELECT 1 FROM private p WHERE p.sha256=a.sha256 AND p.member_id=:m) ORDER BY a.sha256")
    shas = [s for s, in con.execute(q, {"m": member})]
    found: dict[str, tuple[list[str], list[float]]] = {}
    for part in chunks(shas, 5000):
        found.update(judge(con, model, part))
    hold = sorted(s for s, (why, _) in found.items() if why)
    report = {"member": member, "photos": len(shas), "scored": len(found), "held": len(hold),
              "by_reason": {r: sum(r in why for why, _ in found.values()) for r in (*PROMPTS, "is_screenshot", "phone_screenshot")}}
    best = np.array([max(sc) for _, sc in found.values()]) if found else np.zeros(0)
    edges = [round(HOLD_AT + d / 1000, 3) for d in range(-30, 35, 5)]
    report["near"] = [{"from": a, "to": b, "photos": int(((best >= a) & (best < b)).sum())} for a, b in zip(edges, edges[1:])]
    rng = random.Random(0)
    below = sorted((s for s, (why, _) in found.items() if not why), key=lambda s: -max(found[s][1]))
    report["sample_held"] = [{"id": s, "why": found[s][0], "scores": found[s][1]} for s in rng.sample(hold, min(sample, len(hold)))]
    report["sample_just_below"] = [{"id": s, "scores": found[s][1]} for s in below[:max(1, sample // 2)]]
    if dry_run:
        out(f"dry run for member {member}: nothing is changed. {len(shas)} photos to look at, {len(found)} with a vector")
        out(f"would hold {len(hold)} (any prompt at or over {HOLD_AT}, or a screenshot):")
        for r, n in report["by_reason"].items():
            out(f"  {r:26s} {n}")
        out("best score of each photo, near the threshold:")
        for b in report["near"]:
            out(f"  {b['from']:.3f}-{b['to']:.3f} {'*' if b['from'] >= HOLD_AT else ' '} {b['photos']}")
        out(f"a sample of what it would hold (ids; scores in the order {', '.join(PROMPTS)}):")
        for x in report["sample_held"]:
            out(f"  {x['id']}  {x['scores']}  {', '.join(x['why'])}")
        out("the highest-scoring photos it would not hold:")
        for x in report["sample_just_below"]:
            out(f"  {x['id']}  {x['scores']}")
        return report
    if hold:
        with con:
            report["batch"] = _hold(con, member, hold, "library", now())
    out(f"held {len(hold)} of member {member}'s {len(shas)} photos as private to them"
        + ("" if hold else ": nothing new to hold"))
    return report


# -- the sharer's own word -------------------------------------------------------------------------------

def close_held(con, member: int, keep: str | None = None) -> int:
    """`member`'s held inbox item, after a change: once none of their held photos waits to be looked over, every
    open one is done; else one stays open (`keep`, else the newest), its title saying how many wait (retitled,
    not moved up: a review is not news), and any other is done. In the caller's transaction. How many it closed."""
    refs = _open_held(con, member)
    if not refs:
        return 0
    n = counts(con, member)["waiting"]
    stay = (keep if keep in refs else refs[0]) if n else None
    if stay is not None:
        member_inbox.retitle(con, "held", stay, member, _title(n))
    return sum(member_inbox.close(con, "held", ref, member) for ref in refs if ref != stay)


def counts(con, member: int) -> dict:
    """GET /api/held?count=1: how many photos are held for `member` (`held`: open held rows, looked over or not)
    and how many of them wait to be looked over (`waiting`)."""
    held, wait = con.execute("SELECT count(*), coalesce(sum(reviewed_at IS NULL), 0) FROM private WHERE member_id=?"
                             " AND why='held' AND released_at IS NULL", (member,)).fetchone()
    return {"held": held, "waiting": wait}


def waiting(con, member: int) -> list[str]:
    """Every held photo of `member`'s that waits to be looked over: open, held, not reviewed."""
    return [s for s, in con.execute("SELECT sha256 FROM private WHERE member_id=? AND why='held' AND released_at IS NULL"
                                    " AND reviewed_at IS NULL ORDER BY sha256", (member,))]


def section(why: list[str], sc: list[float] | None, named: bool, hidden: bool) -> str:
    """Where a held photo goes in the review (SECTIONS): from why it is held, its scores (None: no vector), whether
    a person is named in it and whether the member hides it."""
    if hidden:
        return "hidden"
    if named:
        return "named"
    best = max(sc) if sc else None
    if "is_screenshot" in why or "phone_screenshot" in why or (best is not None and best >= SURE):
        return "sure"
    return "close" if best is not None and best < HOLD_AT + CLOSE else "documents"


def review_list(con, member: int, model: str, named=lambda sha: False, hidden=lambda sha: False) -> dict:
    """GET /api/held: every held photo of `member`'s that waits to be looked over, whatever their view options,
    stacks or hides (`hidden(sha)`: they hide it; `named(sha)`: a person is named in it), in the review's order:
    by section (SECTIONS), then best score, lowest first (no vector: last), then id, so the same rows are always in
    the same order. Each {sha, section, why, scores}: why it is held now (reasons) and its score for each prompt,
    from the stored vectors and VECTORS, as check() scores an upload (None for a photo with no vector). With the
    counts (`held`, `waiting`), the sections that have photos ({key, n}, in order) and HOLD_AT."""
    shas = waiting(con, member)
    found: dict[str, tuple[list[str], list[float]]] = {}
    try:
        for part in chunks(shas, 5000):
            found.update(judge(con, model, part))
    except MissingVectors:             # (scores unknown: every photo is listed all the same, in id order)
        found = {}
    items = []
    for sha in shas:
        why, sc = found.get(sha, ([], None))
        items.append({"sha": sha, "section": section(why, sc, bool(named(sha)), bool(hidden(sha))), "why": why, "scores": sc})
    items.sort(key=lambda it: (SECTIONS.index(it["section"]), max(it["scores"]) if it["scores"] else 9.0, it["sha"]))
    n = {k: 0 for k in SECTIONS}
    for it in items:
        n[it["section"]] += 1
    return {"items": items, "sections": [{"key": k, "n": n[k]} for k in SECTIONS if n[k]], **counts(con, member),
            "hold_at": HOLD_AT, "prompts": list(PROMPTS)}


def review(con, member: int, release=(), keep=()) -> dict:
    """POST /api/held/review: `member` looked these held photos over: those in `release` are released (the family
    sees them), those in `keep` stay private to them and leave the review (reviewed_at). Only a photo that waits to
    be looked over is touched; any other sha (released or looked over already, held after the page loaded and so in
    neither list, someone else's, or none at all) is skipped and counted, the same for each. ValueError for
    anything that is not two lists of hashes, or a hash in both. One photo.released and one photo.reviewed entry
    (by the member) for what changed; the held item says how many still wait, or is done."""
    if not member:
        raise PrivateError(403, "only a member's photos are private")
    for xs in (release, keep):
        if not isinstance(xs, (list, tuple)) or len(xs) > 50000 or not all(isinstance(s, str) and SHA.fullmatch(s) for s in xs):
            raise ValueError("release and keep are lists of photo ids (SHA-256, lower-case hex)")
    release, keep = list(dict.fromkeys(release)), list(dict.fromkeys(keep))
    if set(release) & set(keep):
        raise ValueError("a photo cannot be released and kept private at once")
    t, ready = now(), set()
    with con:
        for part in chunks(release + keep):
            ready.update(s for s, in con.execute(
                f"SELECT sha256 FROM private WHERE member_id=? AND why='held' AND released_at IS NULL AND reviewed_at IS NULL"
                f" AND sha256 IN ({','.join('?' * len(part))})", [member, *part]))
        rel, kept = [s for s in release if s in ready], [s for s in keep if s in ready]
        con.executemany("UPDATE private SET released_at=?, reviewed_at=? WHERE sha256=? AND member_id=? AND released_at IS NULL",
                        ((t, t, s, member) for s in rel))
        con.executemany("UPDATE private SET reviewed_at=? WHERE sha256=? AND member_id=? AND released_at IS NULL",
                        ((t, s, member) for s in kept))
        if rel:
            journal.append(con, member, "photo.released", rel[0] if len(rel) == 1 else "photos", {"photos": len(rel)})
        if kept:
            journal.append(con, member, "photo.reviewed", kept[0] if len(kept) == 1 else "photos", {"photos": len(kept)})
        close_held(con, member)
        left = counts(con, member)
    return {"released": len(rel), "kept": len(kept), "skipped": len(release) + len(keep) - len(rel) - len(kept),
            "changed": bool(rel or kept), **left}


def day(s) -> str | None:
    """A release day as the API takes it ("2026-12-26"), or None; ValueError for anything else."""
    if s is None:
        return None
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", s):
        raise ValueError("until is a day, YYYY-MM-DD, or null")
    datetime.strptime(s, "%Y-%m-%d")
    return s


def change(con, member: int, shas, private: bool, until=KEEP, sharers_of=None) -> dict:
    """POST /api/private: `member` makes these photos private (`private` true: withdrawn, from a photo of theirs
    they share with the family; already private: its release day set, if `until` is given) or releases them
    (false: now). Only a sharer may (PrivateError 403 for a photo they did not share; the caller has already
    answered 404 for one they may not be shown). One journal entry per action; the held items it empties close."""
    shas = list(dict.fromkeys(s for s in shas if isinstance(s, str)))
    until = until if until is KEEP else day(until)
    t, out = now(), {"withdrawn": 0, "released": 0, "release_set": 0}
    first = _sysadmin(con)
    with con:
        for sha in shas:
            sharers = {m for m, in con.execute("SELECT member_id FROM sharers WHERE sha256=?", (sha,))} or {first}
            if member not in sharers:
                raise PrivateError(403, "only someone who shared a photo can make it private or release it")
        rows = {s: (why, u) for s, why, u in con.execute(
            f"SELECT sha256, why, until FROM private WHERE member_id=? AND released_at IS NULL AND sha256 IN"
            f" ({','.join('?' * len(shas))})", [member, *shas])} if shas else {}
        if private:
            fresh = [s for s in shas if s not in rows]
            for s in fresh:
                con.execute("INSERT INTO private(sha256, member_id, why, since, until) VALUES (?,?,'withdrawn',?,?)"
                            " ON CONFLICT(sha256, member_id) DO UPDATE SET why='withdrawn', since=excluded.since,"
                            " until=excluded.until, released_at=NULL, batch=NULL",
                            (s, member, t, None if until is KEEP else until))
            moved = [s for s in shas if s in rows and until is not KEEP and rows[s][1] != until]
            con.executemany("UPDATE private SET until=? WHERE sha256=? AND member_id=? AND released_at IS NULL",
                            ((until, s, member) for s in moved))
            if fresh:
                journal.append(con, member, "photo.withdrawn", fresh[0] if len(fresh) == 1 else "photos",
                               {"photos": len(fresh), **({"until": until} if until not in (KEEP, None) else {})})
            if moved:
                journal.append(con, member, "photo.release_set", moved[0] if len(moved) == 1 else "photos",
                               {"photos": len(moved), "until": until})
            out.update(withdrawn=len(fresh), release_set=len(moved))
        else:
            gone = [s for s in shas if s in rows]
            con.executemany("UPDATE private SET released_at=? WHERE sha256=? AND member_id=? AND released_at IS NULL",
                            ((t, s, member) for s in gone))
            if gone:
                journal.append(con, member, "photo.released", gone[0] if len(gone) == 1 else "photos", {"photos": len(gone)})
                close_held(con, member)
            out["released"] = len(gone)
    return {**out, "changed": any(out.values())}


def about(con, st: Private, member: int | None, sha: str) -> dict:
    """What the viewer's private line says of a photo `member` may be shown (GET /api/photos/<sha>/why's
    `private`): whether they shared it (and so may withdraw it), whether it is private to them now ({why,
    since, until}), and how many other sharers do not hold it private (while any do, the family still sees it)."""
    m = member if member is not None else st.sysadmin
    sharers = {r for r, in con.execute("SELECT member_id FROM sharers WHERE sha256=?", (sha,))} or \
        ({st.sysadmin} if st.sysadmin is not None else set())
    rows = st.rows.get(sha, {})
    mine = rows.get(m) if m is not None else None
    return {"sharer": m in sharers,
            "mine": {"why": mine[0], "since": mine[1], "until": mine[2]} if mine else None,
            "others_sharing": len(sharers - {m} - rows.keys()) if m in sharers else None}


def release_due(con, today: str) -> dict[int, int]:
    """The timer's (PhotoApp.release_timer): every private row whose release day (`until`) is `today` or before is
    released, one photo.released {photos, auto: true} entry per member, by no actor (the app). {member: photos}."""
    rows = con.execute("SELECT sha256, member_id FROM private WHERE released_at IS NULL AND until IS NOT NULL AND until <= ?",
                       (today,)).fetchall()
    if not rows:
        return {}
    t, by = now(), {}
    for sha, m in rows:
        by.setdefault(m, []).append(sha)
    with con:
        for m, shas in by.items():
            con.executemany("UPDATE private SET released_at=? WHERE sha256=? AND member_id=? AND released_at IS NULL",
                            ((t, s, m) for s in shas))
            journal.append(con, None, "photo.released", shas[0] if len(shas) == 1 else "photos",
                           {"photos": len(shas), "auto": True, "until": today}, m)
            close_held(con, m)
    return {m: len(s) for m, s in by.items()}


# -- tombstones: the phone gate's opt-outs ---------------------------------------------------------------

SHA = re.compile(r"[0-9a-f]{64}")


def tombstones(con, member: int) -> list[str]:
    """GET /api/tombstones: the hashes `member` opted out of and has not taken back (a new phone's first read)."""
    try:
        return [s for s, in con.execute("SELECT sha256 FROM tombstones WHERE member_id=? AND why='opted_out'"
                                        " AND lifted_at IS NULL ORDER BY sha256", (member,))]
    except sqlite3.OperationalError:       # a catalog from before tombstones
        return []


def change_tombstones(con, member: int, add=(), lift=()) -> dict:
    """POST /api/tombstones: `member` opts out of the hashes in `add` (a new row, or one they lifted made to stand
    again) and takes back those in `lift` (lifted_at). Their own only: a purge (member 0) is never theirs.
    ValueError for anything that is not a list of hashes, or a hash in both. One tombstones.changed entry when
    anything changed."""
    if not member:
        raise PrivateError(403, "only a member opts out")
    for xs in (add, lift):
        if not isinstance(xs, (list, tuple)) or len(xs) > 20000 or not all(isinstance(s, str) and SHA.fullmatch(s) for s in xs):
            raise ValueError("add and lift are lists of SHA-256 hashes (lower-case hex)")
    add, lift = list(dict.fromkeys(add)), list(dict.fromkeys(lift))
    if set(add) & set(lift):
        raise ValueError("a hash cannot be opted out of and taken back at once")
    t, added, lifted = now(), 0, 0
    with con:
        for sha in add:
            row = con.execute("SELECT lifted_at FROM tombstones WHERE sha256=? AND member_id=?", (sha, member)).fetchone()
            if row is None:
                con.execute("INSERT INTO tombstones(sha256, member_id, why, at) VALUES (?,?,'opted_out',?)", (sha, member, t))
                added += 1
            elif row[0] is not None:
                added += con.execute("UPDATE tombstones SET lifted_at=NULL WHERE sha256=? AND member_id=?", (sha, member)).rowcount
        for sha in lift:
            lifted += con.execute("UPDATE tombstones SET lifted_at=? WHERE sha256=? AND member_id=? AND lifted_at IS NULL",
                                  (t, sha, member)).rowcount
        if added or lifted:
            one = (add + lift)[0] if len(add) + len(lift) == 1 else "photos"
            journal.append(con, member, "tombstones.changed", one, {"opted_out": added, "lifted": lifted})
    return {"opted_out": added, "lifted": lifted, "changed": bool(added or lifted)}


def refused_upload(con, sha: str, member: int | None) -> str | None:
    """Why an upload of these bytes from `member` is refused: "purged" (the sysadmin purged the photo: from anyone),
    "opted_out" (this member opted out of it on their phone), or None."""
    try:
        rows = con.execute("SELECT member_id FROM tombstones WHERE sha256=? AND lifted_at IS NULL AND member_id IN (0, ?)",
                           (sha.lower(), member if member is not None else 0)).fetchall()
    except sqlite3.OperationalError:       # a catalog from before tombstones
        return None
    ms = {m for m, in rows}
    return "purged" if 0 in ms else "opted_out" if ms else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="estate.db")
    ap.add_argument("--model", default="google/siglip2-base-patch16-256")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="score every new phone upload: hold it, or let it go (ingest-iphone.sh runs it)")
    s = sub.add_parser("library", help="the one-off run: hold what looks private in a member's photos")
    s.add_argument("--member", type=int, required=True, help="members.id (the owner: the first sysadmin)")
    s.add_argument("--dry-run", action="store_true", help="change nothing; print counts, scores near the threshold, ids")
    s.add_argument("--json", action="store_true", help="also print the report as JSON")
    a = ap.parse_args()
    from core import schema
    con = sqlite3.connect(a.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    schema.ensure(con, "catalog", "embeddings", "video", "members")
    try:
        if a.cmd == "check":
            check(con, a.model)
        else:
            got = library(con, a.member, a.model, a.dry_run)
            if a.json:
                print(json.dumps(got, indent=1))
    except (MissingVectors, PrivateError) as e:
        sys.exit(f"hold: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
