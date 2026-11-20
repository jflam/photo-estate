"""The finite domains the modules store and send as strings: a change set's operation and status, a
reviewer's decision, an agent task's status, the ingest's state, an intake job's state, the People page's
clustering. Each is a StrEnum whose values are the strings the catalog, intake.db, the JSON API and the
page already use, so the wire and database formats are unchanged: a member equals its string
(`Decision.ACCEPT == "accept"`), hashes as it, binds into SQLite as it and serializes to JSON as it. A
value read back from SQLite or a JSON body is a plain str; `Decision("accept")` (ValueError for anything
else) turns it into a member where the code needs one.

  ChangeOp       change_sets.op        hide | view
  ChangeStatus   change_sets.status    open | reverted | done
  AlbumKind      change_sets.kind      list | live
  SetState       what a set has come to (derived): empty | album | review | applied | reverted | done
  Decision       change_items.decision pending | accept | reject
  TaskStatus     agent_tasks.status    queued | running | done | failed | cancelled   (tasks.py)
  IngestState    upload.IngestJob      idle | quiet | starting | running | backoff
  JobState       intake jobs.state     queued | running | waiting | done | failed | cancelled
  StageState     an intake stage, as the page shows it: done | pending | running | waiting | failed
  ClusterState   faces.People's clustering of unnamed faces: waiting | working | ready | failed

Beside the lifecycles, the sets of states the modules test against: TASK_ACTIVE (a task that can be
cancelled), JOB_ACTIVE and JOB_RETRYABLE (intake jobs).

The moves each lifecycle may make are tables, {state: the states it may go to}, and every write of a
state goes through check_move(table, from, to, what), which raises IllegalMove for any other step
(issue #86): TASK_MOVES (tasks.Tasks.move), JOB_MOVES (intake.engine.Store.move), and
upload.IngestJob.MOVES (IngestJob._to). core.lifecycle's MOVES is checked the same way.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Collection, Mapping


class IllegalMove(RuntimeError):
    """A state change its lifecycle's table does not allow: a bug (or a lost race) that shows."""


def check_move(moves: Mapping, a, b, what: str = "") -> None:
    """Raise IllegalMove unless `moves` lets state `a` go to `b`."""
    allowed: Collection = moves.get(a, ())
    if b not in allowed:
        raise IllegalMove(f"{what + ': ' if what else ''}{a} -> {b}")


class ChangeOp(StrEnum):
    HIDE = "hide"     # a change set: hides its photos once applied
    VIEW = "view"     # an album to look at; changes nothing


class ChangeStatus(StrEnum):
    OPEN = "open"
    REVERTED = "reverted"
    DONE = "done"


class Decision(StrEnum):
    PENDING = "pending"
    ACCEPT = "accept"
    REJECT = "reject"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_ACTIVE = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})     # a task that can still be cancelled
TASK_MOVES: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),     # the worker claims it, or it is withdrawn
    TaskStatus.RUNNING: frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}),   # failed: also an app restart
    TaskStatus.DONE: frozenset(), TaskStatus.FAILED: frozenset(), TaskStatus.CANCELLED: frozenset(),
}


class IngestState(StrEnum):
    IDLE = "idle"
    QUIET = "quiet"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"       # for the person: a sign-in code, a choice
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


JOB_ACTIVE = frozenset({JobState.QUEUED, JobState.RUNNING, JobState.WAITING})   # not finished: can be cancelled
JOB_RETRYABLE = frozenset({JobState.FAILED, JobState.CANCELLED})                 # can be tried again
_J = JobState
JOB_MOVES: dict[JobState, frozenset[JobState]] = {
    _J.QUEUED: frozenset({_J.RUNNING, _J.CANCELLED}),                       # the engine claims it, or it is withdrawn
    _J.RUNNING: frozenset({_J.WAITING, _J.DONE, _J.FAILED, _J.CANCELLED,
                           _J.QUEUED}),                                     # queued: a restart resumes it
    _J.WAITING: frozenset({_J.RUNNING, _J.FAILED, _J.CANCELLED, _J.QUEUED}),   # running: the person answered
    _J.DONE: frozenset(),
    _J.FAILED: frozenset({_J.QUEUED}),                                      # retry()
    _J.CANCELLED: frozenset({_J.QUEUED}),
}
del _J


class StageState(StrEnum):
    DONE = "done"
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"


class ClusterState(StrEnum):
    WAITING = "waiting"
    WORKING = "working"
    READY = "ready"
    FAILED = "failed"      # the clustering raised: People.clusters_error says what


class AlbumKind(StrEnum):
    LIST = "list"     # the photos it was given
    LIVE = "live"     # whatever its search finds now


class SetState(StrEnum):
    """What a change set has come to (changes.Changes.summary's `state`; derived, never stored)."""
    EMPTY = "empty"
    ALBUM = "album"         # an album (op view) with photos, or a live one
    REVIEW = "review"       # still being looked over: some photos are not yet as marked
    APPLIED = "applied"     # applied in full, waiting to be committed or reverted
    REVERTED = "reverted"
    DONE = "done"
