"""Stopping a child process for good (standard library only).

  stop(proc, grace=10.0) -> exit code   SIGTERM, then up to `grace` seconds for it to go, then SIGKILL

What a task's timeout and cancel (tasks.py) and an intake job's cancel (intake/engine.py) use, so a
child that ignores SIGTERM (or is stuck) cannot keep its reader, and with it the job, waiting forever.
"""
from __future__ import annotations

import subprocess


def stop(proc: subprocess.Popen, grace: float = 10.0) -> int | None:
    """Terminate `proc`, kill it if it is still there after `grace` seconds; its exit code (None if even
    SIGKILL left it, which only an uninterruptible wait in the kernel does)."""
    if proc.poll() is not None:
        return proc.returncode
    try:
        proc.terminate()
        return proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    except ProcessLookupError:          # it went between poll() and terminate()
        return proc.poll()
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        return proc.wait(grace)
    except subprocess.TimeoutExpired:
        return None
