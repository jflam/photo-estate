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
  ClusterState   faces.People's clustering of unnamed faces: waiting | working | ready

Beside the lifecycles, the sets of states the modules test against: TASK_ACTIVE (a task that can be
cancelled), JOB_ACTIVE and JOB_RETRYABLE (intake jobs). The transitions themselves are the modules'
(upload.IngestJob.MOVES is the one written down as a table so far).
"""
from __future__ import annotations

from enum import StrEnum


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
