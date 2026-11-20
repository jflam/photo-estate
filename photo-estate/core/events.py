"""What a pipeline script tells the program that started it, as one JSON object a line (standard library
only: estate.py, places.py, embed.py, faces.py, upload.py and intake/engine.py all import it, each in its
own environment). Issue #84: the app used to read progress and "catalog ready" out of the human log.

  ENV = "PHOTO_ESTATE_EVENTS"   the parent sets it to "1"; without it nothing below prints anything
  PREFIX = "@@pe "              what starts every line of the protocol, on the script's stdout
  VERSION = "1.0"               MAJOR.MINOR: a reader takes any 1.x line and ignores fields it does not know

  enabled() -> bool                                  is ENV "1" in this process's environment
  line(type, **fields) -> str                        one protocol line, without the newline
  emit_progress(stage, done, total=None, what=None)  how far a step has got
  emit_event(name, **fields)                         something the parent acts on (new_assets, catalog_ready)
  parse(line) -> Progress | Event | None             None for a human line, a malformed one, another major
                                                     version or a type this reader does not know

A line is `@@pe {"v":"1.0","type":"progress","stage":"embed","done":40,"total":120}` or
`@@pe {"v":"1.0","type":"event","name":"catalog_ready"}`. The parent reads the child's stdout with stderr
merged into it, so the protocol lines stay in order with the human ones around them; a parent hides every
line that starts with PREFIX from what it shows people. The human lines are unchanged, for people only.
docs/book/16-operations.md lists the stages and events.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TypeGuard

ENV = "PHOTO_ESTATE_EVENTS"
PREFIX = "@@pe "
VERSION = "1.0"
MAJOR = 1


@dataclass(frozen=True)
class Progress:
    stage: str               # the step reporting: ingest, link, exif, search, places, embed, faces
    done: int
    total: int | None = None
    what: str | None = None  # what the step is doing now, for the page ("matching each photo with its details")


@dataclass(frozen=True)
class Event:
    name: str                # new_assets {count}, catalog_ready; a reader ignores names it does not know
    fields: dict = field(default_factory=dict)


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def line(type: str, **fields) -> str:
    return PREFIX + json.dumps({"v": VERSION, "type": type, **fields}, separators=(",", ":"), ensure_ascii=False)


def _emit(type: str, **fields) -> None:
    if enabled():
        print(line(type, **fields), flush=True)


def emit_progress(stage: str, done: int, total: int | None = None, what: str | None = None) -> None:
    kv: dict = {"stage": stage, "done": int(done)}
    if total is not None:
        kv["total"] = int(total)
    if what:
        kv["what"] = what
    _emit("progress", **kv)


def emit_event(name: str, **fields) -> None:
    _emit("event", name=name, **fields)


def _count(x) -> TypeGuard[int]:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def parse(text: str) -> Progress | Event | None:
    """The event on a line of a child's output, or None (never raises)."""
    if not text.startswith(PREFIX):
        return None
    try:
        obj = json.loads(text[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    v = obj.get("v")
    major = v.split(".", 1)[0] if isinstance(v, str) else None
    if major != str(MAJOR):
        return None
    if obj.get("type") == "progress":
        stage, done, total, what = obj.get("stage"), obj.get("done"), obj.get("total"), obj.get("what")
        if not isinstance(stage, str) or not _count(done) or not (total is None or _count(total)) \
                or not (what is None or isinstance(what, str)):
            return None
        return Progress(stage, done, total, what)
    if obj.get("type") == "event":
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            return None
        return Event(name, {k: x for k, x in obj.items() if k not in ("v", "type", "name")})
    return None
