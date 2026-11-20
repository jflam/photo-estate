"""The facts more than one part of Photo Estate must agree on, in one place (standard library only: no
Pillow, no numpy, no models, so the setup server, the CLI tools and the tests can all import it).

  media.py    which files are photos and videos, their MIME types, and which ones each path decodes
  schema.py   every SQLite table, and the additive migrations, as ensure(con, area, ...)
  config.py   the default ports and the URLs built from them

A script in photo-estate/ imports it as `from core import media`: the script's own folder is on
sys.path. The intake/ package is imported from there too. Scripts in a subfolder (tests/, tools/...)
put photo-estate/ on sys.path first, as they already do for their other imports.
"""
