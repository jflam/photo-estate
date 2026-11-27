"""The library as tools for an agent: an MCP endpoint (/mcp) on the running app.

An agent finds photos with the same models the app searches with, and proposes what to do with them
as a change set (changes.py). It cannot apply one: that is done by a person, on the review page,
after looking. And it never sees a photo: every tool answers with ids, scores, dates, places and
names, so nothing of what a photo shows leaves this computer, whichever model the agent is.

The transport is MCP's streamable HTTP in its simplest form: each POST carries one JSON-RPC message
(or a batch) and is answered with JSON; there is no stream and no session. `embed.py serve` mounts it
and gives it `app` (an App: the Protocol below; PhotoApp passes an AppFacade), the handful of functions
the tools are made of:

  app.find(args, member)   -> (ids best first, their scores or None when ranked by date)
  app.row(id, member)      -> what is known of one photo, or None
  app.faces(ids)           -> {id: (faces found, faces named)} where faces have been looked for
  app.overview(member)     -> the library in numbers
  app.changes         -> the change sets (changes.Changes)
  app.tasks           -> requests handed to the agent that runs on this computer (tasks.Tasks)
  app.ask(...)        -> a question filed (stories.ask)
  app.stories(ids, member) -> {id: [stories]} that member may read (stories.about)
  app.family()        -> the family's members: [{id, name, person}]
  app.journal(member, cursor, limit) -> what that member may know of the journal after cursor (journal.for_member)

That agent uses this same endpoint. A run of it carries a token (X-Estate-Agent: tasks.Tasks.mint), honoured only
from this computer and only while the run lasts: it says *this agent, acting for this member (or no one), for this
task*, and nothing about it can be chosen by the caller. A call with a token that is not a live run's, or from
anywhere but this computer, is refused (403), as is one that still carries X-Estate-Task, the task number a run
carried before phase 7: never answered as this computer's sysadmin. A run is not offered the task tools.
Who may call it is the app's rule for every route (an active member, or this computer: photo_app.py).

A call with a member's login (through `tailscale serve`; with --demo, the demo's header or cookie) is that member's
**own agent** (multiplayer phase 8), on the device it came from: photo_app.py gives `handle` its binding
(members.bind: one per member and device, made pending by its first call). Pending, it may call every tool that only
looks; every tool that writes answers that the member must allow it first, in their inbox. Approved, it writes as
the member, and every journal entry it makes carries the binding (journal.by_agent). Revoked, every call is refused
(403). It is never offered submit_task, get_task or cancel_task (its work runs in the member's own harness, on their
own account, never on the owner's subscription; called anyway they are refused), and only it is offered
journal_since: what changed in what its member may know of (journal.for_member). amend_album's `request_done` closes
the member's series_update item once their agent has looked at a series' new photos. Nothing it can send binds it:
the member allows it on the web, from another device (members.decide_binding). A call from this computer with no
login, and a run of the estate's agent with its token, are as before.

The tools answer for a member (`member` above): every search, photo and album is as that member sees the
library (catalog.Live.visible_for), and what a tool makes is theirs. For a login that is its member; from this
computer with no token, the sysadmin. For a run of the estate's agent (members.agent, kind agent) acting for a
member, that member (agent_tasks.acting_for), never the sysadmin it calls as: a task Ben submits searches as Ben,
and its writes go to Ben's albums and change sets, journaled with the agent as the actor acting for Ben. Acting
for no one (the family archivist) it reads as itself, which is shown only photos private to nobody (private.py:
Private.agents), with the estate defaults applied; what it makes is its own, and an album it makes is published
at once as a family series it keeps (changes.py). It can propose change sets; nothing it can call applies one.
A photo private to someone else does not exist for the member a tool answers for: get_photos says "no such
photo", similar_photos "no photo has that id", as for an unknown id, and no count, place, person, album,
neighbour, story or question comes from it.

ask_member (multiplayer phase 7: stories.py) files a question for a member, or the family, and returns at once;
the member's inbox decides when they see it. get_photos carries each photo's stories that the member may read.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

import journal
from core.lifecycle import Unavailable

if TYPE_CHECKING:     # for the types only: the tools reach these through `app`
    from changes import Changes
    from tasks import Tasks

NOBODY = 0     # private.NOBODY: who is shown only photos private to nobody
TOKEN_HEADER = "X-Estate-Agent"   # a run's token (tasks.Tasks.mint)
Found = tuple[list[str], list[float] | None]   # ids best first, and their scores (None: ranked by date)


class App(Protocol):
    """What the tools are made of (the module docstring lists it). PhotoApp gives an AppFacade: its Browse's
    find, row, faces and overview, and its change sets and tasks. A test can give any object with these."""
    @property
    def find(self) -> Callable[[dict, int | None], Found]: ...
    @property
    def row(self) -> Callable[[str, int | None], dict[str, Any] | None]: ...
    @property
    def faces(self) -> Callable[[list[str]], dict[str, tuple[int, int]]]: ...
    @property
    def overview(self) -> Callable[[int | None], dict[str, Any]]: ...
    @property
    def changes(self) -> Changes: ...
    @property
    def tasks(self) -> Tasks: ...
    @property
    def ask(self) -> Callable[..., dict[str, Any]]: ...
    @property
    def stories(self) -> Callable[[list[str], int | None], dict[str, list[dict]]]: ...
    @property
    def family(self) -> Callable[[], list[dict[str, Any]]]: ...
    @property
    def journal(self) -> Callable[[int | None, int, int], dict[str, Any]]: ...


@dataclass(frozen=True)
class Caller:
    """Who a tool call is for. `viewer`: the member whose view it reads and whose albums it writes to; `actor`:
    who the journal names (the estate's agent, for a run); `acting_for`: the member a run works for (None: no
    one); `task`: the run's task (agent_tasks.id), or None for anyone else."""
    who: str = ""                 # created_by: the login, or task:<id>
    actor: int | None = None
    acting_for: int | None = None
    viewer: int | None = None
    task: int | None = None
    binding: int | None = None    # a member's own agent: its agent_bindings.id (the member is the actor)


