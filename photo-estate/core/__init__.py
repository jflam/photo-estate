"""The facts more than one part of Photo Estate must agree on, in one place (standard library only: no
Pillow, no numpy, no models, so the setup server, the CLI tools and the tests can all import it).

  media.py    which files are photos and videos, their MIME types, and which ones each path decodes
  schema.py   every SQLite table, and the additive migrations, as ensure(con, area, ...)
  config.py   the default ports and the URLs built from them
  states.py   the finite string domains (change set op and status, decisions, task, ingest and intake job
              states) as StrEnums whose values are the strings already stored and sent
  lifecycle.py  something loaded in a background thread (the search model, the People page): idle |
              loading | ready | failed under a lock, so a request is told "loading" or "failed", never hangs
  procs.py    stop(proc, grace): SIGTERM, then SIGKILL once the grace is up (a task's or an intake job's child)
  events.py   the pipeline steps' event lines (`@@pe {json}`, only with PHOTO_ESTATE_EVENTS=1): progress, new_assets,
              catalog_ready, written by the scripts and read by upload.py and intake/engine.py (issue #84)

A script in photo-estate/ imports it as `from core import media`: the script's own folder is on
sys.path. The intake/ package is imported from there too. Scripts in a subfolder (tests/, tools/...)
put photo-estate/ on sys.path first, as they already do for their other imports.
"""
