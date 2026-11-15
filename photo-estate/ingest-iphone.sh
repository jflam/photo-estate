#!/usr/bin/env bash
# Catalogue whatever the phone has uploaded since the last run.
#
#   ./ingest-iphone.sh /Volumes/estate          (what a launchd timer runs)
#
# The photo app drops uploads in ROOT/iphone (see upload.py). This ingests that folder, and only if
# it brought anything new runs the rest of the pipeline on the new photos: every stage below only
# touches what is new: link is limited to the inbox, exif/embed/faces skip finished photos, and places
# reuses the place names and zones it already worked out (events and trips are rebuilt, in seconds). Nothing is deleted.
#
# - The photo app runs this itself and reloads its catalog at "catalog ready". Run by hand, set
#   RESTART=com.jflam.photo-search to restart the app at the end instead.
# - Set SKIP_MODELS=1 to catalogue only (ingest, link, exif, places) and leave embeddings and faces
#   for later, e.g. while the Mac is busy.
# - Safe to run while the photo app is up (the catalog is in WAL mode), and safe to overlap: a second
#   run exits at once if one is already going.
set -euo pipefail

ROOT=${1:?usage: ingest-iphone.sh ESTATE_ROOT   (e.g. /Volumes/estate)}
HERE=$(cd "$(dirname "$0")" && pwd)
DB=${DB:-$ROOT/estate.db}
STORE=${STORE:-$ROOT/store}
INBOX=${INBOX:-$ROOT/iphone}

# The store is the disk that must be there; the catalog (ROOT) may be on another one, e.g. the internal SSD.
[[ -d $STORE ]] || { echo "store $STORE does not exist (is the disk mounted?)"; exit 1; }
[[ -d $INBOX ]] || { echo "nothing to do: $INBOX does not exist yet"; exit 0; }
LOCK=${TMPDIR:-/tmp}/photo-estate-ingest-iphone.lock
mkdir "$LOCK" 2>/dev/null || { echo "already running ($LOCK)"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

run() { local s=$1; shift; echo "== $(date +%T) ${s%.py} $1"; uv run --quiet --script "$HERE/$s" --db "$DB" "$@"; }
count() { sqlite3 "$DB" "SELECT (SELECT count(*) FROM assets) + (SELECT count(*) FROM xmp)" 2>/dev/null || echo 0; }

top() { sqlite3 "$DB" "SELECT coalesce(max(rowid), 0) FROM assets" 2>/dev/null || echo 0; }

before=$(count)
top_before=$(top)
echo "== $(date +%T) ingest $INBOX"
run estate.py ingest --store "$STORE" --force --facts "$INBOX.partial/facts" "$INBOX"   # facts: see upload.py
after=$(count)
if [[ $before == "$after" ]]; then
  echo "nothing new"
  exit 0
fi
# exif, embed and faces look only at assets after the last run that finished (a few index lookups);
# asking "what has no embedding yet?" of the whole library takes ~10-20 s on a hard disk. A run that
# fails leaves the mark where it was, so the next run picks its photos up.
MARK=$INBOX.partial/catalogued-rowid
since=$(cat "$MARK" 2>/dev/null || echo "$top_before")
top_now=$(top)

echo "== $(date +%T) $((after - before)) new assets and sidecars: link, exif, places"
run estate.py link --source "$(basename "$INBOX")"
if command -v exiftool >/dev/null; then
  run estate.py exif --store "$STORE" --after-rowid "$since"
else
  echo "warning: exiftool not installed (brew install exiftool): skipping camera, lens and exposure"
fi
run places.py run
if [[ -z ${SKIP_MODELS:-} ]]; then run embed.py run --store "$STORE" --after-rowid "$since"; fi
# The photo app reloads its catalog when it reads this line: the new photos are searchable now, and
# faces (slow, and only the People page needs them) carry on while it does.
echo "== $(date +%T) catalog ready"
if [[ -z ${SKIP_MODELS:-} ]]; then
  run faces.py run --store "$STORE" --after-rowid "$since"
  mkdir -p "$(dirname "$MARK")" && echo "$top_now" > "$MARK"   # everything up to here is catalogued
fi
if [[ -n ${RESTART:-} ]]; then
  echo "== $(date +%T) restart $RESTART"
  launchctl kickstart -k "gui/$(id -u)/$RESTART"
fi
echo "done $(date +%T)"
