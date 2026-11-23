"""
browse.py — finding photos in the photo app (`embed.py serve`): the search language, the ranked and
browsed lists, and the same searches as an agent's tools (estate_mcp.py) ask them.

  GET /api/search    ?q= (words, names, places, key:value filters) and the facet params -> a page of results
                     (`kind=video|photo` or `is:video` in q: only those; a video found by words carries `at`,
                     the seconds into it where they match best)
  GET /api/similar   ?sha= -> photos that look like it
  GET /api/facets    counts per facet value under the current filters
  GET /api/periods   the Library's Years and Months: a card each, with its cover
  GET /api/layout    a byte per photo: the shapes of the Library's photos, in /api/search's order
  GET /api/log       ?ev=open|share&sha=&pos= : a photo opened or shared from a result list (query log)

  Browse.handle(method, path, qs, body, headers, member) -> (status, body, content type, Cache-Control[, headers]) or None
  Browse.find / row / faces / overview            what estate_mcp.py's tools are made of (each for a member)

Every answer is worked out from the catalog being served (catalog.Live), read once per request, as the member
the request is for sees it: Live.visible_for(member), the one way to ask (multiplayer: one member's filters
hide nothing for another). A `limit` or `cursor` that is not a whole number raises http_util.HTTPError (400),
which PhotoApp answers.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from catalog import COMMON_WORDS, Catalog, Live, View, chunks, is_screenshot
from http_util import int_param

filter_re = re.compile(r'\b(place|camera|album|person|people|year|date|event|trip|is):("([^"]*)"|(\S+))', re.I)
sha_re = re.compile(r"^[0-9a-f]{64}$")

# Names and places in a typed search ("ben and matthew on a beach", "ben san diego") are filters, not
# words for the image model: it doesn't know who anyone is, and it can only guess a place from how it
# looks (a sunny Florida street passes for San Diego). A person's full name always counts, and a
# first name when one person has it. A place counts if the library has photos there: a city, region
# or country. A name or place that's also an everyday word ("sunny", "celebration", "golden") counts
# only when capitalized ("Sunny's" too). People: photos with all of them; places: any of them.
joiners = {"\0": {"and", "&", "+", "with", ","}, "\1": {"in", "at", "from", "near", "around", "to", "and", "or", ","}}
filler = {"photo", "photos", "picture", "pictures", "pic", "pics", "of", "the", "all", "show", "me", "find",
          "trip", "trips", "visit"}

# Query-string keys the query log leaves out: paging and view options, not what was asked
unlogged = {"cursor", "limit", "dedupe", "shots", "ev", "sha", "pos"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def js(o, code: int = 200):
    return code, json.dumps(o, separators=(",", ":")).encode(), "application/json", "no-store"


def read_words(c: Catalog, text: str):
    """'ben and matthew in san diego' -> (['Ben Lam', 'Matthew Lam'], ['San Diego'], '')."""
    names, places, parts, at = [], [], [], 0
    for m in c.word_re.finditer(text):
        word = m.group("who") or m.group("where")
        key, capital = word.lower(), word[0].isupper() or m.group(3)
        if m.group("who"):
            if key in COMMON_WORDS and not capital:
                continue
            mark, got, val = "\0", names, c.who[key]
        else:
            if key in c.common_place and not capital:
                continue
            mark, got, val = "\1", places, c.where[key]
        parts += [text[at:m.start()], mark]
        at = m.end()
        if val not in got:
            got.append(val)
    if not names and not places:
        return [], [], text
    toks = re.findall(r"[\0\1]|,|[^\s,\0\1]+", " ".join(parts + [text[at:]]))
    keep = [t for i, t in enumerate(toks) if t not in "\0\1" and not any(
        t.lower() in joiners[mk] for mk in toks[max(0, i - 1):i] + toks[i + 1:i + 2] if mk in joiners)]
    rest = " ".join(keep).strip(" ,")
    everything = filler | joiners["\0"] | joiners["\1"]
    return names, places, "" if all(t.lower() in everything for t in rest.split()) else rest


def parse(view: View, q: str):
    """Split 'beach place:oregon year:2010' into the free text and a mask for the typed filters."""
    c = view.cat
    mask, applied, scope = np.ones(c.N, bool), [], None
    for m in filter_re.finditer(q):
        key, val = m.group(1).lower(), (m.group(3) if m.group(3) is not None else m.group(4))
        v = val.lower().strip()
        if key in ("place", "camera", "album", "person", "people"):
            arr = c.cols["person" if key == "people" else key]
            mask &= np.fromiter((v in x for x in arr), bool, len(arr))
        elif key == "year":
            lo, _, hi = v.partition("-")
            if lo.isdigit():
                mask &= (c.year >= int(lo)) & (c.year <= int(hi if hi.isdigit() else lo))
        elif key == "date":
            mask &= np.fromiter((w.startswith(v) for w in c.when), bool, len(c.when))
        elif key in ("event", "trip") and v.lstrip("-").isdigit():
            mask &= (c.event_of if key == "event" else c.trip_of) == int(v)
            scope = c.scope_of(key, int(v)) or scope
            val = (scope or {}).get("title") or val
        elif key == "is" and v in ("fav", "favorite", "favourite"):
            mask &= view.fav
        elif key == "is" and v in ("video", "videos", "photo", "photos"):
            mask &= c.video if v.startswith("video") else ~c.video
        else:
            continue
        applied.append(f"{key}:{val}")
    text = re.sub(r"\s+", " ", filter_re.sub(" ", q)).strip()
    return text, mask, applied, scope


def view_masks(v: View, qs: dict, masks: dict, text: str, similar: bool) -> bool:
    """The view options, for browsing and filtering (no words to rank by, not "More like this"), as the
    iPhone app has them: screenshots out (`shots=0`), and stacks shown as their top photo (`dedupe=1`) by
    the library-wide flag the iPhone uses too, so a count is the count of what shows, and a cursor is a
    position in that list. True when browsing: take() then skips nothing page by page.
    Stacks collapse in every list, a ranked one too, except the list that is one stack's photos."""
    if (qs.get("dedupe") or ["1"])[0] == "1" and "stack" not in masks:
        masks["dup"] = ~v.dup
    if text or similar:
        return False
    if (qs.get("shots") or ["1"])[0] == "0":
        masks["shots"] = ~v.cat.shot
    return True


