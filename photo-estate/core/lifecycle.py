"""Something the app loads in a background thread (the search model, the People page) as one small state
machine, so a request can always tell "still loading" from "failed" and never waits forever.

  Phase                  idle | loading | ready | failed  (a StrEnum: a member equals its string)
  Lifecycle(name)        .begin()  .succeed(value=None)  .fail(exc)       the loader's side
                         .wait(timeout) -> Phase   .require(timeout) -> value   .snapshot() -> Snapshot
  Unavailable            what require() raises: .status is the HTTP status that says so
    StillLoading         503: loading (or not started), try again in a moment
    LoadFailed           500: the load raised; the message says what it raised

The moves (MOVES): idle -> loading -> ready | failed, and ready | failed -> loading again (a reload). Any
other move raises IllegalMove, so two loads of one thing at once are a bug that shows, not a race. A
loader runs begin(), then succeed() or fail() whatever happens (try/except), so every wait ends.

Everything is read and written under the object's lock; snapshot() is one consistent reading. `value` is
the last good one: a failed reload keeps it, so whatever was answering keeps answering, and `error`
says why the reload failed. The Event is set when a load ends (ready or failed) and cleared by begin();
an idle Lifecycle has nothing in flight, so wait() returns at once rather than waiting for a load that
was never started.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core.states import IllegalMove as IllegalMove, check_move   # IllegalMove: still importable from here


class Phase(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


MOVES: dict[Phase, frozenset[Phase]] = {
    Phase.IDLE: frozenset({Phase.LOADING}),
    Phase.LOADING: frozenset({Phase.READY, Phase.FAILED}),
    Phase.READY: frozenset({Phase.LOADING, Phase.IDLE}),   # idle again: what was loaded has been let go (release)
    Phase.FAILED: frozenset({Phase.LOADING}),
}


class Unavailable(RuntimeError):
    status = 503


class StillLoading(Unavailable):
    status = 503


class LoadFailed(Unavailable):
    status = 500


@dataclass(frozen=True)
class Snapshot:
    phase: Phase
    value: Any            # the last good value (None until the first load succeeds)
    error: str | None     # why the last load failed ("TypeName: message"), None once one succeeds
    at: float | None      # when the last load ended


class Lifecycle:
    def __init__(self, name: str):
        self.name, self.lock, self.done = name, threading.Lock(), threading.Event()
        self.phase, self.value, self.error, self.at = Phase.IDLE, None, None, None

    def _move(self, to: Phase) -> None:   # under self.lock
        check_move(MOVES, self.phase, to, self.name)
        self.phase = to

    def begin(self) -> None:
        with self.lock:
            self._move(Phase.LOADING)
            self.done.clear()

    def succeed(self, value: Any = None) -> None:
        with self.lock:
            self._move(Phase.READY)
            self.value, self.error, self.at = value, None, time.time()
            self.done.set()

    def fail(self, exc: BaseException) -> None:
        with self.lock:
            self._move(Phase.FAILED)
            self.error, self.at = f"{type(exc).__name__}: {exc}", time.time()
            self.done.set()

    def release(self) -> None:
        """Ready -> idle: what was loaded has been let go, and is loaded again when next asked for."""
        with self.lock:
            self._move(Phase.IDLE)
            self.value = None
            self.done.clear()

    def snapshot(self) -> Snapshot:
        with self.lock:
            return Snapshot(self.phase, self.value, self.error, self.at)

    def wait(self, timeout: float | None) -> Phase:
        """Wait up to `timeout` seconds (None: until it ends) for a load in flight; the phase then."""
        with self.lock:
            if self.phase is Phase.IDLE:
                return self.phase
        self.done.wait(timeout)
        with self.lock:
            return self.phase

    def require(self, timeout: float | None, loading: type[Unavailable] = StillLoading,
                failed: type[Unavailable] = LoadFailed) -> Any:
        """The value once ready; raises `loading` if it isn't by `timeout`, `failed` if the load raised."""
        self.wait(timeout)
        s = self.snapshot()
        if s.phase is Phase.READY:
            return s.value
        if s.phase is Phase.FAILED:
            raise failed(f"{self.name} failed to load: {s.error}")
        raise loading(f"{self.name} is still loading; try again in a moment" if s.phase is Phase.LOADING
                      else f"{self.name} has not started loading")