@dataclass(frozen=True)
class AppFacade:
    """The App that `embed.py serve` (PhotoApp) gives the server."""
    find: Callable[[dict, int | None], Found]
    row: Callable[[str, int | None], dict[str, Any] | None]
    faces: Callable[[list[str]], dict[str, tuple[int, int]]]
    overview: Callable[[int | None], dict[str, Any]]
    changes: Changes
    tasks: Tasks
    ask: Callable[..., dict[str, Any]]                             # stories.ask, with the app's refused
    stories: Callable[[list[str], int | None], dict[str, list[dict]]]   # stories.about
    family: Callable[[], list[dict[str, Any]]]                     # the family's members, for ask_member's `to`
    journal: Callable[[int | None, int, int], dict[str, Any]]      # journal.for_member, for journal_since


VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
BASE = """Photo Estate: one family's photo library, with tools to find photos, make albums, propose what to hide, and ask the family what only they know.

Whose eyes. Every tool answers as one member sees the library, and what you make is theirs: the person you are
signed in as, or, for a run of the estate's agent, the member it acts for. Acting for no one, the estate's agent
is the family archivist: it sees only what the whole family may see, and an album it makes is a family series,
published for everyone. A photo private to its sharer is never shown to anyone else, by any tool: it is as if it
did not exist (no id, count, place, person, story or question comes from it). Do not try to reach one.

Finding. search_photos ranks photos against a description with an image-text model (SigLIP 2) and can
narrow by person, place, album, camera and date. You never see a photo: you get ids, scores, dates,
places and the names of people in it. A score is a cosine between the description and the photo: a
photo of the thing described scores about 0.085-0.16, an unrelated one under about 0.07, and in a big
library scores fall smoothly with no gap, so read the `summary` each search returns (how many match,
the score at several ranks, the years they come from, how many have a named person) before choosing a
`min_score`. Describe what is in the picture ("young children in a forest"), not who or why; several
plain descriptions find more than one clever one. similar_photos finds photos that look like one you
have. get_photos tells everything known of a photo, with the stories people have told about it.

Albums. An album is a named set of photos that the member sees in the app's Albums tab. Making one changes
nothing else about the library, so you make and fill albums directly: create_album, then amend_album to
add photos by id or by search, each call optionally under a `section` (a heading in the album: a year, a
person, a place) and with a `reason`. For "the best five of X for every year", search each year with
order "best" and distinct true, and add the top five under that year's section. With a `search`,
create_album makes a live album instead: whatever that search finds, now and later. With a `search` and
`grow` true it makes a series: what the search finds now, chosen photos that you or the member can take out,
and new photos that match are added as they arrive (for a family series by the estate's agent; for a member's
own, the member is asked). Give the person the album's `view` link when it is ready.

Hiding. Hiding is personal: what a member hides is hidden for them alone (their own filter), unless a sysadmin
hides it for everyone. A change set is an album put up for hiding (propose_hiding does that to one you have made).
To hide photos, create a change set, add photos to it (by id, or everything a search finds), and give the person
its review link: they look it over, keep the photos that should not be hidden, and apply it themselves. You cannot
apply one. Each photo you add should carry a short reason a person can read. If a set has been reviewed,
get_change_set tells you which photos they kept: those are your mistakes, and the best guide to doing better.

Asking. Some things only people know: who is in a photo, where it was, what happened. ask_member files a
question for one member (`to`: a name library_overview lists under members) or, without `to`, for the whole
family, about photos they can see (anything else is refused). It returns at once; nobody answers while you
wait. Each member's inbox shows them only a few questions a week, photos nobody has told a story about first,
so ask little and plainly, and give questions about one event the same `group_key` ("Thanksgiving 1998"): they
are asked together. Say in `why` what the answer is for.

Stories. A story is what a member said about photos, attributed to them and dated: typed, or spoken and
transcribed. It is theirs: quote it, use it, never edit or correct it. Two people's stories of one moment are
both kept, side by side."""
HANDING = """Handing it over. For a request that covers many photos or needs judgement ("hide the photos from the
school"), submit_task gives it, in the person's own words, to the estate's agent, which runs on the computer the
library is on, acts for the person and works through it with these same tools. It takes minutes and keeps going
if this conversation ends. Check on it with get_task; when it is done, give the person its report and the
review link of the change set it made."""
OWN = """You are this member's own agent. You run in their harness, on their account, and connect with their login,
from one device. The first time you connect from a device the member is asked, in their inbox, to allow you: until
they do (on the web, from another device than yours), every tool that only looks works, and every tool that changes
something answers that they must allow you first. Once allowed, what you make is theirs and the journal names them,
by their agent. There is no submit_task for you: do the work here, with these tools.

Keeping up. journal_since(cursor) tells what has changed in what your member may know of, oldest first: their
albums and lenses, published series, new photos they may see, stories they may read, their questions and inbox. Keep
the `next_cursor` it gives and start there next time (0 the first time: everything); while `more` is true there is
more to read. A series.update_requested entry means new photos may belong in one of your member's series (an album
that grows from a search): look at them (get_photos; get_album and similar_photos to compare with what it holds), add
those that belong with amend_album, and then call amend_album with `request_done` true, which closes the member's inbox
item about them (the photos you left out stay out). Its `waiting` says whether that item is still open."""
INSTRUCTIONS = BASE + "\n\n" + HANDING
OWN_INSTRUCTIONS = BASE + "\n\n" + OWN

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
    "kind": {"type": "string", "enum": ["photo", "video"],
             "description": "Only photos, or only videos. A video is found by what its frames show; its row has kind 'video' and seconds."},
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
                     "search": {**schema(find), "description": "Makes it a live album of what this search finds."},
                     "grow": {"type": "boolean", "description": "With `search`: a series instead of a live album. It starts as"
                              " what the search finds now, its photos can be taken out, and new photos that match are added"
                              " as they arrive."}},
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
                     "prompt": {"type": "string"},
                     "request_done": {"type": "boolean", "description": "true: the new photos waiting for this series"
                                      " (its series.update_requested entries) have been looked at, and those that belong"
                                      " added; closes the member's inbox item about them. The rest stay out."}},
                    ["album"]), self.amend_album),
            ("propose_hiding", True,
             "Put an album up for hiding: it becomes a change set the person reviews and applies. Nothing is hidden until they do.",
             schema({"album": {"type": "integer"}}, ["album"]), self.hide_album),
            ("ask_member", True,
             "Ask one member, or the whole family, a question about photos: who is in them, where, what happened. Returns at once: it waits in their inbox, which shows each member only a few questions a week, so ask little. Only photos they can see (for the family, photos everyone can see); a question naming any other photo is refused. The answer is a story, told later.",
             schema({"photos": {"type": "array", "items": {"type": "string"}, "description": "1 to 20 photo ids the question is about."},
                     "text": {"type": "string", "description": "The question, plainly, as the person will read it."},
                     "why": {"type": "string", "description": "What the answer is for, in a sentence: shown with the question."},
                     "to": {"type": "string", "description": "A member's name, as library_overview lists them. Without it, the whole family."},
                     "group_key": {"type": "string", "description": "Questions about one event share it and are asked together, e.g. \"Thanksgiving 1998\"."},
                     "priority": {"type": "number", "description": "0 to 1: how much this one matters beside your others (default 0)."},
                     "follow_up": {"type": "boolean", "description": "true: when someone answers, start a new run of the estate's agent with the question and the answer, to act on it (say how in `why`)."}},
                    ["photos", "text"]), self.ask_member),
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
            ("journal_since", False,
             "What has changed in what your member may know of since `cursor`, oldest first: their albums, lenses and"
             " favourites, published series and lenses, new photos they may see, stories they may read, their questions,"
             " inbox and agents, and series.update_requested for their series. Returns `entries`, `next_cursor` (keep it"
             " for next time) and `more`.",
             schema({"cursor": {"type": "integer", "description": "The next_cursor of your last call; 0 the first time."},
                     "limit": {"type": "integer", "description": "How many entries at most (default 100, at most 500)."}}),
             self.journal_since),
        ]
        self.by_name = {t[0]: t for t in self.tools}

    TASK_TOOLS = ("submit_task", "get_task", "cancel_task")   # not for the agent a task runs, nor a member's own
    OWN_TOOLS = ("journal_since",)                             # only for a member's own agent

    @classmethod
    def offered(cls, name: str, in_task: bool = False, own: bool = False) -> bool:
        return not ((in_task or own) and name in cls.TASK_TOOLS or not own and name in cls.OWN_TOOLS)

    def listing(self, in_task: bool = False, own: bool = False) -> list[dict]:
        return [{"name": n, "description": d, "inputSchema": s,
                 "annotations": {"readOnlyHint": not w, "destructiveHint": False, "openWorldHint": False}}
                for n, w, d, s, _ in self.tools if self.offered(n, in_task, own)]

    def journal_since(self, args, cx: Caller):
        cursor = max(0, int(args.get("cursor") or 0))
        return self.app.journal(cx.viewer, cursor, max(1, min(int(args.get("limit") or 100), 500)))

    # -- tasks -----------------------------------------------------------------------------------

    def task_brief(self, t: dict) -> dict:
        out = {"task": t["id"], "status": t["status"], "request": t["prompt"], "report": t["result"], "error": t["error"],
               "change_sets": t["change_sets"], "last_event": (t["last"] or {}).get("id")}
        if "events" in t:
            out["events"] = [{"kind": e["kind"], "text": e["text"]} for e in t["events"][-40:]]
        return {k: v for k, v in out.items() if v is not None}

    def submit_task(self, args, cx: Caller):
        got, code = self.app.tasks.submit(args.get("request"), cx.who, cx.viewer)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.task_brief(got), "next": "get_task shows how it is going; it usually takes a few minutes"}

    def get_task(self, args, cx: Caller):
        got = self.app.tasks.get(int(args["task"]), max(0, int(args.get("after") or 0)), cx.viewer)
        if not got:
            raise ValueError("no such task")
        return self.task_brief(got)

    def cancel_task(self, args, cx: Caller):
        got, code = self.app.tasks.cancel(int(args["task"]), cx.actor)
        if code >= 400:
            raise ValueError(got["error"])
        return self.task_brief(got)

    # -- finding ---------------------------------------------------------------------------------

    def rows(self, ids, cx: Caller, scores=None) -> list[dict]:
        faces = self.app.faces(ids)
        out = []
        for i, sha in enumerate(ids):
            row = self.app.row(sha, cx.viewer)
            if row is None:
                out.append({"id": sha, "error": "no such photo"})
                continue
            if scores is not None:
                row["score"] = scores[i]
            if sha in faces:
                row["faces"], row["named_faces"] = faces[sha]
            out.append(row)
        return out

    def summary(self, ids, scores, cx: Caller) -> dict:
        years, named = Counter(), 0
        for sha in ids:
            row = self.app.row(sha, cx.viewer) or {}
            years[(row.get("date") or "")[:4] or "undated"] += 1
            named += bool(row.get("people"))
        out = {"matching": len(ids), "with_named_people": named, "by_year": dict(sorted(years.items()))}
        if scores:
            out["score_at_rank"] = {str(r): scores[r - 1] for r in (1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000) if r <= len(scores)}
            out["score_at_rank"][str(len(scores))] = scores[-1]
        return out

    def page(self, ids, scores, args, cx: Caller) -> dict:
        limit = max(1, min(int(args.get("limit") or 20), 200))
        at = max(0, int(args.get("offset") or 0))
        return {"summary": self.summary(ids, scores, cx),
                "photos": self.rows(ids[at:at + limit], cx, scores[at:at + limit] if scores else None),
                "next_offset": at + limit if at + limit < len(ids) else None}

    def overview(self, args, cx: Caller):
        return {**self.app.overview(cx.viewer), "members": self.app.family()}

    def search(self, args, cx: Caller):
        ids, scores = self.app.find(args, cx.viewer)
        return self.page(ids, scores, args, cx)

    def similar(self, args, cx: Caller):
        ids, scores = self.app.find({**args, "like": args["id"], "query": None}, cx.viewer)
        return self.page(ids, scores, args, cx)

    def photos(self, args, cx: Caller):
        rows = self.rows([str(s) for s in args["ids"]][:200], cx)
        told = self.app.stories([r["id"] for r in rows if "error" not in r], cx.viewer)
        for r in rows:
            if told.get(r["id"]):
                r["stories"] = [{"told_by": b["told_by"]["name"], "told_at": b["told_at"],
                                 **{k: b[k] for k in ("text", "transcript", "transcript_state") if b.get(k)},
                                 **({"question": b["question"]["text"]} if b["question"] else {}),
                                 **({"recording": True} if b["recording"] else {}),
                                 "photos": b["photos"]} for b in told[r["id"]]]
        return {"photos": rows}

    def ask_member(self, args, cx: Caller):
        if args.get("follow_up") and cx.binding is not None:
            raise ValueError("follow_up starts a run of the estate's agent, on the owner's subscription: you are your"
                             " member's own agent, so read the answer yourself (a story.told entry in journal_since)")
        return self.app.ask(cx.actor, cx.acting_for, args.get("photos"), args.get("text"), args.get("why"), args.get("to"),
                            args.get("group_key"), args.get("priority"), bool(args.get("follow_up")), cx.task)

    # -- albums ----------------------------------------------------------------------------------

    def album_brief(self, s: dict) -> dict:
        out = {"album": s["id"], "title": s["title"], "photos": s["n"], "live": s["kind"] == "live" or None,
               "search": s["query"], "grows_from": s.get("grows_from"), "view": f"/?album={s['id']}",
               "family_series": True if s.get("maintained_by") is not None else None}
        if s["op"] == "hide":
            out.update(up_for_hiding=True, state=s["state"], hidden_now=s["applied"], review=f"/changes/{s['id']}")
        return {k: v for k, v in out.items() if v is not None}

    def albums(self, args, cx: Caller):
        return {"albums": [self.album_brief(s) for s in self.app.changes.listing(albums=True, viewer=cx.viewer)["sets"]]}

    def get_album(self, args, cx: Caller):
        limit, at = max(1, min(int(args.get("limit") or 20), 200)), max(0, int(args.get("offset") or 0))
        got = self.app.changes.photos(int(args["album"]), at, limit, cx.viewer)
        if got is None:
            raise ValueError("no such album")
        extra = {r["sha"]: {k: r[k] for k in ("section", "reason") if r.get(k)} for r in got["results"]}
        return {**self.album_brief(got["album"]), "prompt": got["album"]["prompt"], "sections": got["sections"],
                "photos": [{**row, **extra.get(row["id"], {})} for row in self.rows([r["sha"] for r in got["results"]], cx)],
                "next_offset": got["next"]}

    def create_album(self, args, cx: Caller):
        search = args.get("search")
        if args.get("grow") and search is None:
            raise ValueError("grow makes a series from a search: give `search` too")
        got, code = self.app.changes.create({"title": args.get("title"), "prompt": args.get("prompt"), "op": "view",
                                             **({"grows_from" if args.get("grow") else "query": search} if search is not None else {})},
                                            cx.who, cx.actor, cx.acting_for)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.album_brief(got), "next": "amend_album adds photos; then give the person the view link"
                if got["kind"] != "live" else "it holds what the search finds; give the person the view link"}

    def amend_album(self, args, cx: Caller):
        sid, out = int(args["album"]), None
        changes = any(args.get(k) for k in ("add_ids", "add_search", "remove_ids", "remove_search"))
        if args.get("title") or "prompt" in args:
            got, code = self.app.changes.write(sid, "rename", {k: args[k] for k in ("title", "prompt") if k in args},
                                               cx.who, cx.actor, cx.acting_for)
            if code >= 400:
                raise ValueError(got["error"].replace("change set", "album"))
            out = self.album_brief(got)
        if changes or out is None and not args.get("request_done"):
            got = self.amend_set({**args, "change_set": sid}, cx)
            out = {**{k: v for k, v in got.items() if k not in ("change_set", "state", "hidden_now", "kept_by_person", "review")},
                   "album": sid, "view": f"/?album={sid}"}
        if args.get("request_done"):     # the series' waiting photos have been looked at: its owner's item is done
            got, code = self.app.changes.write(sid, "candidates", {"add": []}, cx.who, cx.actor, cx.acting_for)
            if code >= 400:
                raise ValueError(got["error"].replace("change set", "album"))
            out = {**(out or self.album_brief(got)), "request_done": True}
        return out

    def hide_album(self, args, cx: Caller):
        got, code = self.app.changes.write(int(args["album"]), "hide", {}, cx.who, cx.actor, cx.acting_for)
        if code >= 400:
            raise ValueError(got["error"].replace("change set", "album"))
        return {**self.album_brief(got), "next": "give the person the review link: they look it over and apply it"}

    # -- change sets -----------------------------------------------------------------------------

    def brief(self, s: dict) -> dict:
        return {"change_set": s["id"], "title": s["title"], "state": s["state"], "photos": s["n"],
                "hidden_now": s["applied"], "kept_by_person": s["reject"], "review": f"/changes/{s['id']}"}

    def sets(self, args, cx: Caller):
        return {"change_sets": [self.brief(s) for s in self.app.changes.listing(viewer=cx.viewer)["sets"]]}   # (albums up for hiding)

    def get_set(self, args, cx: Caller):
        d = self.app.changes.detail(int(args["change_set"]), cx.viewer)
        if d is None:
            raise ValueError("no such change set")
        kept = [it["sha"] for it in d["items"] if it["decision"] == "reject"]
        rest = [it["sha"] for it in d["items"] if it["decision"] != "reject"]
        reasons = {it["sha"]: it["reason"] for it in d["items"] if it["reason"]}
        def listed(ids):
            return [{**row, **({"reason": reasons[row["id"]]} if row["id"] in reasons else {})} for row in self.rows(ids, cx)]
        out = {**self.brief(d), "prompt": d["prompt"], "kept": listed(kept[:200]), "kept_not_listed": max(0, len(kept) - 200)}
        n = max(0, min(int(args.get("sample") or 0), 200))
        if n:
            out["sample"] = listed(rest[::max(1, len(rest) // n)][:n])
        return out

    def create_set(self, args, cx: Caller):
        got, code = self.app.changes.create({"title": args.get("title"), "prompt": args.get("prompt"), "op": "hide"},
                                            cx.who, cx.actor, cx.acting_for)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.brief(got), "next": "amend_change_set adds photos; then give the person the review link"}

    def found(self, spec: dict, reason, cx: Caller):
        ids, scores = self.app.find(spec, cx.viewer)
        n = max(0, min(int(spec.get("max") or 20000), 20000))
        why = reason or (f"Matches \"{spec['query']}\"" if spec.get("query") else "Looks like a photo of the set" if spec.get("like") else None)
        return [{"sha": s, "score": scores[i] if scores else None, "reason": why} for i, s in enumerate(ids[:n])], len(ids)

    def amend_set(self, args, cx: Caller):
        sid, reason, section = int(args["change_set"]), args.get("reason"), args.get("section")
        items = [{"sha": str(s), "reason": reason} for s in args.get("add_ids") or []]
        out = {}
        if args.get("add_search"):
            got, out["add_search_matched"] = self.found(args["add_search"], reason, cx)
            items += got
        remove = [str(s) for s in args.get("remove_ids") or []]
        if args.get("remove_search"):
            got, out["remove_search_matched"] = self.found(args["remove_search"], None, cx)
            remove += [it["sha"] for it in got]
        for it in items:
            it["section"] = section
        got, code = self.app.changes.write(sid, "items", {"items": items, "remove": remove}, cx.who, cx.actor, cx.acting_for)
        if code >= 400:
            raise ValueError(got["error"])
        return {**self.brief(got), **out, "added": got["added"], "already_in_set": len(items) - got["added"] - got["unknown"],
                "unknown_ids": got["unknown"], "removed": got["removed"]}


class Server:
    """JSON-RPC over HTTP POST, as much of MCP as tools need."""

    def __init__(self, app: App, name="photo-estate", version="1"):
        self.tools, self.info = Tools(app), {"name": name, "title": "Photo Estate", "version": version}

    def call(self, msg, can_write: bool, cx: Caller, in_task: bool = False, why_not: str | None = None):
        """One JSON-RPC message. `why_not`: what a writing tool answers when `can_write` is false (a member's own agent
        that is not allowed yet); a member's own agent is the Caller with a binding."""
        own = cx.binding is not None
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
                       "serverInfo": self.info, "instructions": OWN_INSTRUCTIONS if own else INSTRUCTIONS})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tools.listing(in_task, own)})
        if method == "tools/call":
            tool = self.tools.by_name.get(params.get("name"))
            if tool and own and tool[0] in Tools.TASK_TOOLS:      # (refused in words: it may well have been told of it)
                return ok({"content": [{"type": "text", "text": f"{tool[0]} is not for a member's own agent: the estate's"
                                        " agent runs on the owner's subscription, and your work runs here, in your"
                                        " member's harness, with these tools."}], "isError": True})
            if not tool or not Tools.offered(tool[0], in_task, own):
                return err(-32602, f"unknown tool: {params.get('name')}")
            name, writes, _, _, fn = tool
            args = params.get("arguments") or {}
            try:
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                if writes and not can_write:
                    raise PermissionError(why_not or "this caller may look but not propose changes")
                with journal.by_agent(cx.binding):
                    out = fn(args, cx)
                return ok({"content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"), default=str)}],
                           "structuredContent": out, "isError": False})
            except (ValueError, KeyError, TypeError, PermissionError, Unavailable) as e:   # the agent can read it and try again
                # (Unavailable: the search model is still loading, or failed to load; encoder.py)
                text = f"missing argument: {e}" if isinstance(e, KeyError) else str(e)
                return ok({"content": [{"type": "text", "text": text}], "isError": True})
        return err(-32601, f"method not found: {method}")

    def handle(self, method: str, headers, body, can_write: bool, who: str = "", local: bool = False,
               member: int | None = None, agent: dict | None = None):
        """One HTTP request to /mcp -> (status, body bytes, content type, Cache-Control). `member`: the member
        making it (members.id). A run of the estate's agent carries its token (X-Estate-Agent, from this computer,
        while the run lasts): then it is the agent, acting for the run's member (tasks.Run). `agent`: a call with a
        member's login is their own agent, and this is its binding (members.bind, with the member's `name`)."""
        js = lambda o, code=200: (code, json.dumps(o, separators=(",", ":"), default=str).encode(), "application/json", "no-store")
        refuse = lambda why: js({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": why}}, 403)   # noqa: E731
        # A page on another site must not be able to drive this from someone's browser.
        origin, host = headers.get("Origin"), headers.get("Host") or ""
        if origin and origin.split("://", 1)[-1] != host:
            return js({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "cross-origin requests are refused"}}, 403)
        if method != "POST":         # no server-to-client stream, no session to end
            return 405, b"", "text/plain", "no-store"
        if headers.get("X-Estate-Task") is not None:     # a run from before tokens: never answered as this computer
            return refuse("X-Estate-Task is no longer honoured: a run of the estate's agent carries its own token")
        token = headers.get(TOKEN_HEADER)
        cx, in_task, why_not = Caller(who, member, None, member), False, None
        if token is None and agent is not None:      # a member's own agent, on one device: what its binding allows
            name, device = agent.get("name") or "your member", agent.get("device") or "this device"
            if agent["status"] == "revoked":
                return refuse(f"{name} has turned off their agent on {device}. They can allow it again on Connect your"
                              " agent (/agents, in the Library's menu), from another device.")
            cx = Caller(who, member, None, member, binding=agent["id"])
            if agent["status"] != "approved":
                can_write = False
                why_not = (f"Not yet: {name} has not allowed this agent (on {device}) to change anything. It is in their"
                           " inbox (the Library's menu, Inbox, or /agents): they allow it there, from another device than"
                           " this one. Until then every tool that only looks works.")
        if token is not None:
            run = self.tools.app.tasks.run_of(token) if local else None
            if run is None:
                return refuse("this token is not a running task's (it ended, or this computer never gave it), or it came"
                              " from another computer")
            # the estate's agent, working for the task's member: it sees what they see; acting for no one, what is
            # private to nobody (its own view: private.Private.agents)
            if not self.tools.app.tasks.lets(run.acting_for):     # revoked since the run began (issue #131)
                return refuse("the member this run acts for is no longer let in: the run is over")
            viewer = run.acting_for if run.acting_for is not None else run.agent
            cx, in_task = Caller(f"task:{run.task}", run.agent, run.acting_for, viewer, run.task), True
        if body is None:
            return js({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "the body is not JSON"}}, 400)
        if isinstance(body, list):
            got = [r for r in (self.call(m, can_write or in_task, cx, in_task, why_not) for m in body) if r is not None]
            return js(got) if got else (202, b"", "application/json", "no-store")
        got = self.call(body, can_write or in_task, cx, in_task, why_not)
        return js(got) if got is not None else (202, b"", "application/json", "no-store")