class Browse:
    def __init__(self, live: Live, encoder, db: str, model: str, query_log: Path,
                 match_floor: float, match_window: float, best_window: float):
        self.live, self.encoder, self.db, self.model = live, encoder, db, model
        self.match_floor, self.match_window, self.best_window = match_floor, match_window, best_window
        self.lru, self.lru_lock = OrderedDict(), threading.Lock()
        # Every search, page of results, and photo opened or shared from one, one JSON object per line.
        # This is the raw material for choosing evaluation queries from real use; nothing reads it back.
        self.qlog, self.qlog_lock = query_log, threading.Lock()

    # -- caching --------------------------------------------------------------------------------

    def remember(self, key, make, member: int | None = None):
        """Small cache so paging through one search doesn't redo the text encoding and the ranking. An entry
        is the catalog generation's it was made in (Live.generation): one made while the catalog was
        replaced, what anyone hides changed, or a favourite set is not kept, and none is found again in a
        later generation. One member's entry is never another's: `member` is part of the key."""
        gen = self.live.generation
        key = (gen, member, key)
        with self.lru_lock:
            if key in self.lru:
                self.lru.move_to_end(key)
                return self.lru[key]
        val = make()
        with self.lru_lock:
            if self.live.generation == gen:
                self.lru[key] = val
                while len(self.lru) > 24:
                    self.lru.popitem(last=False)
        return val

    def forget(self) -> None:
        """Drop every cached mask and ranking: the catalog, or a favourite, has changed. (They would not be
        used again anyway, being an earlier generation's; this only frees them.)"""
        with self.lru_lock:
            self.lru.clear()

    def text_vec(self, text: str):
        return self.remember(("text", text), lambda: self.encoder.text(text))

    # -- reading a query ------------------------------------------------------------------------

    def place_mask(self, c: Catalog, place: str):
        def make():
            rx = re.compile(r"(?<!\w)" + re.escape(place.lower()) + r"(?!\w)")
            return np.fromiter((bool(rx.search(x)) for x in c.cols["place"]), bool, c.N)
        return self.remember(("place", place), make)

    def select(self, v: View, qs: dict):
        """Query string -> one mask per kind of filter, the free text, and the event/trip in scope."""
        c, masks = v.cat, {}
        for k, f in c.single.items():
            if qs.get(k):
                masks[k] = np.isin(f["codes"], [f["ix"][x] for x in qs[k] if x in f["ix"]])
        for k, f in c.multi.items():
            if qs.get(k):
                each = []
                for x in qs[k]:
                    m = np.zeros(c.N, bool)
                    if x in f["ix"]:
                        m[f["rows"][f["codes"] == f["ix"][x]]] = True
                    each.append(m)
                masks[k] = (np.logical_and if k == "person" else np.logical_or).reduce(each)
        lo, hi = c.month_index((qs.get("from") or [""])[0]), c.month_index((qs.get("to") or [""])[0], end=True)
        if lo is not None or hi is not None:
            masks["date"] = (c.ym >= (lo if lo is not None else 0)) & (c.ym <= (hi if hi is not None else c.nmonths))
        if (qs.get("fav") or ["0"])[0] == "1":
            masks["fav"] = v.fav
        if (kind := (qs.get("kind") or [""])[0]) in ("video", "photo"):
            masks["kind"] = c.video if kind == "video" else ~c.video
        if (qs.get("hidden") or ["0"])[0] == "1":   # what the member's filters hide (of what they may be shown)
            masks["hidden"] = ~v.visible
        one = c.index.get((qs.get("stack") or [""])[0])
        if one is not None:   # the photos of one stack, named by any of them
            masks["stack"] = v.stack_of == v.stack_of[one] if v.stack_of[one] >= 0 else np.arange(c.N) == one
        scope = None
        ev = (qs.get("event") or [""])[0]
        if ev.lstrip("-").isdigit():
            masks["event"] = c.event_of == int(ev)
            scope = c.scope_of("event", int(ev))
        elif len(qs.get("trip") or []) == 1 and qs["trip"][0].lstrip("-").isdigit():
            scope = c.scope_of("trip", int(qs["trip"][0]))
        text, typed, applied, typed_scope = parse(v, (qs.get("q") or [""])[0].strip())
        if applied:
            masks["typed"] = typed
        names, places, rest = read_words(c, text)
        if (names or places) and (qs.get("names") or ["1"])[0] != "0":
            if names:
                masks["names"] = np.logical_and.reduce([c.person_mask(n) for n in names])
            if places:
                masks["places"] = np.logical_or.reduce([self.place_mask(c, p) for p in places])
            text = rest
        return masks, text, applied, scope or typed_scope, names, places

    # -- the query log --------------------------------------------------------------------------

    def record(self, headers, ev: str, qs: dict, **fields):
        line = {"ts": now(), "ev": ev,
                "params": {k: v if len(v) > 1 else v[0] for k, v in qs.items() if k not in unlogged},
                **{k: v for k, v in fields.items() if v is not None},
                "who": headers.get("Tailscale-User-Login"), "ua": headers.get("User-Agent")}
        try:
            with self.qlog_lock, self.qlog.open("a") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:  # never fail a search because the log can't be written
            print(f"query log: {e}", flush=True)

    # -- http -----------------------------------------------------------------------------------

    def handle(self, method: str, path: str, qs: dict, body, headers, member: int | None = None):
        """One request for `member` (members.id) -> (status, body bytes, content type, Cache-Control[, headers]),
        or None if it isn't ours."""
        if method != "GET":
            return None
        v = self.live.visible_for(member)
        c = v.cat
        one = lambda k, d="": (qs.get(k) or [d])[0]
        if path in ("/api/search", "/api/similar"):
            return self.search(v, path, qs, headers)
        if path == "/api/log":  # the page reports photos opened or shared from a result list
            if one("ev") in ("open", "share") and sha_re.match(one("sha")):
                pos = one("pos")
                self.record(headers, one("ev"), qs, sha=one("sha"), pos=int(pos) if pos.isdigit() else None)
            return 204, b"", "text/plain", "no-store"
        if path == "/api/facets":
            masks, text = self.select(v, qs)[:2]
            view_masks(v, qs, masks, text, bool(one("sha")))
            return js(v.facet_counts(masks))
        if path == "/api/periods":   # the Library's Years and Months
            masks, text = self.select(v, qs)[:2]
            view_masks(v, qs, masks, "", False)
            mask, sort = v.combine(masks), one("sort") or "new"
            key = ("periods", sort, tuple(sorted((k, tuple(x)) for k, x in qs.items())))

            def make():
                by_time = c.oldest if sort == "old" else c.newest
                return {**v.periods(by_time[mask[by_time]]), "matching": int(mask.sum()), "total": v.info["total"]}
            return js(self.remember(key, make, member))
        if path == "/api/layout":    # the shapes of the Library's photos, in /api/search's order
            masks, text = self.select(v, qs)[:2]
            view_masks(v, qs, masks, "", False)
            sort = one("sort") or "new"
            if sort in ("best", "best-month") and c.best is None:
                sort = "new"
            key = ("layout", sort, tuple(sorted((k, tuple(x)) for k, x in qs.items())))
            out = self.remember(key, lambda: c.shapes(c.browse_order(v.combine(masks), sort)), member)
            return 200, out, "application/octet-stream", "no-cache", {"ETag": f'"{hashlib.md5(out).hexdigest()}"'}
        return None

    def search(self, v: View, path: str, qs: dict, headers):
        """/api/search and /api/similar: one page of a ranked or browsed list."""
        c = v.cat
        one = lambda k, d="": (qs.get(k) or [d])[0]
        t = time.time()
        limit = int_param(qs, "limit", 120, min=1, max=500)        # http_util.HTTPError 400 when not a number
        start = int_param(qs, "cursor", 0, min=0)
        masks, text, applied, scope, names, places = self.select(v, qs)
        deduped = view_masks(v, qs, masks, text, path == "/api/similar")
        mask = v.combine(masks)
        sha = one("sha")
        if path == "/api/similar" and sha not in c.index:
            return js({"error": "unknown photo"}, 404)
        by_default = "best" if "stack" in masks else "old" if scope else "new"   # a stack: its top photo first
        sort = one("sort") or by_default
        if sort in ("best", "best-month") and c.best is None:   # a catalog with no scorers: as if nothing was asked
            sort = "old" if scope or "stack" in masks else "new"
        if path == "/api/similar":
            sort = "match"            # by likeness: there is no other order to choose
        elif text and sort != "best":
            sort = "match"            # words to rank by: the closest match first, unless the best was asked for
        key = (path, sha, text, sort, tuple(sorted((k, tuple(v)) for k, v in qs.items()
                                                   if k not in ("cursor", "limit"))))

        def ranked():
            if path == "/api/similar":
                vec = c.M[c.index[sha]]
            elif text:
                vec = self.text_vec(text)
            else:  # nothing to rank by: what the filters select, by time
                return c.browse_order(mask, sort)
            sims = np.where(mask, c.sims(vec), -np.inf)
            if path == "/api/similar":
                sims[c.index[sha]] = -np.inf
            else:  # only what matches the words: near the best match, and above the floor
                sims[sims < max(self.match_floor, sims.max() - self.match_window)] = -np.inf
                if sort == "best":    # the closest matches only (see BEST_WINDOW), the best of them first
                    sims[sims < sims.max() - self.best_window] = -np.inf
                    found = np.flatnonzero(np.isfinite(sims))
                    return found[np.argsort(c.scored()[1][found], kind="stable")]
            return np.argsort(-sims)[: int(np.isfinite(sims).sum())]

        order = self.remember(key, ranked, v.member)
        results, hidden, nxt = v.take(order, start, limit, one("dedupe", "1") == "1" and not deduped)
        if text and path != "/api/similar" and any(r.get("kind") == "video" for r in results):
            vec = self.text_vec(text)     # where in each video the words match best: the viewer starts there
            for r in results:
                if r.get("kind") == "video" and (at := c.moment(str(r["sha"]), vec)) is not None:
                    r["at"] = at
        self.record(headers, "similar" if path == "/api/similar" else "search", qs, sha=one("sha") or None, text=text,
                    filters=applied, people=names, places=places, cursor=start, matching=int(len(order)),
                    ms=round((time.time() - t) * 1000),
                    top=[r["sha"] for r in results[:20]] if start == 0 else None)
        return js({"results": results, "next": nxt, "hidden": hidden, "total": v.info["total"],
                   "matching": int(len(order)), "text": text, "filters": applied, "people": names, "places": places,
                   "scope": scope, "sort": sort, "ms": round((time.time() - t) * 1000)})

    # -- for an agent (estate_mcp.py) -----------------------------------------------------------
    # An agent says exactly what it wants, so nothing is read out of its words (no names or places
    # taken from the description) and no view option applies: it finds near-identical shots and
    # screenshots too, and every match, not a screenful.

    def find(self, args: dict, member: int | None = None) -> tuple[list[str], list[float] | None]:
        """Photos `member` sees that match the filters, best first: ([ids], [scores]), or ([ids], None) newest
        first when there is nothing to rank by. Ranks by `query` (words) or `like` (a photo's id).
        `hidden_only`: the photos the member's filters hide instead (of those they may be shown)."""
        view = self.live.visible_for(member)
        c = view.cat
        N, M, index, shas = c.N, c.M, c.index, c.shas
        mask = view.shared & ~view.visible if args.get("hidden_only") else view.visible.copy()
        for key in ("place", "album", "camera"):
            if args.get(key):
                v = str(args[key]).lower()
                mask &= np.fromiter((v in x for x in c.cols[key]), bool, N)
        for name in args.get("people") or []:
            if name not in c.people_ix:
                raise ValueError(f"nobody in the library is named {name!r}; library_overview lists the names")
            mask &= c.person_mask(name)
        if args.get("named_people") in ("none", "some"):
            has = np.fromiter((bool(x) for x in c.cols["person"]), bool, N)
            mask &= has if args["named_people"] == "some" else ~has
        lo, hi = c.month_index(str(args.get("date_from") or "")), c.month_index(str(args.get("date_to") or ""), end=True)
        if lo is not None or hi is not None:
            mask &= (c.ym >= max(lo or 0, 0)) & (c.ym <= (hi if hi is not None else c.nmonths))
        if args.get("event") is not None:
            mask &= c.event_of == int(args["event"])
        if args.get("trip") is not None:
            mask &= c.trip_of == int(args["trip"])
        if args.get("favorite"):
            mask &= view.fav
        if args.get("screenshots") is False:
            mask &= ~c.shot
        if args.get("kind") in ("video", "photo"):
            mask &= c.video if args["kind"] == "video" else ~c.video
        if args.get("distinct"):         # the top photo of each stack of near-identical shots, not the others
            mask &= ~view.dup
        by_best = args.get("order") == "best"
        if by_best and c.rank is None:
            raise ValueError("this library has no quality scores, so nothing can be ordered best first")
        quality = lambda ids: [round(float(x), 3) for x in c.scored()[2][ids]]
        like, text = args.get("like"), str(args.get("query") or "").strip()
        if like:
            if like not in index:
                raise ValueError("no photo has that id")
            sims = c.sims(M[index[like]])
            mask[index[like]] = False
        elif text:
            sims = c.sims(self.text_vec(text))
        elif by_best:
            best = c.scored()[0]
            ids = best[mask[best]]
            return [shas[i] for i in ids], quality(ids)
        else:
            return [shas[i] for i in c.newest[mask[c.newest]]], None
        sims = np.where(mask, sims, -np.inf)
        if args.get("min_score") is not None:
            sims[sims < float(args["min_score"])] = -np.inf
        elif not like and mask.any():   # as a typed search in the app: near the best match, above the floor
            sims[sims < max(self.match_floor, sims.max() - self.match_window)] = -np.inf
        if by_best:                      # what matches, the best of it first
            found = np.flatnonzero(np.isfinite(sims))
            ids = found[np.argsort(c.scored()[1][found], kind="stable")]
            return [shas[i] for i in ids], quality(ids)
        order = np.argsort(-sims)[: int(np.isfinite(sims).sum())]
        return [shas[i] for i in order], [round(float(sims[i]), 4) for i in order]

    def row(self, sha: str, member: int | None = None) -> dict | None:
        """What an agent is told of one photo, as `member` sees it; None for no such photo, or one they may not
        be shown (shared_for)."""
        v = self.live.visible_for(member)
        c = v.cat
        m = c.meta.get(sha)
        if m is None or not v.shared[c.index[sha]]:
            return None
        i = c.index[sha]
        row = {"id": sha, "date": m.get("local") or m["taken"], "place": m.get("place"), "people": m["people"],
               "albums": m["albums"], "event": m.get("event_title"), "event_id": m.get("event"), "trip": m.get("trip_title"),
               "camera": m.get("camera"), "favorite": bool(v.fav[i]) or None, "screenshot": is_screenshot(m) or None,
               "source_path": m["path"], "hidden": (not v.visible[i]) or None,
               "kind": m.get("kind"), "seconds": m.get("dur"), "live_photo": bool(m.get("live")) or None,
               "quality": round(float(c.score[i]), 3) if c.score is not None else None,
               "shots_alike": int(v.stack_n[i]) if v.stack_n[i] > 1 else None}
        return {k: v for k, v in row.items() if v not in (None, [], "")}

    def faces(self, ids: list[str]) -> dict[str, tuple[int, int]]:
        """{id: (faces found, faces named)} for the photos in which faces were looked for and found."""
        if not self.live.cat.has_faces or not ids:
            return {}
        con = sqlite3.connect(self.db, timeout=30)
        try:
            return {sha: (n, named or 0) for c in chunks(ids) for sha, n, named in con.execute(
                "SELECT sha256, count(*), sum(person_id > 0) FROM faces WHERE sha256 IN (%s) GROUP BY 1" % ",".join("?" * len(c)), c)}
        except sqlite3.OperationalError:
            return {}
        finally:
            con.close()

    def overview(self, member: int | None = None) -> dict:
        v = self.live.visible_for(member)
        c = v.cat
        top = lambda pairs, n=40: [{"name": k, "photos": x} for k, x in pairs[:n]]
        f = v.info["facets"]
        month = lambda i: f"{(c.m0 + i) // 12}-{(c.m0 + i) % 12 + 1:02d}"
        dated = [i for i, n in enumerate(v.info["months"]) if n]
        return {"photos": v.info["total"], "hidden": int((v.shared & ~v.visible).sum()),
                "first_month": month(dated[0]) if dated else None, "last_month": month(dated[-1]) if dated else None,
                "by_year": dict(sorted(Counter(str(y) if y else "undated" for y in c.year[v.visible].tolist()).items())),
                "people": top(f["person"], 200), "countries": top(f["country"]), "cities": top(f["city"]),
                "albums": top(f["album"]), "cameras": top(f["camera"]), "trips": len(v.trip_list),
                "faces_found": c.has_faces,
                "search": {"model": self.model, "match_floor": self.match_floor, "match_window": self.match_window}}
