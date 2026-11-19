"""Bringing photos into the library from the app: the setup page's engine.

  base.py      what a source is (guide, checks, sign-in, acquire), and @register
  sources/     every source: local.py (files on this computer), practice.py (--sandbox stand-ins)
  engine.py    jobs, and the catalogue pipeline every source feeds (ingest, link, exif, places, embed, faces)
  api.py       the HTTP API the setup page (and, later, an agent over MCP) uses
  server.py    the small server that runs before there is a library to show

See docs/notes/INTAKE-PLAN.md for the plan this is phase 1 of."""
