"""The library as tools for an agent: an MCP endpoint (/mcp) on the running app.

An agent finds photos with the same models the app searches with, and proposes what to do with them
as a change set (changes.py). It cannot apply one: that is done by a person, on the review page,
after looking. And it never sees a photo: every tool answers with ids, scores, dates, places and
names, so nothing of what a photo shows leaves this computer, whichever model the agent is.

The transport is MCP's streamable HTTP in its simplest form: each POST carries one JSON-RPC message
(or a batch) and is answered with JSON; there is no stream and no session. `embed.py serve` mounts it
and gives it `app` (an App: the Protocol below; PhotoApp passes an AppFacade), the handful of functions
the tools are made of:

  app.find(args)      -> (ids best first, their scores or None when ranked by date)
  app.row(id)         -> what is known of one photo, or None
  app.faces(ids)      -> {id: (faces found, faces named)} where faces have been looked for
  app.overview()      -> the library in numbers
  app.changes         -> the change sets (changes.Changes)
  app.tasks           -> requests handed to the agent that runs on this computer (tasks.Tasks)

That agent uses this same endpoint. It is told apart by a header (X-Estate-Task, honoured only from
this computer): the change sets it makes carry its task's name, and it is not offered the task tools.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:     # for the types only: the tools reach these through `app`
    from changes import Changes
    from tasks import Tasks

Found = tuple[list[str], list[float] | None]   # ids best first, and their scores (None: ranked by date)


class App(Protocol):
    """What the tools are made of (the module docstring lists it). PhotoApp gives an AppFacade: its Browse's
    find, row, faces and overview, and its change sets and tasks. A test can give any object with these."""
    @property
    def find(self) -> Callable[[dict], Found]: ...
    @property
    def row(self) -> Callable[[str], dict[str, Any] | None]: ...
    @property
    def faces(self) -> Callable[[list[str]], dict[str, tuple[int, int]]]: ...
    @property
    def overview(self) -> Callable[[], dict[str, Any]]: ...
    @property
    def changes(self) -> Changes: ...
    @property
    def tasks(self) -> Tasks: ...


@dataclass(frozen=True)
class AppFacade:
    """The App that `embed.py serve` (PhotoApp) gives the server."""
    find: Callable[[dict], Found]
    row: Callable[[str], dict[str, Any] | None]
    faces: Callable[[list[str]], dict[str, tuple[int, int]]]
    overview: Callable[[], dict[str, Any]]
    changes: Changes
    tasks: Tasks


VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = """Photo Estate: one family's photo library, with tools to find photos and to propose changes to it.

Finding. search_photos ranks photos against a description with an image-text model (SigLIP 2) and can
narrow by person, place, album, camera and date. You never see a photo: you get ids, scores, dates,
places and the names of people in it. A score is a cosine between the description and the photo: a
photo of the thing described scores about 0.085-0.16, an unrelated one under about 0.07, and in a big
library scores fall smoothly with no gap, so read the `summary` each search returns (how many match,
the score at several ranks, the years they come from, how many have a named person) before choosing a
`min_score`. Describe what is in the picture ("young children in a forest"), not who or why; several
plain descriptions find more than one clever one. similar_photos finds photos that look like one you
have.

Albums. An album is a named set of photos that the person sees in the app's Albums tab. Making one changes
nothing else about the library, so you make and fill albums directly: create_album, then amend_album to
add photos by id or by search, each call optionally under a `section` (a heading in the album: a year, a
person, a place) and with a `reason`. For "the best five of X for every year", search each year with
order "best" and distinct true, and add the top five under that year's section. With a `search`,
create_album makes a live album instead: whatever that search finds, now and later. Give the person the
album's `view` link when it is ready.

Changing. Nothing you do changes what the library shows. A change set is an album put up for hiding (propose_hiding does that
to one you have made). To hide photos, create a change set, add
photos to it (by id, or everything a search finds), and give the person its review link: they look it
over, keep the photos that should not be hidden, and apply it themselves. Each photo you add should
carry a short reason a person can read. If a set has been reviewed, get_change_set tells you which
photos they kept: those are your mistakes, and the best guide to doing better.

