"""The shape every intake source has: what the setup page (and later an agent, over MCP) needs to know
about it, and the one thing it does, which is to put a service's photos somewhere `estate.py ingest`
can read them.

A source never touches the catalog. It ends by handing the engine a list of zips and folders; the
engine then runs the same pipeline for every source (ingest, link, exif, places, embed, faces). That
is why adding a source is small: Takeout's, iCloud's and Flickr's metadata are recognised by `ingest`
from the files themselves, so a source only has to explain itself, sign in, and fetch.

To add a source, subclass `Source` in a module under `intake/sources/` and decorate it with
`@register`. `intake/sources/local.py` is the simplest example; `intake/sources/practice.py` shows
each way of signing in (a browser approval, a password and a code)."""
from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


# How a source signs in. The setup page draws each differently; an agent reads it to know what the
# person has to do themselves.
AUTH_NONE = "none"                 # nothing to sign in to: files already on this computer
AUTH_BROWSER = "browser"           # approve access in a browser window (OAuth, e.g. rclone for Drive/Dropbox/OneDrive)
AUTH_PASSWORD_CODE = "password_code"   # a password, then a code sent to a device (iCloud)
AUTH_MANUAL = "manual"             # the service emails a download: the person fetches it, then points here


@dataclass
class Step:
    """One instruction in a source's guide. `link` is a page the person opens; `note` is a caution."""
    title: str
    body: str = ""
    link: str | None = None
    link_label: str | None = None
    note: str | None = None


@dataclass
class Field:
    """Something the person fills in before a source runs. kind: path | text."""
    key: str
    label: str
    kind: str = "text"
    help: str = ""
    placeholder: str = ""
    required: bool = True


@dataclass
class Check:
    """A prerequisite: a tool that must be installed, a sign-in that must exist."""
    label: str
    ok: bool
    fix: str = ""                  # what to do when it is not ok (a command, or a sentence)
    required: bool = True          # False: the photos still come in, with something missing (e.g. exiftool)


@dataclass
class Preview:
    """What a source would bring in with this configuration, before anything runs."""
    ok: bool
    summary: str = ""
    problems: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)      # the zips/folders found, for the page to list
    media: int | None = None                            # photos and videos, when it is cheap to count


@dataclass
class Prompt:
    """The job is waiting for the person. kind:
       approve   a button ("I've done it")
       browser   open `url`, approve there; the job continues by itself
       password  a secret, typed into the app, held in memory only for this job
       code      a one-time code sent to the person's device
    Secrets are typed into the app only: the API never returns them and an agent never sees them."""
    kind: str
    label: str
    help: str = ""
    url: str | None = None
    secret: bool = False


class Cancelled(Exception):
    pass


class Context:
    """What a running source can do: say how far it has got, ask the person something, notice it was
    cancelled, and find its scratch space. The engine provides it."""

    def __init__(self, job_id: int, staging: Path, log: Callable[[str], None],
                 progress: Callable[[int | None, int | None, str], None],
                 ask: Callable[[Prompt], str], cancelled: Callable[[], bool], here: Path):
        self.job_id, self.staging, self.here = job_id, staging, here
        self.log, self._progress, self._ask, self._cancelled = log, progress, ask, cancelled

    def progress(self, done: int | None = None, total: int | None = None, detail: str = "") -> None:
        self.check()
        self._progress(done, total, detail)

    def ask(self, prompt: Prompt) -> str:
        """Block until the person answers in the app (or the job is cancelled)."""
        self.check()
        return self._ask(prompt)

    def check(self) -> None:
        if self._cancelled():
            raise Cancelled()


class Source:
    """A place photos come from. Class attributes describe it; methods do the work."""
    id: str = ""
    name: str = ""
    blurb: str = ""                # one line under the name
    group: str = "other"           # google | apple | flickr | cloud | computer | practice
    auth: str = AUTH_NONE
    time_hint: str = ""            # "about a day for Google to prepare the export", ...
    fields: list[Field] = []
    practice: bool = False         # a stand-in that only exists in --sandbox

    def guide(self) -> list[Step]:
        """How to get the photos out of the service, step by step."""
        return []

    def checks(self) -> list[Check]:
        """Prerequisites on this computer."""
        return []

    def preview(self, config: dict) -> Preview:
        """What would come in with this config. Must be quick and change nothing."""
        return Preview(ok=True)

    def acquire(self, config: dict, ctx: Context) -> list[Path]:
        """Fetch the photos (signing in through ctx.ask if needed) and return the zips and folders for
        `estate.py ingest`. Must be safe to run again after a stop: skip what is already fetched."""
        raise NotImplementedError

    # -- for the API

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "blurb": self.blurb, "group": self.group, "auth": self.auth,
                "time_hint": self.time_hint, "practice": self.practice,
                "fields": [asdict(f) for f in self.fields], "guide": [asdict(s) for s in self.guide()],
                "checks": [asdict(c) for c in self.checks()]}


def tool_check(cmd: str, label: str, fix: str, required: bool = True) -> Check:
    return Check(label=label, ok=shutil.which(cmd) is not None, fix=fix, required=required)


SOURCES: dict[str, Source] = {}


def register(cls):
    """Class decorator: make a source available to the engine and the setup page."""
    if not cls.id or cls.id in SOURCES:
        raise ValueError(f"source id {cls.id!r} is empty or already taken")
    SOURCES[cls.id] = cls()
    return cls