Handing it over. For a request that covers many photos or needs judgement ("hide the photos from the
school"), submit_task gives it, in the person's own words, to an agent that runs on the computer the
library is on and works through it with these same tools. It takes minutes and keeps going if this
conversation ends. Check on it with get_task; when it is done, give the person its report and the
review link of the change set it made."""

FILTERS = {
    "people": {"type": "array", "items": {"type": "string"},
               "description": "Full names as library_overview lists them; only photos with all of them."},
    "named_people": {"type": "string", "enum": ["any", "none", "some"],
                     "description": "'none': only photos in which nobody has been named (strangers, or no people). 'some': only photos with a named person."},
    "place": {"type": "string", "description": "Part of a place name: city, region or country."},
    "album": {"type": "string", "description": "Part of an album name."},
    "camera": {"type": "string", "description": "Part of a camera or lens name."},
    "date_from": {"type": "string", "description": "YYYY or YYYY-MM, inclusive."},
    "date_to": {"type": "string", "description": "YYYY or YYYY-MM, inclusive."},
    "event": {"type": "integer", "description": "An event id (from a photo's event_id)."},
    "trip": {"type": "integer", "description": "A trip id."},
    "favorite": {"type": "boolean", "description": "Only favourites."},
    "screenshots": {"type": "boolean", "description": "false leaves screenshots out."},
    "hidden_only": {"type": "boolean", "description": "Only photos a change set has hidden. Without it, hidden photos are never found."},
    "distinct": {"type": "boolean", "description": "true leaves out near-identical shots of a photo already found: of each such group, only its top photo."},
}
SEARCH = {
    "query": {"type": "string", "description": "What the photo shows, in plain words. Without it, photos come newest first with no score."},
    "min_score": {"type": "number", "description": "Keep photos scoring at least this. Without it, the app's own rule: near the best match and above its floor."},
    "order": {"type": "string", "enum": ["match", "best"],
              "description": "'best': the best photos first, by the library's own quality score (how good a photograph it is, not how well it matches); each photo's `score` is then that quality. Default 'match': by the query, or newest first without one."},
    **FILTERS,
}
PAGE = {
    "limit": {"type": "integer", "description": "How many photos to list (default 20, at most 200). The summary always covers every match."},
    "offset": {"type": "integer", "description": "Skip this many, to see further down the ranking."},
}


def schema(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


class Tools:
    def __init__(self, app: App):
        self.app = app
        find = {k: v for k, v in SEARCH.items()}
        self.tools = [   # (name, changes something?, description, input schema, function)
            ("library_overview", False,
             "The library in numbers: how many photos, over which years, the named people, and the most common places, albums and cameras. Start here.",
             schema({}), self.overview),
            ("search_photos", False,
             "Find photos by what they show and/or by person, place, album, camera and date. Returns a summary of every match and a page of them, best first.",
             schema({**SEARCH, **PAGE}), self.search),
            ("similar_photos", False,
             "Photos that look like a given photo, most alike first, with the same filters as search_photos. Scores here are photo-to-photo: near-identical shots score above 0.97, the same scene about 0.8-0.9.",
             schema({"id": {"type": "string", "description": "The photo to compare with."}, "min_score": SEARCH["min_score"],
                     **FILTERS, **PAGE}, ["id"]), self.similar),
            ("get_photos", False,
             "Everything known about particular photos: date, place, people named in it, how many faces were found and how many of those are named, albums, event, camera, where the file came from, and whether it is hidden.",
             schema({"ids": {"type": "array", "items": {"type": "string"}, "description": "Up to 200 photo ids."}}, ["ids"]), self.photos),
            ("list_change_sets", False,
             "Every change set, newest first, with how many photos it has, how many it hides now, and how many the person kept.",
             schema({}), self.sets),
            ("get_change_set", False,
             "One change set: its counts, where to review it, and the photos the person marked to keep (your mistakes). `sample` adds that many of its other photos.",
             schema({"change_set": {"type": "integer"}, "sample": {"type": "integer", "description": "How many of the photos not kept to list (default 0, at most 200)."}},
                    ["change_set"]), self.get_set),
            ("create_change_set", True,
             "Start a change set: a proposal to hide photos. It is empty until amend_change_set adds photos, and changes nothing until a person applies it on its review page.",
             schema({"title": {"type": "string", "description": "A few words a person will recognise it by."},
                     "prompt": {"type": "string", "description": "What was asked for, and how you chose the photos, in a sentence or two. Shown on the review page."}},
                    ["title"]), self.create_set),
            ("amend_change_set", True,
             "Add photos to a change set or take them out, by id or by search (everything the search finds, best first, up to `max`). Photos already in the set keep the person's marks. Taking out a photo the set already hides shows it again.",
             schema({"change_set": {"type": "integer"},
                     "add_ids": {"type": "array", "items": {"type": "string"}},
                     "add_search": {**schema({**find, "like": {"type": "string", "description": "A photo id: rank by likeness to it instead of by `query`."},
                                              "max": {"type": "integer", "description": "At most this many (default and limit 20000)."}}),
                                    "description": "Add every photo this search finds."},
                     "remove_ids": {"type": "array", "items": {"type": "string"}},
                     "remove_search": {**schema({**find, "like": {"type": "string"}, "max": {"type": "integer"}}),
                                       "description": "Take out every photo of the set that this search finds."},
                     "reason": {"type": "string", "description": "Why these photos, for the person reviewing; set on the photos added by this call."}},
                    ["change_set"]), self.amend_set),
            ("list_albums", False,
             "Every album, newest first: its title, how many photos, whether it is a live search, and whether it is up for hiding.",
             schema({}), self.albums),
            ("get_album", False,
             "One album: its sections and a page of its photos in order, each with its section and reason.",
             schema({"album": {"type": "integer"}, **PAGE}, ["album"]), self.get_album),
            ("create_album", True,
             "Make an album: a named set of photos the person sees in the Albums tab. Empty until amend_album adds photos. With `search`, a live album instead: whatever that search finds, now and later.",
             schema({"title": {"type": "string", "description": "A few words a person will recognise it by."},
                     "prompt": {"type": "string", "description": "What was asked for, and how you chose the photos, in a sentence or two. Shown under the title."},
                     "search": {**schema(find), "description": "Makes it a live album of what this search finds."}},
                    ["title"]), self.create_album),
            ("amend_album", True,
             "Add photos to an album or take them out, by id or by search (the first `max` it finds, in its order). Photos added by one call go under `section`, if given, in the order given.",
             schema({"album": {"type": "integer"},
                     "add_ids": {"type": "array", "items": {"type": "string"}},
                     "add_search": {**schema({**find, "like": {"type": "string", "description": "A photo id: rank by likeness to it instead of by `query`."},
                                              "max": {"type": "integer", "description": "At most this many (default and limit 20000)."}}),
                                    "description": "Add what this search finds."},
                     "remove_ids": {"type": "array", "items": {"type": "string"}},
                     "remove_search": {**schema({**find, "like": {"type": "string"}, "max": {"type": "integer"}}),
                                       "description": "Take out every photo of the album that this search finds."},
                     "section": {"type": "string", "description": "A heading within the album for the photos this call adds, e.g. a year."},
                     "reason": {"type": "string", "description": "Why these photos, in a few words a person can read."},
                     "title": {"type": "string", "description": "Rename the album."},
                     "prompt": {"type": "string"}},
                    ["album"]), self.amend_album),
            ("propose_hiding", True,
             "Put an album up for hiding: it becomes a change set the person reviews and applies. Nothing is hidden until they do.",
             schema({"album": {"type": "integer"}}, ["album"]), self.hide_album),
            ("submit_task", True,
             "Hand a request to the agent that runs on the library's own computer. It works out which photos are meant and proposes a change set for the person to review. Returns at once with a task number; the work takes minutes. One task runs at a time.",
             schema({"request": {"type": "string", "description": "What the person wants, in their own words, with anything they said about which photos and which not."}},
                    ["request"]), self.submit_task),
            ("get_task", False,
             "How a task is going: its status (queued, running, done, failed, cancelled), what the agent has done since event `after`, and when done its report and the change set to review.",
             schema({"task": {"type": "integer"}, "after": {"type": "integer", "description": "Only events after this event id (from last_event)."}},
                    ["task"]), self.get_task),
            ("cancel_task", True, "Stop a task that is queued or running. A change set it already made stays, unapplied.",
             schema({"task": {"type": "integer"}}, ["task"]), self.cancel_task),
        ]
        self.by_name = {t[0]: t for t in self.tools}

    TASK_TOOLS = ("submit_task", "get_task", "cancel_task")   # not for the agent a task runs

    def listing(self, in_task: bool = False) -> list[dict]:
        return [{"name": n, "description": d, "inputSchema": s,
                 "annotations": {"readOnlyHint": not w, "destructiveHint": False, "openWorldHint": False}}
                for n, w, d, s, _ in self.tools if not (in_task and n in self.TASK_TOOLS)]

    # -- tasks -----------------------------------------------------------------------------------

    def task_brief(self, t: dict) -> dict:
        out = {"task": t["id"], "status": t["status"], "request": t["prompt"], "report": t["result"], "error": t["error"],
               "change_sets": t["change_sets"], "last_event": (t["last"] or {}).get("id")}
        if "events" in t:
            out["events"] = [{"kind": e["kind"], "text": e["text"]} for e in t["events"][-40:]]
        return {k: v for k, v in out.items() if v is not None}

    def submit_task(self, args, who=""):
        got, code = self.app.tasks.submit(args.get("request"), who)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.task_brief(got), "next": "get_task shows how it is going; it usually takes a few minutes"}

    def get_task(self, args):
        got = self.app.tasks.get(int(args["task"]), max(0, int(args.get("after") or 0)))
        if not got:
            raise ValueError("no such task")
        return self.task_brief(got)

    def cancel_task(self, args, who=""):
        got, code = self.app.tasks.cancel(int(args["task"]))
        if code >= 400:
            raise ValueError(got["error"])
        return self.task_brief(got)

    # -- finding ---------------------------------------------------------------------------------

    def rows(self, ids, scores=None) -> list[dict]:
        faces = self.app.faces(ids)
        out = []
        for i, sha in enumerate(ids):
            row = self.app.row(sha)
            if row is None:
                out.append({"id": sha, "error": "no such photo"})
                continue
            if scores is not None:
                row["score"] = scores[i]
            if sha in faces:
                row["faces"], row["named_faces"] = faces[sha]
            out.append(row)
        return out

    def summary(self, ids, scores) -> dict:
        years, named = Counter(), 0
        for sha in ids:
            row = self.app.row(sha) or {}
            years[(row.get("date") or "")[:4] or "undated"] += 1
            named += bool(row.get("people"))
        out = {"matching": len(ids), "with_named_people": named, "by_year": dict(sorted(years.items()))}
        if scores:
            out["score_at_rank"] = {str(r): scores[r - 1] for r in (1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000) if r <= len(scores)}
            out["score_at_rank"][str(len(scores))] = scores[-1]
        return out

    def page(self, ids, scores, args) -> dict:
        limit = max(1, min(int(args.get("limit") or 20), 200))
        at = max(0, int(args.get("offset") or 0))
        return {"summary": self.summary(ids, scores),
                "photos": self.rows(ids[at:at + limit], scores[at:at + limit] if scores else None),
                "next_offset": at + limit if at + limit < len(ids) else None}

    def overview(self, args):
        return self.app.overview()

    def search(self, args):
        ids, scores = self.app.find(args)
        return self.page(ids, scores, args)

    def similar(self, args):
        ids, scores = self.app.find({**args, "like": args["id"], "query": None})
        return self.page(ids, scores, args)

    def photos(self, args):
        return {"photos": self.rows([str(s) for s in args["ids"]][:200])}

    # -- albums ----------------------------------------------------------------------------------

    def album_brief(self, s: dict) -> dict:
        out = {"album": s["id"], "title": s["title"], "photos": s["n"], "live": s["kind"] == "live" or None,
               "search": s["query"], "view": f"/?album={s['id']}"}
        if s["op"] == "hide":
            out.update(up_for_hiding=True, state=s["state"], hidden_now=s["applied"], review=f"/changes/{s['id']}")
        return {k: v for k, v in out.items() if v is not None}

    def albums(self, args):
        return {"albums": [self.album_brief(s) for s in self.app.changes.listing(albums=True)["sets"]]}

    def get_album(self, args):
        limit, at = max(1, min(int(args.get("limit") or 20), 200)), max(0, int(args.get("offset") or 0))
        got = self.app.changes.photos(int(args["album"]), at, limit)
        if got is None:
            raise ValueError("no such album")
        extra = {r["sha"]: {k: r[k] for k in ("section", "reason") if r.get(k)} for r in got["results"]}
        return {**self.album_brief(got["album"]), "prompt": got["album"]["prompt"], "sections": got["sections"],
                "photos": [{**row, **extra.get(row["id"], {})} for row in self.rows([r["sha"] for r in got["results"]])],
                "next_offset": got["next"]}

    def create_album(self, args, who=""):
        got, code = self.app.changes.create({"title": args.get("title"), "prompt": args.get("prompt"), "op": "view",
                                             **({"query": args["search"]} if args.get("search") is not None else {})}, who)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.album_brief(got), "next": "amend_album adds photos; then give the person the view link"
                if got["kind"] != "live" else "it holds what the search finds; give the person the view link"}

    def amend_album(self, args, who=""):
        sid = int(args["album"])
        if args.get("title") or "prompt" in args:
            got, code = self.app.changes.write(sid, "rename", {k: args[k] for k in ("title", "prompt") if k in args})
            if code >= 400:
                raise ValueError(got["error"].replace("change set", "album"))
            if not any(args.get(k) for k in ("add_ids", "add_search", "remove_ids", "remove_search")):
                return self.album_brief(got)
        out = self.amend_set({**args, "change_set": sid}, who)
        return {**{k: v for k, v in out.items() if k not in ("change_set", "state", "hidden_now", "kept_by_person", "review")},
                "album": sid, "view": f"/?album={sid}"}

    def hide_album(self, args, who=""):
        got, code = self.app.changes.write(int(args["album"]), "hide", {})
        if code >= 400:
            raise ValueError(got["error"].replace("change set", "album"))
        return {**self.album_brief(got), "next": "give the person the review link: they look it over and apply it"}

    # -- change sets -----------------------------------------------------------------------------

    def brief(self, s: dict) -> dict:
        return {"change_set": s["id"], "title": s["title"], "state": s["state"], "photos": s["n"],
                "hidden_now": s["applied"], "kept_by_person": s["reject"], "review": f"/changes/{s['id']}"}

    def sets(self, args):
        return {"change_sets": [self.brief(s) for s in self.app.changes.listing()["sets"]]}   # (albums up for hiding)

    def get_set(self, args):
        d = self.app.changes.detail(int(args["change_set"]))
        if d is None:
            raise ValueError("no such change set")
        kept = [it["sha"] for it in d["items"] if it["decision"] == "reject"]
        rest = [it["sha"] for it in d["items"] if it["decision"] != "reject"]
        reasons = {it["sha"]: it["reason"] for it in d["items"] if it["reason"]}
        def listed(ids):
            return [{**row, **({"reason": reasons[row["id"]]} if row["id"] in reasons else {})} for row in self.rows(ids)]
        out = {**self.brief(d), "prompt": d["prompt"], "kept": listed(kept[:200]), "kept_not_listed": max(0, len(kept) - 200)}
        n = max(0, min(int(args.get("sample") or 0), 200))
        if n:
            out["sample"] = listed(rest[::max(1, len(rest) // n)][:n])
        return out

    def create_set(self, args, who=""):
        got, code = self.app.changes.create({"title": args.get("title"), "prompt": args.get("prompt"), "op": "hide"}, who)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.brief(got), "next": "amend_change_set adds photos; then give the person the review link"}

    def found(self, spec: dict, reason):
        ids, scores = self.app.find(spec)
        n = max(0, min(int(spec.get("max") or 20000), 20000))
        why = reason or (f"Matches \"{spec['query']}\"" if spec.get("query") else "Looks like a photo of the set" if spec.get("like") else None)
        return [{"sha": s, "score": scores[i] if scores else None, "reason": why} for i, s in enumerate(ids[:n])], len(ids)

    def amend_set(self, args, who=""):
        sid, reason, section = int(args["change_set"]), args.get("reason"), args.get("section")
        items = [{"sha": str(s), "reason": reason} for s in args.get("add_ids") or []]
        out = {}
        if args.get("add_search"):
            got, out["add_search_matched"] = self.found(args["add_search"], reason)
            items += got
        remove = [str(s) for s in args.get("remove_ids") or []]
        if args.get("remove_search"):
            got, out["remove_search_matched"] = self.found(args["remove_search"], None)
            remove += [it["sha"] for it in got]
        for it in items:
            it["section"] = section
        got, code = self.app.changes.write(sid, "items", {"items": items, "remove": remove})
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.brief(got), **out, "added": got["added"], "already_in_set": len(items) - got["added"] - got["unknown"],
                "unknown_ids": got["unknown"], "removed": got["removed"]}


class Server:
    """JSON-RPC over HTTP POST, as much of MCP as tools need."""

    def __init__(self, app: App, name="photo-estate", version="1"):
        self.tools, self.info = Tools(app), {"name": name, "title": "Photo Estate", "version": version}

    def call(self, msg, can_write: bool, who: str, in_task: bool = False):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32600, "message": "not a JSON-RPC 2.0 request"}}
        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        if "id" not in msg:          # a notification (notifications/initialized, cancelled): nothing to answer
            return None
        ok = lambda result: {"jsonrpc": "2.0", "id": rid, "result": result}
        err = lambda code, text: {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}
        if method == "initialize":
            want = params.get("protocolVersion")
            return ok({"protocolVersion": want if want in VERSIONS else VERSIONS[0], "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": self.info, "instructions": INSTRUCTIONS})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tools.listing(in_task)})
        if method == "tools/call":
            tool = self.tools.by_name.get(params.get("name"))
            if not tool or in_task and tool[0] in Tools.TASK_TOOLS:
                return err(-32602, f"unknown tool: {params.get('name')}")
            name, writes, _, _, fn = tool
            args = params.get("arguments") or {}
            try:
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                if writes and not can_write:
                    raise PermissionError("this caller may look but not propose changes")
                out = fn(args, who) if writes else fn(args)
                return ok({"content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"), default=str)}],
                           "structuredContent": out, "isError": False})
            except (ValueError, KeyError, TypeError, PermissionError) as e:   # the agent can read it and try again
                text = f"missing argument: {e}" if isinstance(e, KeyError) else str(e)
                return ok({"content": [{"type": "text", "text": text}], "isError": True})
        return err(-32601, f"method not found: {method}")

    def handle(self, method: str, headers, body, can_write: bool, who: str = "", local: bool = False):
        """One HTTP request to /mcp -> (status, body bytes, content type, Cache-Control)."""
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":"), default=str).encode(), "application/json", "no-store")
        # A page on another site must not be able to drive this from someone's browser.
        origin, host = headers.get("Origin"), headers.get("Host") or ""
        if origin and origin.split("://", 1)[-1] != host:
            return js({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "cross-origin requests are refused"}}, 403)
        if method != "POST":         # no server-to-client stream, no session to end
            return 405, b"", "text/plain", "no-store"
        if body is None:
            return js({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "the body is not JSON"}}, 400)
        task = headers.get("X-Estate-Task") or ""
        in_task = local and task.isdigit()
        if in_task:
            who = f"task:{task}"
        if isinstance(body, list):
            got = [r for r in (self.call(m, can_write, who, in_task) for m in body) if r is not None]
            return js(got) if got else (202, b"", "application/json", "no-store")
        got = self.call(body, can_write, who, in_task)
        return js(got) if got is not None else (202, b"", "application/json", "no-store")
